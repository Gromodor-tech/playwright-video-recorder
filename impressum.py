"""
impressum.py - Impressum / Imprint extraction for enrich_companies.py (Internship Task 3).

Kept separate from the script so that everything except the network calls is unit-testable
(tests/test_impressum.py): URL normalization, finding the Impressum link, visible-text extraction,
TOKEN OPTIMIZATION, the DeepSeek client, answer normalization and the status decision.

Pipeline for one company (enrich_company):
    web -> normalize URL -> fetch home page -> find Impressum link -> fetch Impressum page
        -> visible text -> optimize_text() -> DeepSeek (JSON mode) -> normalize -> status

Token optimization (optimize_text) in short - see the function for details:
    1. under MAX_TEXT_CHARS the text is sent unchanged
    2. boilerplate lines (cookie banners, menus, (c) ...) are dropped
    3. the legal block is located by STRONG SIGNALS (phone, VAT id, e-mail, register, responsible
       person, German postcode+city) and a head anchor ("Impressum", "Anbieter", ...);
       only [head anchor - margin, end of the signal cluster + M] is kept
    4. verification + fallbacks (classic first-anchor/last-signal cut, whole text, key lines,
       head+tail window)

Only beautifulsoup4 / requests are third-party; both are imported lazily.
"""

import enum
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Status codes of companies.status (defined ONCE, used everywhere by name)
# ---------------------------------------------------------------------------
class CompanyStatus(enum.IntEnum):
    OK = 0                       # Impressum parsed AND address complete
    NO_IMPRESSUM_LINK_FOUND = 1  # home page has no Impressum/Imprint link
    PARSING_FAILED = 2           # Impressum parsed but no fields detected (empty JSON)
    ADDRESS_INCOMPLETE = 3       # fields parsed but postcode/city/street/house_no not all filled
    ERROR = 4                    # transport failure: unreachable, HTTP 4xx/5xx, DeepSeek API error
    NO_WEBSITE = 5               # web column is empty
    # NULL in the database = "pending" (imported, not processed yet)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
MAX_TEXT_CHARS = 8000            # about 2000 tokens with the rough heuristic tokens = len(text) // 4
TAIL_MARGIN = 400                # M: characters kept after the last signal of the legal block
HEAD_MARGIN = 150                # characters kept before the head anchor
HEAD_LOOKBACK = 800              # how far before the first signal a head anchor is searched
HEAD_FALLBACK = 600              # characters kept before the first signal when there is no head anchor
CLUSTER_GAP = 700                # signals closer than this (chars) belong to the same legal block

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 impressum-enrichment/1.0")

SYSTEM_PROMPT = """\
You extract legally required company information ("Impressum" / "Imprint") from
German-language website text.
Return ONLY a valid JSON object (no markdown fences, no commentary) that parses
with json.loads. The JSON must have exactly these keys:
{
  "company_name": "<string or null>",
  "postal_address": {
    "street": "<string or null>",
    "house_no": "<string or null>",
    "postcode": "<string or null>",
    "city": "<string or null>",
    "country": "<string or null>"
  },
  "handelsregister": "<string or null>",
  "vat_number": "<string or null>",
  "phone": "<string or null>",
  "email": "<string or null>",
  "responsible_person": "<string or null>"
}
Rules:
- Extract ONLY information actually present in the text; use null when absent.
- Do not invent or guess values.
- phone: keep the international format when possible (e.g. +49 ...).
- vat_number: German VAT ids look like DE followed by 9 digits.
- email: lowercase.
- responsible_person: the named person or role legally responsible for the
  content (e.g. Geschaeftsfuehrer, Vorstand, "inhaltlich verantwortlich").
- Output valid JSON only.
"""


def estimate_tokens(text_or_len):
    """Rough token estimate required by the task: tokens ~ len(text) // 4."""
    length = text_or_len if isinstance(text_or_len, int) else len(text_or_len)
    return length // 4


# ---------------------------------------------------------------------------
# URLs and links
# ---------------------------------------------------------------------------
def normalize_url(web):
    """'www.example.de' -> 'https://www.example.de'. None for an empty or unusable value."""
    value = (web or "").strip()
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value.lstrip("/")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return value


def _parser_name():
    try:
        import lxml  # noqa: F401
        return "lxml"
    except ImportError:
        return "html.parser"


def make_soup(content):
    """BeautifulSoup over bytes (the parser reads the charset from the page) or text."""
    from bs4 import BeautifulSoup
    return BeautifulSoup(content, _parser_name())


_LINK_TEXT = ("impressum", "imprint")


def find_impressum_link(content, base_url):
    """Absolute URL of the Impressum/Imprint page, or None.

    1. the first <a> whose visible text (stripped, lower-cased) is "impressum" or "imprint";
    2. otherwise the first <a> whose href contains impressum / imprint.
    Relative links are resolved against the final URL of the home page (urljoin).
    """
    soup = make_soup(content)
    anchors = [a for a in soup.find_all("a", href=True)
               if not a["href"].strip().lower().startswith(("javascript:", "mailto:", "tel:", "#"))]
    chosen = None
    for a in anchors:
        if a.get_text(" ", strip=True).strip().lower() in _LINK_TEXT:
            chosen = a["href"]
            break
    if chosen is None:
        for a in anchors:
            href = a["href"].lower()
            if "impressum" in href or "imprint" in href:
                chosen = a["href"]
                break
    if chosen is None:
        return None
    absolute = urllib.parse.urljoin(base_url, chosen.strip())
    return urllib.parse.urldefrag(absolute)[0]


# ---------------------------------------------------------------------------
# Visible text
# ---------------------------------------------------------------------------
_CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table",
               "section", "article", "address", "dl", "dt", "dd", "blockquote", "pre", "main",
               "aside", "hr", "figure", "details", "summary"]
_CELL_TAGS = ["td", "th"]
_AT_RE = re.compile(r"\s*[\[(]\s*(?:at|AT)\s*[\])]\s*")
_DOT_RE = re.compile(r"\s*[\[(]\s*(?:dot|DOT)\s*[\])]\s*")


def decode_cfemail(encoded):
    """Cloudflare e-mail protection: hex string, the first byte is the XOR key."""
    try:
        key = int(encoded[:2], 16)
        chars = [chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded) - 1, 2)]
    except (ValueError, IndexError):
        return ""
    return urllib.parse.unquote("".join(chars)).strip()


def reveal_emails(soup):
    """Many sites hide e-mail addresses from bots (Cloudflare: '[email protected]' in the visible text).
    Put the real address back, otherwise it would be lost for the extraction."""
    for element in soup.find_all(attrs={"data-cfemail": True}):
        address = decode_cfemail(element["data-cfemail"])
        if address:
            element.replace_with(address)
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        match = _CF_HREF_RE.search(href)
        if match:
            address = decode_cfemail(match.group(1))
            if address:
                anchor.clear()
                anchor.append(address)
        elif href.strip().lower().startswith("mailto:"):
            address = urllib.parse.unquote(href.strip()[7:].split("?")[0]).strip()
            if "@" in address and "@" not in anchor.get_text():
                anchor.append(" " + address)


def normalize_text(raw):
    """Collapse whitespace; one non-empty line per block; drop immediately repeated lines."""
    raw = raw.replace("\xa0", " ").replace("\u200b", "").replace("\u00ad", "")
    raw = _DOT_RE.sub(".", _AT_RE.sub("@", raw))
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def extract_visible_text(content, strict=True):
    """Visible text of a page (NOT html): script/style/noscript/svg/form (and in strict mode
    nav/header/footer) are removed, protected e-mail addresses are revealed first."""
    soup = make_soup(content)
    reveal_emails(soup)
    drop = ["script", "style", "noscript", "svg", "form"]
    if strict:
        drop += ["nav", "header", "footer"]
    for tag in soup.find_all(drop):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for tag in soup.find_all(_CELL_TAGS):
        tag.insert_after(" ")
    return normalize_text(soup.get_text())


# ---------------------------------------------------------------------------
# Token optimization
# ---------------------------------------------------------------------------
# Strong, hard-to-confuse signals of the legal block. National phone numbers ("T 0251 135 66-0",
# "Mobil: 0151 ...") and VAT ids written with spaces ("DE 217 733 235") are as common as the
# international forms, so they are recognized too.
_P = re.compile
SIGNAL_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("phone", _P(r"(?:\+|\b00)\s?49[\s()\-/.]*\d(?:[\s()\-/.]*\d){5,}"
                 r"|(?<![A-Za-z0-9])(?:tel(?:efon)?|phone|fon|mobil(?:e|telefon)?|handy|fax|[tf])\b\.?"
                 r"\s*[:/]?\s*(?:\+\s?\d|\(?0\d)(?:[\s()\-/.]*\d){5,}", re.I)),
    ("vat", _P(r"\bDE[\s.\-]?\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}\b|\bUSt\.?[\s\-]?Id\w*"
               r"|\bUmsatzsteuer[\s\-]?Identifikationsnummer", re.I)),
    ("email", _P(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("register", _P(r"\bHR[AB]\s?\d+|\bRegistergericht|\bAmtsgericht|\bHandelsregister\w*"
                    r"|\bRegisternummer", re.I)),
    ("responsible", _P(r"\bverantwortlich\w*|\bvertreten\s+durch|\bvertretungsberechtigt\w*"
                       r"|\bGesch[äa]ftsf[üu]hr\w*|\bInhaber\w*|\bGesellschafter\w*|\bVorstand", re.I)),
    ("address", _P(r"\b\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\-]{2,}")),
]
_VERIFY_KINDS = {"phone", "vat", "email", "register", "address"}

HEAD_ANCHOR_RE = _P(r"Angaben\s+gem[äa][ßs]s?\s*§\s*5|Impressum|Anbieter\w*|Diensteanbieter"
                    r"|Imprint|Legal\s+notice", re.I)

# lines that are certainly not legal data (only short lines, and never when they hold a signal)
_BOILERPLATE_RE = _P(
    r"^(?:©.*|copyright.*|\(c\)\s*\d{4}.*|alle rechte vorbehalten.*|all rights reserved.*"
    r"|datenschutz(?:erkl[äa]rung|hinweise?|einstellungen|bestimmungen)?|privacy(?: policy)?"
    r"|agb|allgemeine gesch[äa]ftsbedingungen|widerruf\w*(?: ?belehrung)?|nutzungsbedingungen"
    r"|versand(?:kosten)?|zahlungsarten|cookies?[\w\- ]*|(?:alle |nur )?(?:cookies? )?"
    r"(?:akzeptieren|ablehnen|zulassen|erlauben)(?: .*)?|einstellungen(?: [äa]ndern)?|men[üu]"
    r"|navigation|startseite|home|suche(?:n)?|warenkorb.*|newsletter.*|sprache|language"
    r"|deutsch|english|englisch|fran[cç]ais|italiano|espa[ñn]ol|nederlands"
    r"|zum (?:haupt)?inhalt springen|nach oben|zur[üu]ck|weiterlesen|mehr erfahren"
    r"|login|anmelden|registrieren|mein konto|sitemap|kontakt|folgen sie uns|seite teilen"
    r"|facebook|instagram|twitter|youtube|linkedin|pinterest|tiktok|xing)$", re.I)

_KEY_LINE_RE = _P(r"stra(?:ß|ss)e|\bstr\.|\bweg\b|\bplatz\b|allee|gasse|\bring\b|\btel|\bfon\b|fax|mobil"
                  r"|e-?mail|@|\bust|\bhr[ab]\b|register|amtsgericht|vertreten|verantwortlich"
                  r"|gesch[äa]ftsf|inhaber|gesellschafter|vorstand|\b\d{5}\b|gmbh|\bag\b|\bkg\b"
                  r"|\bgbr\b|\bug\b|\be\.?\s?k\.?\b|e\.\s?v\.", re.I)


@dataclass
class OptimizedText:
    text: str
    chars_before: int
    chars_after: int
    method: str                  # unchanged | boilerplate | cluster | classic | key_lines | relaxed | head_tail
    note: str = ""

    @property
    def tokens_before(self):
        return estimate_tokens(self.chars_before)

    @property
    def tokens_after(self):
        return estimate_tokens(self.chars_after)


def find_signals(text):
    """All strong signals as sorted (start, end, kind)."""
    found = []
    for kind, pattern in SIGNAL_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.end(), kind))
    found.sort()
    return found


def has_signal(line):
    return any(pattern.search(line) for _kind, pattern in SIGNAL_PATTERNS)


def strip_boilerplate(lines):
    """Step 1 of the strategy: drop lines that are obviously not legal data."""
    kept = [line for line in lines
            if not (len(line) <= 80 and _BOILERPLATE_RE.match(line.strip()) and not has_signal(line))]
    # duplicate "Impressum" menu items: keep only the standalone heading closest to the legal data
    standalone = [i for i, line in enumerate(kept) if line.strip().lower() in ("impressum", "imprint")]
    if len(standalone) > 1:
        first_signal = next((i for i, line in enumerate(kept) if has_signal(line)), len(kept))
        before = [i for i in standalone if i < first_signal]
        keep_index = before[-1] if before else standalone[0]
        kept = [line for i, line in enumerate(kept) if i not in standalone or i == keep_index]
    return kept


def _line_start(text, pos):
    return text.rfind("\n", 0, max(pos, 0)) + 1


def _line_end(text, pos):
    end = text.find("\n", min(pos, len(text)))
    return len(text) if end == -1 else end


def best_cluster(signals, gap=CLUSTER_GAP):
    """The legal block is one dense group of signals. Group signals that are at most `gap`
    characters apart and keep the best group: most DIFFERENT kinds of signals, then most signals,
    then the earliest. (A lone e-mail / phone in a later 'no advertising' paragraph loses.)"""
    clusters = []
    current = [signals[0]]
    current_end = signals[0][1]
    for signal in signals[1:]:
        if signal[0] - current_end <= gap:
            current.append(signal)
            current_end = max(current_end, signal[1])
        else:
            clusters.append(current)
            current = [signal]
            current_end = signal[1]
    clusters.append(current)
    return max(clusters, key=lambda c: (len({s[2] for s in c}), len(c), -c[0][0]))


def cluster_window(text, signals, head_margin=HEAD_MARGIN, tail_margin=TAIL_MARGIN):
    """Improved sandwich: the cut is [head anchor - margin, end of the best signal cluster + M]."""
    cluster = best_cluster(signals)
    c_start = min(s[0] for s in cluster)
    c_end = max(s[1] for s in cluster)
    anchor = next(iter(HEAD_ANCHOR_RE.finditer(text, max(0, c_start - HEAD_LOOKBACK), c_start)), None)
    start = (anchor.start() - head_margin) if anchor else (c_start - HEAD_FALLBACK)
    return _line_start(text, start), _line_end(text, c_end + tail_margin)


def classic_window(text, signals, head_margin=HEAD_MARGIN, tail_margin=TAIL_MARGIN):
    """The recommended baseline: first head anchor ... last signal + M."""
    anchor = HEAD_ANCHOR_RE.search(text)
    start = _line_start(text, (anchor.start() - head_margin) if anchor else 0)
    end = _line_end(text, max(s[1] for s in signals) + tail_margin)
    return (start, end) if start < end else None


def key_lines(text, context=1):
    """Optional refinement: keep only lines with a known key (Straße, Tel, E-Mail, USt, HRB, ...)
    plus a small context window."""
    lines = text.split("\n")
    keep = set()
    for index, line in enumerate(lines):
        if _KEY_LINE_RE.search(line):
            keep.update(range(max(0, index - context), min(len(lines), index + context + 1)))
    return "\n".join(lines[i] for i in sorted(keep))


def head_tail_window(text, max_chars):
    """Last resort: first N + last N characters (legal data is rarely in the very middle)."""
    marker = "\n[...]\n"
    half = max(1, (max_chars - len(marker)) // 2)
    return text[:half] + marker + text[-half:]


def verify(text):
    """After cutting, the result must still contain a head anchor AND at least one strong signal."""
    return bool(HEAD_ANCHOR_RE.search(text)) and any(
        kind in _VERIFY_KINDS for _s, _e, kind in find_signals(text))


def optimize_text(text, max_chars=MAX_TEXT_CHARS, tail_margin=TAIL_MARGIN, head_margin=HEAD_MARGIN):
    """Cut the visible Impressum text down to the legally relevant block.

    * <= max_chars: returned unchanged.
    * Boilerplate lines are dropped; if that is enough, done.
    * Otherwise the legal block is found by its strong signals (phone, VAT id, e-mail, register,
      responsible person, postcode+city). Signals form clusters; the best cluster is the legal block
      (a single e-mail in a later paragraph does not drag the whole disclaimer in, which the
      "last match + M" rule does). The text is cut to [head anchor - head_margin : cluster end +
      tail_margin] on line boundaries.
    * The cut is verified (head anchor + a strong signal present). On failure the classic
      first-anchor/last-signal cut is tried, then the whole text, and only as the very last resort
      key lines / the first N + last N characters.
    """
    before = len(text)
    if before <= max_chars:
        return OptimizedText(text, before, before, "unchanged")

    stripped = "\n".join(strip_boilerplate(text.split("\n")))
    if len(stripped) <= max_chars:
        return OptimizedText(stripped, before, len(stripped), "boilerplate")

    signals = find_signals(stripped)
    if signals:
        candidates = [("cluster", cluster_window(stripped, signals, head_margin, tail_margin))]
        classic = classic_window(stripped, signals, head_margin, tail_margin)
        if classic:
            candidates.append(("classic", classic))
        for method, (start, end) in candidates:
            piece = stripped[start:end]
            if len(piece) > max_chars:
                shortened = key_lines(piece)
                if shortened and len(shortened) <= max_chars:
                    piece, method = shortened, method + "+key_lines"
                else:
                    piece, method = head_tail_window(piece, max_chars), method + "+head_tail"
            if verify(piece):
                return OptimizedText(piece, before, len(piece), method)

    if len(stripped) <= max_chars:
        return OptimizedText(stripped, before, len(stripped), "relaxed", "verification failed")
    window = head_tail_window(stripped, max_chars)
    return OptimizedText(window, before, len(window), "head_tail", "no usable anchor/signal")


# ---------------------------------------------------------------------------
# DeepSeek client (JSON mode)
# ---------------------------------------------------------------------------
class TransportError(Exception):
    """Network failure, HTTP 4xx/5xx or an API error -> status ERROR."""


class ConfigError(Exception):
    """Missing configuration (for example DEEPSEEK_API_KEY)."""


@dataclass
class ExtractionOutcome:
    parsed: Optional[dict]               # None = empty / unparseable even after the retries
    usage: dict = field(default_factory=dict)
    attempts: int = 1


def parse_json_object(content):
    """JSON object from the model output (tolerates ```json fences); None if there is none."""
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0] if "{" in text else None):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class DeepSeekClient:
    """Calls POST {base_url}/chat/completions with JSON mode. The API key comes from the
    DEEPSEEK_API_KEY environment variable (never hard-coded, never logged)."""

    RETRYABLE_HTTP = (408, 409, 425, 429)

    def __init__(self, api_key=None, model=None, base_url=None, post=None, sleep=time.sleep,
                 max_attempts=3, timeout=60):
        self.api_key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ConfigError("DEEPSEEK_API_KEY is not set (environment variable or .env file)")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL).rstrip("/")
        self._post = post
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.timeout = timeout

    def _payload(self, text):
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": "Extract the Impressum data from the text below and return JSON:\n\n" + text},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 1024,
            "temperature": 0,
        }

    def extract(self, text):
        """-> ExtractionOutcome. Blank / non-JSON answers are retried (backoff), then parsed=None.
        Network errors, 429/5xx are retried; other HTTP 4xx and exhausted retries raise TransportError."""
        post = self._post
        if post is None:
            import requests
            post = requests.post
        problem = ""
        transport = False
        usage = {}
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                self._sleep(2 ** (attempt - 1))
            try:
                response = post(self.base_url + "/chat/completions",
                                headers={"Authorization": "Bearer " + self.api_key},
                                json=self._payload(text), timeout=self.timeout)
            except Exception as exc:                       # requests.RequestException and friends
                problem, transport = "{}: {}".format(type(exc).__name__, exc), True
                continue
            code = response.status_code
            if code >= 400:
                problem, transport = "DeepSeek HTTP {}: {}".format(code, _snippet(response)), True
                if code >= 500 or code in self.RETRYABLE_HTTP:
                    continue
                raise TransportError(problem)              # 401 bad key, 402 balance, 400 bad request, ...
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage") or usage
            except (ValueError, KeyError, IndexError, TypeError):
                problem, transport = "unexpected DeepSeek response format", False
                continue
            parsed = parse_json_object(content)
            if parsed is not None:
                return ExtractionOutcome(parsed, usage, attempt)
            problem, transport = "blank or non-JSON answer", False
        if transport:
            raise TransportError(problem)
        return ExtractionOutcome(None, usage, self.max_attempts)


def _snippet(response):
    try:
        return (response.text or "").strip().replace("\n", " ")[:200]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Answer normalization and the status decision (section 4.6)
# ---------------------------------------------------------------------------
_EMPTY_WORDS = {"", "null", "none", "n/a", "na", "-", "--", "unknown", "nicht angegeben"}
_STREET_NO_RE = re.compile(r"^(?P<street>.*?\D)\s*(?P<no>\d+\s?[A-Za-z]?(?:\s?[-–/]\s?\d+\s?[A-Za-z]?)?)$")


def _text(value):
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return None if text.lower() in _EMPTY_WORDS else text


@dataclass
class Extraction:
    legal_name: Optional[str] = None
    street: Optional[str] = None
    house_no: Optional[str] = None
    postcode: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    postal_address: Optional[str] = None
    handelsregister: Optional[str] = None
    vat_number: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    responsible_person: Optional[str] = None
    detected: bool = False               # at least one field was found

    @property
    def address_complete(self):
        return all((self.postcode, self.city, self.street, self.house_no))


def normalize_extraction(parsed):
    """Parsed DeepSeek JSON -> Extraction. Tolerates missing keys, 'null' strings, numbers,
    an address given as one string, and a house number left glued to the street."""
    parsed = parsed if isinstance(parsed, dict) else {}
    address = parsed.get("postal_address")
    address_text = None
    if isinstance(address, dict):
        comp = {k: _text(address.get(k)) for k in ("street", "house_no", "postcode", "city", "country")}
    else:
        comp = {k: None for k in ("street", "house_no", "postcode", "city", "country")}
        address_text = _text(address)

    if comp["street"] and not comp["house_no"]:               # "Am Mittelhafen 56" in one field
        match = _STREET_NO_RE.match(comp["street"])
        if match and len(match.group("street").strip(" ,")) >= 3:
            comp["street"], comp["house_no"] = match.group("street").strip(" ,"), match.group("no").strip()
    if comp["postcode"] and not comp["city"]:                  # "48155 Münster" in one field
        match = re.match(r"^(\d{4,5})\s+(.+)$", comp["postcode"])
        if match:
            comp["postcode"], comp["city"] = match.group(1), match.group(2).strip()

    email = _text(parsed.get("email"))
    result = Extraction(
        legal_name=_text(parsed.get("company_name")),
        handelsregister=_text(parsed.get("handelsregister")),
        vat_number=_text(parsed.get("vat_number")),
        phone=_text(parsed.get("phone")),
        email=email.lower() if email else None,
        responsible_person=_text(parsed.get("responsible_person")),
        **comp,
    )
    street_line = " ".join(x for x in (result.street, result.house_no) if x)
    city_line = " ".join(x for x in (result.postcode, result.city) if x)
    result.postal_address = ", ".join(x for x in (street_line, city_line, result.country) if x) or address_text
    result.detected = any((result.legal_name, result.handelsregister, result.vat_number, result.phone,
                           result.email, result.responsible_person, result.postal_address))
    return result


def decide_status(extraction):
    """Section 4.6, in this order: PARSING_FAILED, ADDRESS_INCOMPLETE, OK."""
    if extraction is None or not extraction.detected:
        return CompanyStatus.PARSING_FAILED
    if not extraction.address_complete:
        return CompanyStatus.ADDRESS_INCOMPLETE
    return CompanyStatus.OK


# ---------------------------------------------------------------------------
# Fetching pages
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """A page could not be fetched (unreachable, HTTP error, not HTML, robots.txt)."""


@dataclass
class Page:
    url: str                             # final URL after redirects
    content: bytes


class Fetcher:
    """requests-based page fetcher: browser-like User-Agent, timeout, size limit,
    optional robots.txt check."""

    def __init__(self, session=None, timeout=(10, 30), max_bytes=5 * 1024 * 1024, respect_robots=False,
                 user_agent=USER_AGENT):
        self._session = session
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self._robots = {}

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent,
                                          "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
                                          "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
        return self._session

    def _allowed(self, url):
        from urllib.robotparser import RobotFileParser
        parts = urllib.parse.urlsplit(url)
        base = "{}://{}".format(parts.scheme, parts.netloc)
        parser = self._robots.get(base)
        if parser is None:
            parser = RobotFileParser()
            try:
                response = self.session.get(base + "/robots.txt", timeout=self.timeout)
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except Exception:
                parser.parse([])
            self._robots[base] = parser
        return parser.can_fetch(self.user_agent, url)

    def __call__(self, url):
        import requests
        if self.respect_robots and not self._allowed(url):
            raise FetchError("disallowed by robots.txt: " + url)
        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            raise FetchError("{}: {}".format(type(exc).__name__, _short(exc)))
        try:
            if response.status_code >= 400:
                raise FetchError("HTTP {} for {}".format(response.status_code, url))
            kind = (response.headers.get("Content-Type") or "").lower()
            if kind and not any(t in kind for t in ("html", "xml", "text/plain")):
                raise FetchError("not an HTML page ({})".format(kind.split(";")[0]))
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    break
            return Page(response.url, bytes(data))
        except requests.RequestException as exc:
            raise FetchError("{}: {}".format(type(exc).__name__, _short(exc)))
        finally:
            response.close()


def _short(exc):
    return str(exc).strip().replace("\n", " ")[:200]


# ---------------------------------------------------------------------------
# One company, end to end
# ---------------------------------------------------------------------------
@dataclass
class EnrichmentResult:
    status: CompanyStatus
    impressum_url: Optional[str] = None
    extraction: Optional[Extraction] = None     # only after a DeepSeek answer
    raw_json: Optional[dict] = None
    error: str = ""
    home_url: Optional[str] = None
    chars_before: int = 0
    chars_after: int = 0
    method: str = ""
    prompt_tokens: Optional[int] = None         # reported by the API (usage.prompt_tokens)
    attempts: int = 0


def enrich_company(web, fetch, deepseek, max_chars=MAX_TEXT_CHARS, tail_margin=TAIL_MARGIN):
    """Steps 4.1 - 4.6 for one company; never raises for expected failures.

    fetch(url)    -> Page, raises FetchError
    deepseek      -> object with .extract(text) -> ExtractionOutcome (raises TransportError)
    """
    url = normalize_url(web)
    if not (web or "").strip():
        return EnrichmentResult(CompanyStatus.NO_WEBSITE)
    if url is None:
        return EnrichmentResult(CompanyStatus.ERROR, error="invalid web address: {!r}".format(web))

    try:
        home = fetch(url)
    except FetchError as exc:
        return EnrichmentResult(CompanyStatus.ERROR, error="home page: {}".format(exc), home_url=url)
    link = find_impressum_link(home.content, home.url)
    if link is None:
        return EnrichmentResult(CompanyStatus.NO_IMPRESSUM_LINK_FOUND, home_url=home.url)

    try:
        page = fetch(link)
    except FetchError as exc:
        return EnrichmentResult(CompanyStatus.ERROR, impressum_url=link, home_url=home.url,
                                error="impressum page: {}".format(exc))
    impressum_url = page.url or link

    text = extract_visible_text(page.content, strict=True)
    if not any(kind in _VERIFY_KINDS for _s, _e, kind in find_signals(text)):
        # the legal block may sit inside <header>/<footer>/<nav>: look again without removing them
        relaxed = extract_visible_text(page.content, strict=False)
        if len(relaxed) > len(text):
            text = relaxed
    optimized = optimize_text(text, max_chars=max_chars, tail_margin=tail_margin)

    try:
        outcome = deepseek.extract(optimized.text)
    except TransportError as exc:
        return EnrichmentResult(CompanyStatus.ERROR, impressum_url=impressum_url, home_url=home.url,
                                error="deepseek: {}".format(exc), chars_before=optimized.chars_before,
                                chars_after=optimized.chars_after, method=optimized.method)

    extraction = normalize_extraction(outcome.parsed)
    return EnrichmentResult(
        status=decide_status(extraction), impressum_url=impressum_url, extraction=extraction,
        raw_json=outcome.parsed if outcome.parsed is not None else {}, home_url=home.url,
        chars_before=optimized.chars_before, chars_after=optimized.chars_after, method=optimized.method,
        prompt_tokens=(outcome.usage or {}).get("prompt_tokens"), attempts=outcome.attempts)

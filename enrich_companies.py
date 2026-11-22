"""
enrich_companies.py - Impressum / Imprint enrichment of the companies table (Internship Task 3).

For every selected company (ordered by id) the program

    1. normalizes the web address (no scheme -> https://), empty web -> status NO_WEBSITE
    2. opens the home page and finds the Impressum / Imprint link (none -> NO_IMPRESSUM_LINK_FOUND)
    3. fetches the Impressum page and extracts its visible TEXT (never HTML)
    4. cuts the text down to the legally relevant block (token optimization, see impressum.py)
    5. asks DeepSeek for structured JSON and decides the status (OK / PARSING_FAILED / ADDRESS_INCOMPLETE)
    6. writes the result back to PostgreSQL; a transport/API failure marks the company ERROR and the
       batch continues

Usage:
    python enrich_companies.py [--company "ACME GmbH"] [--limit N] [--status 0]

Default selection: rows that are not OK yet (status IS NULL OR status <> 0) - so the script is
re-runnable and skips OK rows. Retry the failures with --status 4, everything pending with --status pending.
In Docker:   docker compose --profile tools run --rm enrich --limit 2

Configuration (environment variables or a .env file in the working directory / next to this script):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD     PostgreSQL (same as the web service)
    DEEPSEEK_API_KEY                                    required, NEVER hard-code or commit it
    DEEPSEEK_MODEL                                      default deepseek-chat
"""

import argparse
import logging
import os
import sys
import time

import psycopg2.extras

import db
import impressum
from impressum import CompanyStatus

log = logging.getLogger("enrich")

PENDING = "pending"                       # --status pending: rows that were never processed (status IS NULL)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_dotenv(paths=None):
    """Minimal .env reader (KEY=VALUE per line). Only DB_* and DEEPSEEK_* are taken and a variable
    that is already set in the environment always wins."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in paths or [os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")]:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key.startswith(("DB_", "DEEPSEEK_", "IMPRESSUM_")) and value:
                    os.environ.setdefault(key, value)


def parse_status(value):
    """--status N (a status code) or --status pending."""
    if value.strip().lower() in (PENDING, "null", "none"):
        return PENDING
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "status must be a code (0=OK, 1=NO_IMPRESSUM_LINK_FOUND, 2=PARSING_FAILED, "
            "3=ADDRESS_INCOMPLETE, 4=ERROR, 5=NO_WEBSITE) or 'pending'")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Enrich the companies table with the Impressum data found on the company websites.")
    parser.add_argument("--company", help="process only this one company (exact company_name)")
    parser.add_argument("--limit", type=int, metavar="N", help="process at most N rows")
    parser.add_argument("--status", type=parse_status, metavar="CODE",
                        help="process only rows with this status (default: not OK yet, i.e. "
                             "status IS NULL OR status <> 0); 'pending' = never processed")
    parser.add_argument("--max-chars", type=int,
                        default=int(os.environ.get("IMPRESSUM_MAX_CHARS", impressum.MAX_TEXT_CHARS)),
                        help="character threshold of the token optimization (default {})".format(
                            impressum.MAX_TEXT_CHARS))
    parser.add_argument("--delay", type=float, default=1.0,
                        help="pause between companies in seconds - politeness (default 1.0)")
    parser.add_argument("--respect-robots", action="store_true", help="honour robots.txt when fetching pages")
    parser.add_argument("--dry-run", action="store_true", help="do everything except writing to the database")
    return parser


# ---------------------------------------------------------------------------
# Database access (a new connection per operation)
# ---------------------------------------------------------------------------
def select_companies(conn, company=None, status=None, limit=None):
    """Rows to process, ordered by id."""
    where, params = [], []
    if company is not None:
        where.append("company_name = %s")
        params.append(company)
    if status == PENDING:
        where.append("status IS NULL")
    elif status is not None:
        where.append("status = %s")
        params.append(int(status))
    elif company is None:                                  # default: everything that is not OK yet
        where.append("(status IS NULL OR status <> %s)")
        params.append(int(CompanyStatus.OK))
    sql = "SELECT id, company_name, web, status FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def save_result(conn, company_id, csv_name, result):
    """Write one result. After a DeepSeek answer every extracted field is stored; the earlier
    failure paths (no website / no link / transport error) only set the status (and the Impressum URL
    when it is known) so that a failed re-run never wipes data that an earlier run extracted."""
    with conn.cursor() as cur:
        if result.extraction is not None:
            ex = result.extraction
            cur.execute(
                """
                UPDATE companies SET
                    status = %s, impressum_url = %s, legal_name = %s, postal_address = %s,
                    handelsregister = %s, vat_number = %s, phone = %s, email = %s,
                    responsible_person = %s, raw_json = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (int(result.status), result.impressum_url, ex.legal_name or csv_name, ex.postal_address,
                 ex.handelsregister, ex.vat_number, ex.phone, ex.email, ex.responsible_person,
                 psycopg2.extras.Json(result.raw_json), company_id),
            )
        else:
            cur.execute(
                "UPDATE companies SET status = %s, impressum_url = COALESCE(%s, impressum_url), "
                "updated_at = NOW() WHERE id = %s",
                (int(result.status), result.impressum_url, company_id),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def describe(result):
    """One readable line about the outcome."""
    ex = result.extraction
    if ex is None:
        return result.error or ""
    return "legal_name={!r} address={!r} phone={!r} email={!r}".format(
        ex.legal_name, ex.postal_address, ex.phone, ex.email)


def process_company(row, fetcher, client, max_chars):
    """enrich_company() with a safety net: whatever happens, the batch goes on."""
    try:
        return impressum.enrich_company(row["web"], fetcher, client, max_chars=max_chars)
    except Exception as exc:                               # a bug / unexpected page must not stop the batch
        log.exception("unexpected error for %s", row["company_name"])
        return impressum.EnrichmentResult(CompanyStatus.ERROR, error="unexpected: {}: {}".format(
            type(exc).__name__, exc))


def main(argv=None, fetcher=None, client=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    load_dotenv()

    try:
        db.wait_for_db()
        db.init_schema()                                   # the companies table exists even before the web service ran
        conn = db.connect()
        try:
            rows = select_companies(conn, args.company, args.status, args.limit)
        finally:
            conn.close()
    except Exception as exc:
        log.error("database: %s", str(exc).strip().splitlines()[0] if str(exc).strip() else exc)
        return 2
    if not rows:
        log.info("nothing to do: no matching companies")
        return 0

    if client is None and any((row["web"] or "").strip() for row in rows):
        try:
            client = impressum.DeepSeekClient()            # reads DEEPSEEK_API_KEY / DEEPSEEK_MODEL
        except impressum.ConfigError as exc:
            log.error("%s", exc)
            return 2
    if fetcher is None:
        fetcher = impressum.Fetcher(respect_robots=args.respect_robots)

    counts = {}
    total_before = total_after = api_tokens = 0
    total = len(rows)
    for number, row in enumerate(rows, start=1):
        log.info("[%d/%d] %s  (web: %s)", number, total, row["company_name"], row["web"] or "-")
        started = time.time()
        result = process_company(row, fetcher, client, args.max_chars)

        if result.impressum_url:
            log.info("    impressum: %s", result.impressum_url)
        elif result.home_url and result.status == CompanyStatus.NO_IMPRESSUM_LINK_FOUND:
            log.info("    no Impressum/Imprint link on %s", result.home_url)
        if result.chars_before:
            saved = 100.0 * (result.chars_before - result.chars_after) / result.chars_before
            log.info("    text: %d -> %d chars (~%d -> ~%d tokens, %.0f%% less, method=%s)%s",
                     result.chars_before, result.chars_after,
                     impressum.estimate_tokens(result.chars_before), impressum.estimate_tokens(result.chars_after),
                     saved, result.method,
                     "  | API prompt_tokens=%s attempts=%s" % (result.prompt_tokens, result.attempts)
                     if result.prompt_tokens is not None else "")
            total_before += result.chars_before
            total_after += result.chars_after
            api_tokens += result.prompt_tokens or 0
        log_fn = log.warning if result.status == CompanyStatus.ERROR else log.info
        log_fn("    -> %s (%d) in %.1fs  %s", result.status.name, int(result.status), time.time() - started,
               describe(result))
        counts[result.status.name] = counts.get(result.status.name, 0) + 1

        if not args.dry_run:
            try:
                save = db.connect()
                try:
                    save_result(save, row["id"], row["company_name"], result)
                finally:
                    save.close()
            except Exception as exc:                       # keep going, the row stays as it was
                log.error("    could not save %s: %s", row["company_name"], exc)
        if args.delay and number < total and (row["web"] or "").strip():
            time.sleep(args.delay)

    log.info("done: %d company(ies): %s", total, ", ".join("{}={}".format(k, v) for k, v in sorted(counts.items())))
    if total_before:
        log.info("token optimization: %d -> %d chars (~%d -> ~%d tokens), %.0f%% saved%s",
                 total_before, total_after, impressum.estimate_tokens(total_before),
                 impressum.estimate_tokens(total_after), 100.0 * (total_before - total_after) / total_before,
                 "; API prompt_tokens total=%d" % api_tokens if api_tokens else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())

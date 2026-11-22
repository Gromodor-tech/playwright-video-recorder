# Apache + mod_wsgi + Flask video web service, zoom worker, and Impressum enrichment
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends apache2 apache2-dev build-essential libpq-dev curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
 && a2enmod wsgi

# Application + Task 3 modules + Apache configuration
COPY web_service.py worker.py apply_zoom.py app.wsgi db.py impressum.py enrich_companies.py schema.sql ./
COPY apache-video.conf /etc/apache2/sites-available/video-service.conf
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && a2dissite 000-default \
 && a2ensite video-service \
 && mkdir -p /app/uploads \
 && chown -R www-data:www-data /app/uploads \
 && ln -sf /dev/stdout /var/log/apache2/access.log \
 && ln -sf /dev/stderr /var/log/apache2/error.log

EXPOSE 80

ENTRYPOINT ["docker-entrypoint.sh"]
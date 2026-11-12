# Apache + mod_wsgi + Flask video web service
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Apache, headers to build mod_wsgi (apxs), libpq for PostgreSQL, curl for the healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends apache2 apache2-dev build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies (flask, psycopg2, mod_wsgi built for THIS python)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Register mod_wsgi in Apache: `module-config` prints the LoadModule line
RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
 && a2enmod wsgi

# Application + Apache configuration
COPY web_service.py app.wsgi ./
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

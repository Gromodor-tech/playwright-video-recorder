#!/bin/sh
# Starts Apache in the foreground (PID 1 of the web container).
set -e

# environment variables of Apache (APACHE_RUN_DIR, APACHE_LOG_DIR, ...)
. /etc/apache2/envvars

# a stale pid file (after `docker restart`) would stop Apache from starting
mkdir -p "$APACHE_RUN_DIR" "$APACHE_LOCK_DIR"
rm -f "$APACHE_PID_FILE"

# the uploads volume must be writable by the Apache user
mkdir -p /app/uploads
chown www-data:www-data /app/uploads

exec apache2 -D FOREGROUND

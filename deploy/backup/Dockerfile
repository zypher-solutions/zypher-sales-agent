# Nightly database backup. pg_dump from the official Postgres 17 image (Supabase runs 17.x; pg_dump must be the
# same major version or newer), plus rclone to copy the files off the server. Published for amd64 and arm64.
FROM postgres:17-alpine
RUN apk add --no-cache rclone curl tzdata
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]

#!/bin/sh
# Nightly pg_dump of the agent's tables, kept 14 days, copied off the server, reported to Healthchecks.io.
#   backup.sh          wait for BACKUP_AT (local time, TZ) every day, forever (the container's normal mode)
#   backup.sh once     one backup now, then exit (testing: docker compose run --rm backup once)
# Restore: see docs/runbooks/server-setup.md, "Restore a backup".
set -u

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
AT="${BACKUP_AT:-02:30}"

ping() {  # $1 = "" for success or "/fail"; silent when no URL is set; never fails the backup
    [ -n "${HEALTHCHECKS_BACKUP_URL:-}" ] && curl -fsS -m 10 --retry 3 -o /dev/null "${HEALTHCHECKS_BACKUP_URL}$1" || true
}

backup_once() {
    stamp=$(date +%Y-%m-%d)
    file="/backups/zypher-internal-$stamp.dump"
    echo "$(date '+%F %T') backup starting"
    # public = every table the agent owns. Supabase's own schemas (auth, storage...) are not ours to restore.
    # Custom format (-Fc) is compressed and lets pg_restore pick tables. The URL is never printed.
    if ! pg_dump --format=custom --no-owner --no-privileges --schema=public --file="$file.part" "$DATABASE_URL"; then
        echo "$(date '+%F %T') BACKUP FAILED (pg_dump)"; rm -f "$file.part"; ping /fail; return 1
    fi
    mv "$file.part" "$file"
    echo "$(date '+%F %T') wrote $file ($(du -h "$file" | cut -f1))"
    find /backups -name 'zypher-internal-*.dump' -mtime +"$((KEEP_DAYS - 1))" -delete
    if [ -n "${BACKUP_REMOTE:-}" ]; then
        # sync = the remote mirrors this folder, so it also keeps only the last 14 days
        if ! rclone sync /backups "$BACKUP_REMOTE" --include 'zypher-internal-*.dump' --quiet; then
            echo "$(date '+%F %T') BACKUP FAILED (copy off the server)"; ping /fail; return 1
        fi
        echo "$(date '+%F %T') copied to $BACKUP_REMOTE"
    else
        echo "$(date '+%F %T') WARNING: BACKUP_REMOTE is empty: copies are on this server only"
    fi
    ping ""
}

if [ "${1:-}" = "once" ]; then
    backup_once; exit $?
fi

echo "backup service: daily at $AT ($TZ), keeping $KEEP_DAYS days"
while true; do
    now=$(date +%s)
    next=$(date -d "$(date +%F) $AT" +%s 2>/dev/null || date -D '%Y-%m-%d %H:%M' -d "$(date +%F) $AT" +%s)
    [ "$next" -le "$now" ] && next=$((next + 86400))
    sleep $((next - now))
    backup_once || true  # a failed night is reported (Healthchecks); try again tomorrow
done

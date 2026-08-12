#!/usr/bin/env bash
# Restore ONLY the tables the graph needs, into a disposable Postgres 18 container.
#
# WHY POSTGRES AND NOT A COPY-STREAM PARSER. The graph needs joins the dump does not
# pre-materialize: play counts live in aggregate_plays, follower counts in
# aggregate_user, permalinks are (handle, slug) from track_routes. Those are three
# lines of SQL and three subtle bugs in a hand-rolled parser.
#
# WHY 18 AND NOT THE SYSTEM POSTGRES. The archive is format 1.16 (pg_dump 17+); the
# system pg_restore here is 16 and refuses it with "unsupported version (1.16)".
#
# WHY SELECTIVE. The dump is a PRODUCTION SOCIAL DATABASE — it contains chat,
# chat_message and chat_blocked_users. We restore the catalogue and the public social
# graph and nothing else, so private messages are never written to disk at all.
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER=audius-pg
VOLUME=audius-pgdata
DUMP=${1:-dump.bin}
DB=audius

# Every table the 16 relations and 6 node types are derived from. Nothing else.
TABLES=(
  tracks users playlists playlist_tracks track_routes stems
  aggregate_user aggregate_plays aggregate_track aggregate_playlist
  follows saves reposts related_artists user_tips
)

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "[restore] starting $CONTAINER"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # A NAMED VOLUME, not a bind mount. Postgres 18 moved the expected mount point to
  # /var/lib/postgresql (data now lives in a `18/docker` subdirectory so pg_upgrade
  # --link can work), and a host bind mount at the old .../data path makes the
  # container exit at boot. A named volume sidesteps both that and the uid-999
  # ownership problem a bind mount creates. Remove with:
  #   docker rm -f audius-pg && docker volume rm audius-pgdata
  docker volume create "$VOLUME" >/dev/null
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=audius -e POSTGRES_DB="$DB" \
    -v "$VOLUME:/var/lib/postgresql" \
    -v "$PWD:/work" \
    -p 55432:5432 \
    postgres:latest >/dev/null
  echo "[restore] waiting for postgres"
  for i in $(seq 120); do
    docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 1
  done
fi

ARGS=()
for t in "${TABLES[@]}"; do ARGS+=(--table="$t"); done

echo "[restore] restoring ${#TABLES[@]} tables (this reads the whole 24 GB archive)"
# --jobs needs a seekable file, which is why the dump is on disk rather than piped.
# Output goes straight to the log, unpiped: `| grep | tail` buffers the entire run,
# so a job this long would show nothing at all until it finished.
docker exec "$CONTAINER" pg_restore \
  --dbname="postgresql://postgres:audius@localhost/$DB" \
  --no-owner --no-privileges --jobs 4 --verbose \
  "${ARGS[@]}" "/work/$DUMP" >restore-detail.log 2>&1 || true
echo "[restore] pg_restore finished; last lines:"
tail -5 restore-detail.log

echo "[restore] row counts:"
docker exec "$CONTAINER" psql -U postgres -d "$DB" -tA -c "
  select relname, n_live_tup from pg_stat_user_tables order by n_live_tup desc;" \
  | awk -F'|' '{printf \"  %-22s %12s\\n\", $1, $2}'
echo "[restore] on-disk size:"
docker exec "$CONTAINER" psql -U postgres -d "$DB" -tA -c "select pg_size_pretty(pg_database_size('$DB'));"

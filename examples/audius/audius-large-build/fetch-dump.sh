#!/usr/bin/env bash
# Download Audius' published Postgres dump, resumably.
#
# 26.3 GB. S3 drops connections, and a dump streamed into a pipe CANNOT resume — a
# drop restarts the whole transfer from zero. So: to disk, with `curl -C -`.
# See audius-status.txt "bulk data / embedding at scale" for the rest of the traps.
set -uo pipefail
cd "$(dirname "$0")"
URL=https://audius-pgdump.s3.us-west-2.amazonaws.com/discProvProduction.dump
OUT=${1:-dump.bin}
EXPECT=26258738086
for attempt in $(seq 1 40); do
  have=$( [ -f "$OUT" ] && stat -c%s "$OUT" || echo 0 )
  if [ "$have" -ge "$EXPECT" ]; then echo "COMPLETE $have"; exit 0; fi
  echo "attempt $attempt: have $((have/1024/1024)) MiB of $((EXPECT/1024/1024)) MiB"
  curl -fsSL -C - --connect-timeout 30 --speed-limit 1048576 --speed-time 60 -o "$OUT" "$URL" || true
  sleep 3
done
have=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
[ "$have" -ge "$EXPECT" ] && echo "COMPLETE $have" || { echo "INCOMPLETE $have"; exit 1; }

#!/usr/bin/env sh
# Container entrypoint: migrate once (guarded by a Postgres advisory lock inside
# alembic/env.py), then hand PID 1 over to the bot so signals are delivered.
set -eu

tries=0
until alembic upgrade head; do
  tries=$((tries + 1))
  if [ "$tries" -ge 10 ]; then
    echo "migrations failed after $tries attempts" >&2
    exit 1
  fi
  echo "migration attempt $tries failed, retrying in $((tries * 2))s" >&2
  sleep $((tries * 2))
done

exec python -m app.main

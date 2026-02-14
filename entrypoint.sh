#!/usr/bin/env sh
set -eu

tries=0
until alembic upgrade head
do
  tries=$((tries+1))
  if [ "$tries" -ge 10 ]; then
    exit 1
  fi
  sleep $((tries*2))
done

exec python -m app.main

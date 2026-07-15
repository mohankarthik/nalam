#!/bin/sh
# Runs supercronic's baked crontab and the web UI in the same container.
# If the web UI process dies, the container dies with it (exec, not &) so
# Docker's restart policy notices; supercronic runs alongside in the
# background the same way it always has.
set -e

supercronic /app/crontab &

exec python run_webui.py

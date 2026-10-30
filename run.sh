#!/usr/bin/env bash
# One click: brings the tunnel up if it is down, starts the dashboard, opens
# the browser. Ctrl+C stops both — but only the tunnel this script started,
# so it will not kill one you are running yourself in another terminal.
set -u

cd "$(dirname "$0")"
BOLD=$'\033[1m'; DIM=$'\033[2m'; OK=$'\033[32m'; WARN=$'\033[33m'; BAD=$'\033[31m'; OFF=$'\033[0m'

DB_HOST=${DB_HOST:-127.0.0.1}
DB_PORT=${DB_PORT:-15432}
TUNNEL_HOST=${DB_TUNNEL_HOSTNAME:-sql.myrealprofit.io}
PORT=${PORT:-5000}
LOG=${TMPDIR:-/tmp}/mrp-tunnel.log
STARTED_TUNNEL=""

port_open() {
    python3 - "$DB_HOST" "$DB_PORT" <<'EOF' 2>/dev/null
import socket, sys
with socket.create_connection((sys.argv[1], int(sys.argv[2])), 2):
    pass
EOF
}

cleanup() {
    if [ -n "$STARTED_TUNNEL" ] && kill -0 "$STARTED_TUNNEL" 2>/dev/null; then
        printf '\n%sStopping the tunnel this script started (pid %s)…%s\n' "$DIM" "$STARTED_TUNNEL" "$OFF"
        kill "$STARTED_TUNNEL" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

if [ ! -x venv/bin/python ]; then
    printf '%s✗%s No virtualenv. Run ./install.sh first.\n' "$BAD" "$OFF"
    exit 1
fi

if port_open; then
    printf '%s✓%s Tunnel already up on %s:%s\n' "$OK" "$OFF" "$DB_HOST" "$DB_PORT"
else
    if ! command -v cloudflared >/dev/null; then
        printf '%s✗%s cloudflared is not installed. Run ./install.sh first.\n' "$BAD" "$OFF"
        exit 1
    fi
    # A dead instance that still holds the port looks exactly like a slow
    # database, so say so rather than starting a second one that cannot bind.
    #
    # -x matches the executable name. `pgrep -f 'cloudflared access tcp'`
    # would also match any shell whose command line mentions that string,
    # including this script's own caller, and report a tunnel that is not
    # there. (The same trap makes `pkill -f` look like it did nothing.)
    if pgrep -x cloudflared >/dev/null; then
        printf '%s!%s cloudflared is running but %s:%s is not answering.\n' "$WARN" "$OFF" "$DB_HOST" "$DB_PORT"
        printf '  Its tunnel is probably dead. Find it and kill it by pid:\n'
        printf '    %spgrep -af cloudflared%s\n' "$DIM" "$OFF"
        printf '    %skill <pid>%s     (add -9 if it ignores that)\n' "$DIM" "$OFF"
        printf '  Then run this script again.\n'
        exit 1
    fi
    printf '%s…%s Starting the tunnel (first run opens a browser to sign in)\n' "$DIM" "$OFF"
    cloudflared access tcp --hostname "$TUNNEL_HOST" --url "localhost:$DB_PORT" >"$LOG" 2>&1 &
    STARTED_TUNNEL=$!
    for _ in $(seq 1 60); do
        sleep 1
        port_open && break
        if ! kill -0 "$STARTED_TUNNEL" 2>/dev/null; then
            printf '%s✗%s The tunnel exited. Last lines of %s:\n' "$BAD" "$OFF" "$LOG"
            tail -5 "$LOG"
            exit 1
        fi
    done
    if port_open; then
        printf '%s✓%s Tunnel up\n' "$OK" "$OFF"
    else
        printf '%s✗%s Tunnel did not come up in 60s. See %s\n' "$BAD" "$OFF" "$LOG"
        exit 1
    fi
fi

URL="http://127.0.0.1:$PORT"
printf '%s✓%s Dashboard starting on %s%s%s\n' "$OK" "$OFF" "$BOLD" "$URL" "$OFF"
( sleep 3
  for opener in xdg-open open; do
      command -v "$opener" >/dev/null && "$opener" "$URL" >/dev/null 2>&1 && break
  done ) &

exec ./venv/bin/python dashboard.py

#!/usr/bin/env bash
# One-command setup. Run once, answer the prompts, then use ./run.sh.
#
#   ./install.sh
#
# Installs what can be installed and asks for the credentials that are issued
# to you personally and cannot be guessed. Passwords are read without echo and
# never reach your shell history or a process argument.
set -u

cd "$(dirname "$0")"
BOLD=$'\033[1m'; DIM=$'\033[2m'; OK=$'\033[32m'; WARN=$'\033[33m'; BAD=$'\033[31m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
good() { printf '  %s✓%s %s\n' "$OK" "$OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$WARN" "$OFF" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$BAD" "$OFF" "$*"; }

# GNU sed takes `-i`, BSD sed (macOS) takes `-i ''`. Getting this wrong
# leaves a stray backup file or errors out, so detect it once.
if sed --version >/dev/null 2>&1; then sed_i() { sed -i "$@"; }
else sed_i() { sed -i '' "$@"; }; fi

case "$(uname -s)" in
    Darwin) PLATFORM=mac ;;
    Linux)  PLATFORM=linux ;;
    *)      PLATFORM=other ;;
esac

DB_HOST=${DB_HOST:-127.0.0.1}
DB_PORT=${DB_PORT:-15432}
DB_NAME=${DB_NAME:-main}
TUNNEL_HOST=${DB_TUNNEL_HOSTNAME:-sql.myrealprofit.io}
PGPASS=${PGPASSFILE:-$HOME/.pgpass}

say "${BOLD}Operations Hub — setup${OFF}"
say "${DIM}Read SETUP.md first if you have not been granted access yet.${OFF}"

# ── system packages ──────────────────────────────────────────────────────────
step "Checking system packages ($PLATFORM)"
if [ "$PLATFORM" = other ]; then
    bad "This installer supports macOS and Linux."
    say "  On Windows, install WSL first and run it inside Ubuntu:"
    say "    ${DIM}wsl --install -d Ubuntu${OFF}   (PowerShell as administrator, then reboot)"
    exit 1
fi

MISSING=()
command -v python3 >/dev/null || MISSING+=(python3)
command -v curl >/dev/null || MISSING+=(curl)
command -v psql >/dev/null || MISSING+=(psql)
# venv ships with python3 on macOS; on Debian it is a separate package.
VENV_MISSING=""
python3 -c 'import venv' 2>/dev/null || VENV_MISSING=1

if [ ${#MISSING[@]} -eq 0 ] && [ -z "$VENV_MISSING" ]; then
    good "python3, venv, curl, psql all present"
elif [ "$PLATFORM" = linux ] && command -v apt-get >/dev/null; then
    PKGS=()
    for m in "${MISSING[@]}"; do
        [ "$m" = psql ] && PKGS+=(postgresql-client) || PKGS+=("$m")
    done
    [ -n "$VENV_MISSING" ] && PKGS+=(python3-venv)
    warn "missing: ${PKGS[*]}"
    say "  Installing (you may be asked for your password)…"
    sudo apt-get update -qq && sudo apt-get install -y "${PKGS[@]}" \
        && good "installed" || { bad "could not install — do it by hand and re-run"; exit 1; }
elif [ "$PLATFORM" = mac ]; then
    if ! command -v brew >/dev/null; then
        bad "Homebrew is required on macOS. Install it from https://brew.sh then re-run."
        exit 1
    fi
    warn "missing: ${MISSING[*]}"
    for m in "${MISSING[@]}"; do
        case "$m" in
            psql)    brew install libpq && brew link --force libpq ;;
            python3) brew install python ;;
            curl)    brew install curl ;;
        esac
    done
    good "installed"
else
    bad "install these with your package manager, then re-run: ${MISSING[*]}"
    exit 1
fi

# ── cloudflared ──────────────────────────────────────────────────────────────
step "Checking cloudflared"
if command -v cloudflared >/dev/null; then
    good "$(cloudflared --version 2>&1 | head -1)"
else
    warn "not installed"
    if [ "$PLATFORM" = mac ]; then
        if command -v brew >/dev/null; then
            brew install cloudflared && good "installed" \
                || { bad "brew install cloudflared failed"; exit 1; }
        else
            bad "install Homebrew from https://brew.sh, then re-run"; exit 1
        fi
    elif command -v apt-get >/dev/null; then
        say "  Adding Cloudflare's package repository…"
        sudo mkdir -p --mode=0755 /usr/share/keyrings \
        && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
             | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null \
        && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
             | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null \
        && sudo apt-get update -qq && sudo apt-get install -y cloudflared \
        && good "installed" || { bad "install cloudflared by hand, then re-run"; exit 1; }
    else
        bad "install cloudflared from github.com/cloudflare/cloudflared/releases"; exit 1
    fi
fi

# ── virtualenv ───────────────────────────────────────────────────────────────
step "Building the Python environment"
if [ ! -x venv/bin/python ]; then
    rm -rf venv && python3 -m venv venv || { bad "could not create venv"; exit 1; }
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt && good "dependencies installed"

# ── database credentials ─────────────────────────────────────────────────────
step "Database login"
say "  Your own read-only role on database '$DB_NAME'. Not a shared one:"
say "  ${DIM}the role's connection limit is about 3, so two people on one role${OFF}"
say "  ${DIM}will disconnect each other.${OFF}"
printf '  Role name [press Enter to skip this step]: '
read -r DB_ROLE
if [ -n "$DB_ROLE" ]; then
    LINE_PREFIX="$DB_HOST:$DB_PORT:$DB_NAME:$DB_ROLE:"
    if [ -f "$PGPASS" ] && grep -q "^$(printf '%s' "$LINE_PREFIX" | sed 's/[.[\*^$]/\\&/g')" "$PGPASS"; then
        warn "$PGPASS already has a line for this role — leaving it alone"
    else
        printf '  Password (not shown): '
        read -rs DB_PW; echo
        if [ -n "$DB_PW" ]; then
            umask 077
            [ -f "$PGPASS" ] && cp "$PGPASS" "$PGPASS.bak"
            printf '%s%s\n' "$LINE_PREFIX" "$DB_PW" >> "$PGPASS"
            unset DB_PW
            chmod 600 "$PGPASS"
            good "written to $PGPASS (chmod 600)"
        else
            warn "no password entered — add the line to $PGPASS yourself"
        fi
    fi
    # The app defaults to a different role name, so record theirs.
    [ -f .env ] || cp .env.example .env
    if grep -q '^DB_USER=' .env; then
        sed_i "s|^DB_USER=.*|DB_USER=$DB_ROLE|" .env
    else
        printf '\nDB_USER=%s\n' "$DB_ROLE" >> .env
    fi
    good "DB_USER set in .env"
else
    warn "skipped — see SETUP.md section 3"
fi

# ── .env ─────────────────────────────────────────────────────────────────────
step "Application credentials"
if [ ! -f .env ]; then
    cp .env.example .env && good "created .env from the template"
else
    good ".env already exists — not overwriting"
fi
chmod 600 .env
if grep -qE '^DB_PASSWORD=.+' .env; then
    sed_i 's/^DB_PASSWORD=/# DB_PASSWORD=/' .env
    warn "commented out DB_PASSWORD in .env — it overrides ~/.pgpass silently"
fi
say "  Edit .env to fill in the Amazon and production API values:"
say "  ${DIM}nano .env${OFF}"

step "Stripe key (only for the Disable Fetching card)"
if [ -f "$HOME/.stripe_key" ]; then
    good "~/.stripe_key already exists"
else
    printf '  Restricted read-only Stripe key, or Enter to skip: '
    read -rs STRIPE_KEY; echo
    if [ -n "$STRIPE_KEY" ]; then
        umask 077
        printf '%s\n' "$STRIPE_KEY" > "$HOME/.stripe_key"
        unset STRIPE_KEY
        chmod 600 "$HOME/.stripe_key"
        good "written to ~/.stripe_key (chmod 600)"
    else
        warn "skipped — that one card will say the key is missing"
    fi
fi

# ── verify ───────────────────────────────────────────────────────────────────
step "Checking the connection"
if python3 - "$DB_HOST" "$DB_PORT" <<'EOF' 2>/dev/null
import socket, sys
with socket.create_connection((sys.argv[1], int(sys.argv[2])), 3):
    pass
EOF
then
    good "tunnel is up"
    ./venv/bin/python db_smoke.py || warn "see the failures above"
else
    warn "tunnel is not running yet — that is expected on a fresh machine"
    say "  ./run.sh starts it for you, and the first start opens a browser"
    say "  for Cloudflare Access."
fi

step "Done"
say "  Start it with:   ${BOLD}./run.sh${OFF}"
say "  Check anything:  ${DIM}./venv/bin/python db_smoke.py${OFF}"
say "  Full notes:      ${DIM}SETUP.md${OFF}"

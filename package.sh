#!/usr/bin/env bash
# Build a handover folder/zip that contains the code and nothing private.
#
#   ./package.sh            -> operations-hub-YYYY-MM-DD.zip
#
# Uses `git archive`, so only committed, non-ignored files are included. Your
# .env, venv, subscriptions.db and any stray notes cannot slip in — which is
# the whole point, because copying the working folder by hand does ship them.
set -eu

cd "$(dirname "$0")"
BOLD=$'\033[1m'; DIM=$'\033[2m'; OK=$'\033[32m'; BAD=$'\033[31m'; OFF=$'\033[0m'

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    printf '%s✗%s Not a git repository.\n' "$BAD" "$OFF"; exit 1
fi

REF=${1:-HEAD}
STAMP=$(date +%Y-%m-%d)
NAME="operations-hub-$STAMP"
OUT="$PWD/$NAME.zip"

if ! git diff --quiet || ! git diff --cached --quiet; then
    printf '%s!%s You have uncommitted changes. They will NOT be included.\n' "$BOLD" "$OFF"
    printf '  %sCommit them first if the recipient needs them.%s\n\n' "$DIM" "$OFF"
fi

rm -f "$OUT"
git archive --format=zip --prefix="$NAME/" -o "$OUT" "$REF"

printf '%s✓%s %s\n\n' "$OK" "$OFF" "$OUT"
printf '%sContents:%s\n' "$BOLD" "$OFF"
git archive --format=tar --prefix="$NAME/" "$REF" | tar -t | sed 's/^/  /'

cat <<EOF

${BOLD}Before you send it${OFF}
  Nothing private is in that zip: no .env, no venv, no database file.
  The person still needs their own credentials — SETUP.md tells them which
  and who to ask. Do not send them yours.

${BOLD}What they do${OFF}
  unzip $NAME.zip && cd $NAME
  ./install.sh      # once, answers a few questions
  ./run.sh          # every time after that
EOF

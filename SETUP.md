# Operations Hub — setup

A local web dashboard for MyRealProfit operations: stuck SQP reports, SQP
status, Marketing Streams, subscriptions, plan upgrades, inactive-account
cleanup and duplicate detection.

It runs on **your own machine** and serves `http://127.0.0.1:5000`. Nothing is
deployed anywhere.

## Before you start: what it can do

The database connection is **read-only** and cannot be used to change anything.

But several cards **write to production** through APIs, with your credentials:

| Card | What it can change |
| --- | --- |
| Cancel Stuck Reports | cancels report requests at Amazon |
| Batch Reports PUT | triggers report generation per seller |
| Marketing Streams | creates stream subscriptions at Amazon |
| Subscriptions / Upgrader | adds schedulers in production |
| Disable Fetching | removes schedulers, archives stream subscriptions |
| Remover | deletes sellers from MyRealProfit |

Duplicates never executes anything — it only produces SQL for someone with
write access to run.

Treat it as a production tool, not a reporting dashboard.

## 1. Access you need first

Ask for these before installing anything. You cannot reuse someone else's.

- **Cloudflare Access** for `sql.myrealprofit.io`, authorised for your work
  account. Needed for the database tunnel.
- **A read-only Postgres role of your own**, e.g. `ro_<yourname>`, on database
  `main`, with the same grants as the existing read-only role. Do not share a
  role with someone else: the role's `CONNECTION LIMIT` is about 3, so two
  people on one role will knock each other off.
- **A MyRealProfit production login** — your own, for the cards that write.
- **Amazon app credentials** from the team credential store: an SP-API client
  id and secret, and a **separate** Advertising API client id. They are
  different Amazon apps; reusing one for both fails with `invalid_client`.
- **A restricted Stripe key** of your own, read-only on Customers and
  Subscriptions, no write scopes. Only the Disable Fetching card needs it.

Nobody should send you a `.env`, a `~/.pgpass` or a Stripe key over Slack or
email. Each of those is yours to create locally from credentials you were
granted directly.

## 2. Install

```bash
sudo apt install python3-venv postgresql-client     # Debian/Ubuntu
```

cloudflared, via Cloudflare's repo:

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

Then the code:

```bash
git clone <repo-url> ~/dashboard
cd ~/dashboard
git checkout claude/nifty-noether-mv85cu
```

## 3. Database password

One line in `~/.pgpass`, with **your** role and password:

```bash
umask 077
read -rs -p 'password: ' PW && printf '127.0.0.1:15432:main:%s:%s\n' "$USER_ROLE" "$PW" > ~/.pgpass; unset PW; echo
chmod 600 ~/.pgpass
```

Set `USER_ROLE` to your role name first, or just edit the file with `nano`.
The format is `host:port:database:user:password`.

Three things that break this silently:

- **Permissions.** If the file is group- or world-readable, libpq ignores it
  entirely. `chmod 600`.
- **`localhost` instead of `127.0.0.1`.** The tunnel binds IPv4 only, and the
  line must match what the client connects to.
- **`DB_PASSWORD` set in `.env`.** It overrides `~/.pgpass` completely. Leave
  it unset.

## 4. `.env`

```bash
cp .env.example .env
nano .env
```

Fill in the Amazon and production API values. Leave every database setting
commented out unless your role name differs from the default — then set
`DB_USER`. `.env` is gitignored; never commit it.

The Stripe key goes in its own file, not `.env`:

```bash
umask 077 && nano ~/.stripe_key && chmod 600 ~/.stripe_key
```

## 5. Run

Two terminals. The tunnel must be up first, and stay up.

```bash
cloudflared access tcp --hostname sql.myrealprofit.io --url localhost:15432
```

The first run opens a browser for Cloudflare Access. After that the token is
cached. Leave this terminal alone.

```bash
cd ~/dashboard && ./start.sh
```

Then open http://127.0.0.1:5000. `PORT=5050 ./start.sh` if 5000 is taken.

## 6. Check it before using it

```bash
./venv/bin/python check_env.py     # credentials, tunnel, login
./venv/bin/python db_smoke.py      # every card can read what it needs
```

`db_smoke.py` ends with either `All cards can read what they need` or a list
of failures naming the table and whether it is missing or permission-denied.
A permission-denied is a one-line `GRANT` for whoever owns the cluster.

Run both with `./venv/bin/python`, not system `python3` — the dependencies
live in the project's virtualenv.

## 7. When it does not work

| Symptom | Cause |
| --- | --- |
| `connection refused` on 15432 | tunnel is not running |
| `timeout expired` on 15432 | tunnel IS running but its upstream is dead — check its terminal, and `pgrep -af cloudflared` for a stale second copy holding the port |
| `password authentication failed` | wrong password, wrong host in the `.pgpass` line, file not `chmod 600`, or `DB_PASSWORD` set in `.env` |
| `too many connections` | the role allows ~3; close psql or pgAdmin sessions, or lower `DB_MAX_CONNECTIONS` |
| `ModuleNotFoundError` | wrong interpreter — use `./venv/bin/python` |
| `invalid_client` from Amazon | SP-API and Advertising client ids swapped, or a secret was rotated |

A stale cloudflared holding port 15432 while forwarding nothing is the most
confusing failure, because it looks exactly like a slow database. Kill it by
PID and start one fresh.

## Connecting a GUI client

pgAdmin and friends go through the same tunnel — `127.0.0.1:15432`, database
`main`. Use a **different role** from the dashboard's if you have one, because
pgAdmin opens a connection per server plus one per query tab, and will exhaust
a 3-connection limit on its own.

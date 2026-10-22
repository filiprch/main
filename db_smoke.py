#!/usr/bin/env python3
"""
Smoke-test the database connection, per card.

check_env.py proves the tunnel is up and the login works. This proves the
thing after that: every table each card reads is actually readable by this
role, on this database. Run it once after a move; it is the difference
between "I can log in" and "the dashboard will work".

    python3 db_smoke.py

Read-only and fast. Every probe is `select 1 from <table> limit 1`, so it
costs nothing even on the 480-million-row tables, and it never writes.
"""

import os
import sys

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.sql
    from dotenv import load_dotenv
except ImportError as exc:
    # The dependencies live in the project's venv, so running this with the
    # system python fails on the first import with a bare traceback.
    sys.exit(
        f"\n  {exc.name} is not installed in this interpreter.\n\n"
        "  The dependencies live in the project's virtual environment:\n"
        "      ./venv/bin/python db_smoke.py\n\n"
        "  or activate it first:\n"
        "      source venv/bin/activate && python db_smoke.py\n\n"
        "  (./start.sh creates the venv if it is not there yet)\n")

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "15432"))
DB_NAME = os.environ.get("DB_NAME", "main")
DB_USER = os.environ.get("DB_USER", "ro_filip_seidel")
DB_TUNNEL_HOSTNAME = os.environ.get("DB_TUNNEL_HOSTNAME", "sql.myrealprofit.io")

# Which tables each card cannot work without.
CARDS = [
    ("Cancel Stuck Reports", ["amazon_report_info", "amazon_selling_partner",
                              "amazon_selling_api_token", "amazon_region"]),
    ("Global SQP Status", ["amazon_report_info", "amazon_selling_partner"]),
    ("Marketing Streams", ["advertising_profile", "amazon_selling_partner",
                           "my_real_profit_user", "amazon_marketplace"]),
    ("Subscriptions", ["scheduler_config", "amazon_selling_partner"]),
    ("Upgrader", ["scheduler_config", "advertising_profile",
                  "amazon_selling_partner", "amazon_advertising_api_token"]),
    ("Disable Fetching", ["my_real_profit_user", "stripe_detail",
                          "amazon_selling_partner", "advertising_profile",
                          "scheduler_config", "sp_traffic", "sqp_by_asin"]),
    ("Duplicates", ["amazon_merchant_listing", "amazon_referral",
                    "amazon_fba_estimated", "amazon_product_tags",
                    "amazon_sku_to_cogs", "amazon_sku_to_vat"]),
]

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def probe_table(cur, table: str) -> str:
    """'' when readable, else a short reason."""
    try:
        cur.execute(psycopg2.sql.SQL("select 1 from {} limit 1").format(
            psycopg2.sql.Identifier(table)))
        cur.fetchall()
        return ""
    except Exception as exc:
        text = " ".join(str(exc).split())
        if "does not exist" in text:
            return "missing"
        if "permission denied" in text:
            return "permission denied"
        return text[:60]


def main() -> int:
    print("=" * 72)
    print(f"DATABASE SMOKE TEST  —  {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print("=" * 72)

    kwargs = {"host": DB_HOST, "port": DB_PORT, "dbname": DB_NAME,
              "user": DB_USER, "connect_timeout": 10,
              "application_name": "db-smoke"}
    if os.environ.get("DB_PASSWORD"):
        kwargs["password"] = os.environ["DB_PASSWORD"]
    try:
        conn = psycopg2.connect(**kwargs)
    except Exception as exc:
        text = " ".join(str(exc).split())
        low = text.lower()
        start = (f"cloudflared access tcp --hostname {DB_TUNNEL_HOSTNAME} "
                 f"--url localhost:{DB_PORT}")
        print(f"\n{BAD} cannot connect: {text}")
        # Refused and timed out point in opposite directions.
        if "timeout" in low or "timed out" in low:
            print("\n  Timed out, not refused: something IS listening on "
                  f"{DB_HOST}:{DB_PORT}, so cloudflared is")
            print("  running — but its tunnel to the database is down. Check "
                  "the cloudflared terminal.")
            print("  A stale second copy holding the port is the usual cause:")
            print("    pgrep -af cloudflared")
            print("    pkill -f 'cloudflared access tcp'")
            print(f"    {start}")
        elif "refused" in low or "could not connect" in low:
            print("\n  Refused: nothing is listening, so the tunnel is not running.")
            print(f"    {start}")
        elif "password" in low or "authentication" in low:
            print(f"\n  The tunnel is fine; the login was rejected. Check "
                  f"~/.pgpass holds a line for")
            print(f"    {DB_HOST}:{DB_PORT}:{DB_NAME}:{DB_USER}:<password>")
            print("  and that the file is chmod 600 — libpq ignores it otherwise.")
        return 1

    failures = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select version() as v, current_user as u, "
                        "current_database() as d")
            r = cur.fetchone()
            print(f"\n{OK} connected as {r['u']} to {r['d']}")
            print(f"       {r['v'].split(',')[0]}")

            # The role's own connection limit, and how much of it is in use.
            cur.execute("select rolconnlimit from pg_roles where rolname = current_user")
            row = cur.fetchone()
            limit = row["rolconnlimit"] if row else None
            cur.execute("select count(*) as n from pg_stat_activity "
                        "where usename = current_user")
            in_use = cur.fetchone()["n"]
            shown = "unlimited" if limit in (None, -1) else limit
            print(f"\n  Connections: {in_use} in use, role limit {shown}")
            if isinstance(limit, int) and limit != -1 and limit < 3:
                print(f"{WARN} the dashboard hands out 2 at a time "
                      f"(DB_MAX_CONNECTIONS) — lower it below {limit}")

            # The long scans raise this; a server-side cap would truncate them.
            cur.execute("SET statement_timeout = '20min'")
            cur.execute("show statement_timeout")
            got = cur.fetchone()["statement_timeout"]
            if got in ("20min", "1200s", "1200000ms"):
                print(f"{OK} statement_timeout can be raised to 20min")
            else:
                failures += 1
                print(f"{BAD} asked for 20min, got {got!r} — the long scans "
                      "will be cut short")
            cur.execute("SET statement_timeout = '30s'")

            print("\n  Tables, per card")
            seen = {}
            for card, tables in CARDS:
                bad = []
                for t in tables:
                    if t not in seen:
                        seen[t] = probe_table(cur, t)
                        if seen[t]:
                            conn.rollback()
                            cur.execute("SET statement_timeout = '30s'")
                    if seen[t]:
                        bad.append(f"{t} ({seen[t]})")
                if bad:
                    failures += 1
                    print(f"{BAD} {card}")
                    for b in bad:
                        print(f"         {b}")
                else:
                    print(f"{OK} {card}  ({len(tables)} tables)")

            # Overall flow discovers its own tables, so it depends on what
            # this role can see in the catalog rather than on a fixed list.
            print("\n  Overall flow discovery")
            cur.execute("""
                select count(distinct c.table_name) as n
                from information_schema.columns c
                join information_schema.tables t
                  on t.table_schema = c.table_schema
                 and t.table_name = c.table_name
                 and t.table_type = 'BASE TABLE'
                where c.table_schema = 'public'
                  and c.column_name = ANY (%(cols)s)
            """, {"cols": ["amazon_selling_partner_id", "seller_id",
                           "selling_partner_id", "advertiser_id",
                           "created_at", "updated_at", "inserted_at"]})
            n = cur.fetchone()["n"]
            if n:
                print(f"{OK} {n} candidate table(s) visible in the catalog")
            else:
                failures += 1
                print(f"{BAD} the catalog returned nothing — Overall flow "
                      "would find no tables to scan")
    finally:
        conn.close()

    print()
    if failures:
        print(f"{failures} problem(s) above. The dashboard will fail on those cards.")
    else:
        print("All cards can read what they need. The dashboard is good to go.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

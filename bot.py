#!/usr/bin/env python3
"""
Shared Account Access Bot

Usage:
    python bot.py <request_id> [<request_id> ...]

Example:
    python bot.py 238 239 240

Reads each request ID from the database, calls the prod API to grant or
revoke Amazon Selling Partner access, marks the request DONE via API,
and reacts to the corresponding Slack message with the result.

Required environment variables (copy .env.example → .env and fill in):
    DATABASE_URL   postgresql://user:password@host:5432/dbname
    PROD_HOST      https://your-prod-host.com
    API_USERNAME   filip@myrealprofit.com
    API_PASSWORD   your_password
    SLACK_TOKEN    xoxb-your-slack-bot-token
    SLACK_CHANNEL  #internal-shared-account  (optional, this is the default)
"""

import os
import re
import sys

import psycopg2
import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PROD_HOST = os.environ["PROD_HOST"].rstrip("/")
API_USERNAME = os.environ["API_USERNAME"]
API_PASSWORD = os.environ["API_PASSWORD"]
SLACK_TOKEN = os.environ["SLACK_TOKEN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#internal-shared-account")

slack = WebClient(token=SLACK_TOKEN)

_DB_QUERY = """
    SELECT
        sar.id,
        sar.owner_my_real_profit_user_id,
        sar.amazon_selling_partner_id,
        mrpu.id AS mrpu_id,
        sar.email,
        sar.action,
        sar.status,
        sar.created_at
    FROM shared_account_request sar
    LEFT JOIN my_real_profit_user mrpu ON mrpu.username = sar.email
    WHERE sar.status = 'TODO'
      AND sar.id = %s
"""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def fetch_request(request_id: int) -> dict | None:
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(_DB_QUERY, (request_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def api_login() -> str:
    r = requests.post(
        f"{PROD_HOST}/login",
        json={"username": API_USERNAME, "password": API_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    for key in ("token", "accessToken", "access_token", "jwt", "idToken", "id_token"):
        if key in data:
            return data[key]
    raise ValueError(
        f"Could not find auth token in login response. Keys returned: {list(data.keys())}"
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def grant_access(token: str, mrpu_id: int, sp_id: int) -> None:
    r = requests.post(
        f"{PROD_HOST}/user/add-linked-amazon-selling-partners",
        headers=_auth(token),
        json={"myRealProfitUserId": mrpu_id, "amazonSellingPartnerIds": [sp_id]},
        timeout=30,
    )
    r.raise_for_status()


def revoke_access(token: str, mrpu_id: int, sp_id: int) -> None:
    r = requests.post(
        f"{PROD_HOST}/user/remove-linked-amazon-selling-partners",
        headers=_auth(token),
        json={"myRealProfitUserId": mrpu_id, "amazonSellingPartnerIds": [sp_id]},
        timeout=30,
    )
    r.raise_for_status()


def mark_done(token: str, seller_id: int, request_id: int) -> None:
    # If the server returns 405, try changing "put" → "post" here.
    r = requests.put(
        f"{PROD_HOST}/users/shared-accounts/actions",
        params={"sellerId": seller_id, "statuses": ["PENDING", "APPROVED"]},
        headers={**_auth(token), "Content-Type": "application/json"},
        json=[{"id": request_id, "status": "DONE"}],
        timeout=30,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def resolve_channel_id(channel_name: str) -> str:
    name = channel_name.lstrip("#")
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        result = slack.conversations_list(**kwargs)
        for ch in result["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = (result.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise ValueError(
        f"Slack channel {channel_name!r} not found. "
        "Make sure the bot is invited to the channel."
    )


def find_message(channel_id: str, request_id: int) -> dict | None:
    """Scan channel history for a message ending with (ID: <request_id>)."""
    pattern = re.compile(rf"\(ID:\s*{re.escape(str(request_id))}\)\s*$")
    cursor = None
    for _ in range(15):  # up to 15 pages × 200 messages = 3 000 messages
        kwargs: dict = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        result = slack.conversations_history(**kwargs)
        for msg in result["messages"]:
            if pattern.search(msg.get("text", "")):
                return msg
        if not result.get("has_more"):
            break
        cursor = result["response_metadata"]["next_cursor"]
    return None


def add_reaction(channel_id: str, ts: str, emoji: str) -> None:
    try:
        slack.reactions_add(channel=channel_id, timestamp=ts, name=emoji)
    except SlackApiError as exc:
        if exc.response["error"] != "already_reacted":
            raise


def post_reply(channel_id: str, ts: str, text: str) -> None:
    slack.chat_postMessage(channel=channel_id, thread_ts=ts, text=text)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process(request_id: int, channel_id: str, token: str) -> None:
    print(f"\n[{request_id}] Querying database...")
    record = fetch_request(request_id)
    if not record:
        print(f"[{request_id}] No TODO record found — skipped.")
        return

    action = record["action"].upper()
    mrpu_id = record["mrpu_id"]
    sp_id = int(record["amazon_selling_partner_id"])

    print(f"[{request_id}] action={action}  mrpu_id={mrpu_id}  sp_id={sp_id}")

    print(f"[{request_id}] Searching Slack channel for message...")
    msg = find_message(channel_id, request_id)
    if not msg:
        print(f"[{request_id}] WARNING: Slack message not found — proceeding without reactions.")

    # ── User does not exist ──────────────────────────────────────────────────
    if mrpu_id is None:
        print(f"[{request_id}] mrpu_id is NULL — user not found, flagging in Slack.")
        if msg:
            add_reaction(channel_id, msg["ts"], "red_circle")
            post_reply(channel_id, msg["ts"], "User does not exist - need human action")
        return

    # ── Grant or revoke access ───────────────────────────────────────────────
    if action == "ADD":
        print(f"[{request_id}] Granting access...")
        grant_access(token, int(mrpu_id), sp_id)
    elif action in ("REVOKE", "REMOVE"):
        print(f"[{request_id}] Revoking access...")
        revoke_access(token, int(mrpu_id), sp_id)
    else:
        print(f"[{request_id}] Unknown action {action!r} — skipped.")
        return

    # ── Mark request DONE ────────────────────────────────────────────────────
    print(f"[{request_id}] Marking request DONE via API...")
    mark_done(token, sp_id, request_id)

    if msg:
        add_reaction(channel_id, msg["ts"], "white_check_mark")

    print(f"[{request_id}] ✓ Complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    request_ids = [int(x) for x in sys.argv[1:]]
    print(f"Processing {len(request_ids)} request(s): {request_ids}")

    print(f"Resolving Slack channel {SLACK_CHANNEL!r}...")
    channel_id = resolve_channel_id(SLACK_CHANNEL)
    print(f"  → channel ID: {channel_id}")

    print("Logging in to API...")
    token = api_login()
    print("  → logged in.")

    errors: list[tuple[int, str]] = []
    for rid in request_ids:
        try:
            process(rid, channel_id, token)
        except Exception as exc:
            print(f"[{rid}] ERROR: {exc}")
            errors.append((rid, str(exc)))

    print(f"\n{'=' * 50}")
    print(f"Done. {len(request_ids) - len(errors)}/{len(request_ids)} succeeded.")
    if errors:
        print("Failed requests:")
        for rid, err in errors:
            print(f"  [{rid}] {err}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

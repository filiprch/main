#!/usr/bin/env python3
"""
Batch Reports PUT Bot

Performs the Postman request

    PUT {PROD_HOST}/reports/bi/<seller_id>/reports

for every seller in a batch, using one shared JSON body per batch.

You work in "batches": each batch is one request body plus a list of
seller IDs. The bot asks for the body, asks for the seller IDs, shows a
summary, and (after you confirm) fires the PUT for each seller. It then
offers to start another batch.

Interactive usage (recommended):
    python reports_bot.py

    # paste/type the JSON body, finish with a line containing only: END
    # paste/type the seller IDs (space, comma or newline separated),
    #   finish with a blank line
    # confirm, and the bot PUTs to every seller

Non-interactive usage (one batch, good for scripting):
    python reports_bot.py --body-file body.json --sellers 27760726877 12345 ...
    python reports_bot.py --body-file body.json --sellers-file sellers.txt
    python reports_bot.py --body-file body.json --sellers-file sellers.txt --yes
    python reports_bot.py --body-file body.json --sellers 27760726877 --dry-run

Required environment variables (copy .env.example -> .env and fill in):
    PROD_HOST      https://api.myrealprofit.com
    API_USERNAME   filip@myrealprofit.com
    API_PASSWORD   your_password

    # Optional: skip /login and use a token you already have
    #   (e.g. the access_token from the Postman environment)
    ACCESS_TOKEN   eyJhbGci...
"""

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PROD_HOST = os.environ["PROD_HOST"].rstrip("/")
API_USERNAME = os.environ.get("API_USERNAME")
API_PASSWORD = os.environ.get("API_PASSWORD")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def api_login() -> str:
    """Log in via /login and return the bearer token."""
    if not API_USERNAME or not API_PASSWORD:
        raise SystemExit(
            "No ACCESS_TOKEN set and API_USERNAME / API_PASSWORD are missing.\n"
            "Set ACCESS_TOKEN, or both API_USERNAME and API_PASSWORD, in your .env."
        )
    r = requests.post(
        f"{PROD_HOST}/login",
        json={"username": API_USERNAME, "password": API_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    for key in ("accessToken", "token", "access_token", "jwt", "idToken", "id_token"):
        if key in data:
            return data[key]
    raise ValueError(
        f"Could not find auth token in login response. Keys returned: {list(data.keys())}"
    )


def put_reports(token: str, seller_id: str, body: dict) -> requests.Response:
    """PUT the reports body for a single seller."""
    return requests.put(
        f"{PROD_HOST}/reports/bi/{seller_id}/reports",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _read_body_interactive() -> dict:
    print(
        "\nPaste the JSON body for this batch.\n"
        "Finish with a line containing only:  END\n"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    raw = "\n".join(lines).strip()
    if not raw:
        raise ValueError("Empty body.")
    return json.loads(raw)


def _parse_seller_ids(raw: str) -> list[str]:
    """Split on commas/whitespace and keep only non-empty tokens (order preserved, deduped)."""
    tokens = raw.replace(",", " ").split()
    seen: set[str] = set()
    ids: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            ids.append(t)
    return ids


def _read_sellers_interactive() -> list[str]:
    print(
        "\nEnter the seller IDs for this batch (space, comma or newline separated).\n"
        "Finish with an empty line.\n"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        if line.strip() == "":
            continue
        lines.append(line)
    return _parse_seller_ids("\n".join(lines))


def _load_body_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_sellers_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return _parse_seller_ids(fh.read())


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

def run_batch(
    token: str,
    seller_ids: list[str],
    body: dict,
    *,
    dry_run: bool = False,
    delay: float = 0.0,
) -> int:
    """PUT the body to every seller. Returns the number of failures."""
    report_names = [r.get("reportName", "?") for r in body.get("reports", [])]
    print(f"\n{'=' * 60}")
    print(f"Batch: {len(seller_ids)} seller(s), {len(report_names)} report(s) in body")
    if report_names:
        print("  Reports: " + ", ".join(report_names))
    print(f"  Sellers: {', '.join(seller_ids)}")
    if dry_run:
        print("  MODE: dry-run (no requests will be sent)")
    print(f"{'=' * 60}")

    failures: list[tuple[str, str]] = []
    for i, seller_id in enumerate(seller_ids, 1):
        prefix = f"[{i}/{len(seller_ids)}] seller {seller_id}"
        if dry_run:
            print(f"{prefix}: would PUT {PROD_HOST}/reports/bi/{seller_id}/reports")
            continue
        try:
            resp = put_reports(token, seller_id, body)
            if resp.ok:
                print(f"{prefix}: OK ({resp.status_code})")
            else:
                snippet = (resp.text or "").strip().replace("\n", " ")[:200]
                print(f"{prefix}: FAILED ({resp.status_code}) {snippet}")
                failures.append((seller_id, f"HTTP {resp.status_code} {snippet}"))
        except Exception as exc:  # network / timeout / etc.
            print(f"{prefix}: ERROR {exc}")
            failures.append((seller_id, str(exc)))
        if delay:
            time.sleep(delay)

    done = len(seller_ids) - len(failures)
    print(f"\n{'-' * 60}")
    print(f"Batch done. {done}/{len(seller_ids)} succeeded.")
    if failures:
        print("Failed sellers:")
        for seller_id, err in failures:
            print(f"  {seller_id}: {err}")
    return len(failures)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch PUT reports per seller.")
    parser.add_argument("--body-file", help="Path to a JSON file with the request body.")
    parser.add_argument("--sellers", nargs="+", help="Seller IDs (space separated).")
    parser.add_argument("--sellers-file", help="Path to a file with seller IDs.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, send nothing.")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait between requests.")
    args = parser.parse_args()

    # Authenticate once for the whole session.
    if ACCESS_TOKEN:
        print("Using ACCESS_TOKEN from environment.")
        token = ACCESS_TOKEN
    else:
        print("Logging in to API...")
        token = api_login()
        print("  -> logged in.")

    non_interactive = bool(args.body_file or args.sellers or args.sellers_file)

    if non_interactive:
        if not args.body_file:
            parser.error("--body-file is required when running non-interactively.")
        if not (args.sellers or args.sellers_file):
            parser.error("Provide --sellers or --sellers-file when running non-interactively.")
        body = _load_body_file(args.body_file)
        if args.sellers:
            seller_ids = _parse_seller_ids(" ".join(args.sellers))
        else:
            seller_ids = _load_sellers_file(args.sellers_file)
        if not seller_ids:
            parser.error("No seller IDs found.")
        if not args.yes and not args.dry_run:
            reply = input(f"\nPUT to {len(seller_ids)} seller(s) on {PROD_HOST}? [y/N] ")
            if reply.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                sys.exit(1)
        failures = run_batch(token, seller_ids, body, dry_run=args.dry_run, delay=args.delay)
        sys.exit(1 if failures else 0)

    # Interactive mode: loop over as many batches as the user wants.
    total_failures = 0
    while True:
        try:
            body = _read_body_interactive()
        except ValueError as exc:
            print(f"Body error: {exc}")
            continue
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}")
            continue

        seller_ids = _read_sellers_interactive()
        if not seller_ids:
            print("No seller IDs entered — skipping this batch.")
        else:
            report_names = [r.get("reportName", "?") for r in body.get("reports", [])]
            print(f"\nAbout to PUT to {len(seller_ids)} seller(s) on {PROD_HOST}")
            print(f"  Sellers: {', '.join(seller_ids)}")
            print(f"  Reports in body: {', '.join(report_names) or '(none)'}")
            reply = input("Proceed? [y/N] ")
            if reply.strip().lower() in ("y", "yes"):
                total_failures += run_batch(
                    token, seller_ids, body, dry_run=args.dry_run, delay=args.delay
                )
            else:
                print("Batch skipped.")

        again = input("\nStart another batch? [y/N] ")
        if again.strip().lower() not in ("y", "yes"):
            break

    print(f"\nAll done. Total failed sellers across batches: {total_failures}")
    sys.exit(1 if total_failures else 0)


if __name__ == "__main__":
    main()

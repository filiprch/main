#!/usr/bin/env python3
"""
Diagnose .env problems.

    python3 check_env.py

Two passes:

1. FORMAT — reads .env and flags values that look damaged: trailing spaces,
   carriage returns from a Windows/copy-paste edit, quotes that ended up
   inside the value, inline '# comments', and so on. Secrets are never
   printed in full, only lengths and short masked previews.

2. LIVE   — asks Amazon whether each app's client_id/client_secret pair is
   valid, using a deliberately bogus refresh token. Amazon answers:
       invalid_client → the client id/secret are wrong  ✗
       invalid_grant  → the credentials are fine, only the (fake) token was
                        rejected                        ✓
   That isolates a credential problem from a token problem without needing
   a real refresh token.
"""

import os
import sys

import requests
from dotenv import load_dotenv

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# key -> (which card needs it, is it a secret)
KEYS = [
    ("DB_PASSWORD",        "SQP + Marketing Streams", True),
    ("LWA_CLIENT_ID",      "Cancel Stuck Reports (SP-API app)", False),
    ("LWA_CLIENT_SECRET",  "Cancel Stuck Reports (SP-API app)", True),
    ("ADS_CLIENT_ID",      "Marketing Streams (Advertising app)", False),
    ("ADS_CLIENT_SECRET",  "Marketing Streams (only if DB stores Atzr| tokens)", True),
    ("PROD_HOST",          "Batch Reports PUT", False),
    ("API_USERNAME",       "Batch Reports PUT", False),
    ("API_PASSWORD",       "Batch Reports PUT", True),
    ("ACCESS_TOKEN",       "Batch Reports PUT (alternative to password)", True),
]


def mask(value: str, secret: bool) -> str:
    if not value:
        return "(empty)"
    if not secret:
        return value if len(value) <= 46 else f"{value[:34]}…{value[-8:]}"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def raw_lines() -> dict:
    """Map KEY -> raw text after '=' straight from the file, unparsed."""
    out = {}
    if not os.path.exists(ENV_PATH):
        return out
    with open(ENV_PATH, "rb") as fh:
        for raw in fh.read().split(b"\n"):
            line = raw.decode("utf-8", "replace")
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val
    return out


def check_format() -> bool:
    print("=" * 72)
    print("1. FORMAT CHECK  —  " + ENV_PATH)
    print("=" * 72)
    if not os.path.exists(ENV_PATH):
        print("  .env not found. Copy .env.example to .env and fill it in.")
        return False

    raws = raw_lines()
    load_dotenv(ENV_PATH)
    problems = 0

    for key, used_by, secret in KEYS:
        parsed = os.environ.get(key, "")
        raw = raws.get(key)

        if raw is None and not parsed:
            print(f"\n  {key}: not set        [{used_by}]")
            continue

        # Only things that survive parsing actually break the credential.
        # Raw-file quirks that dotenv cleans up are reported as notes.
        warn, notes = [], []
        if parsed != parsed.strip():
            warn.append("leading/trailing whitespace in the value dotenv produced")
        # Passwords may legitimately contain quotes, so only machine-issued
        # credentials are checked for them.
        cred_like = key.endswith(("_ID", "_SECRET", "_TOKEN"))
        if cred_like and ('"' in parsed or "'" in parsed):
            warn.append("quote character inside the value")
        elif not cred_like and (parsed[:1] in ("'", '"') and parsed[-1:] in ("'", '"')):
            warn.append("value is wrapped in quotes — dotenv should have removed them")
        if any(ord(c) < 32 for c in parsed):
            warn.append("control character inside the value")
        if " " in parsed.strip() and key.endswith(("_ID", "_SECRET", "_TOKEN")):
            warn.append("value contains a space — credentials normally have none")
        if raw is not None:
            body = raw.rstrip("\r\n")
            if "\r" in raw:
                notes.append("file has a carriage return here (dotenv strips it)")
            if body != body.rstrip() or body[:1] == " ":
                notes.append("stray spaces around the value in the file (dotenv strips them)")
            if " #" in body:
                notes.append("inline '#' in the file (dotenv treats it as a comment)")

        status = "!!" if warn else "ok"
        print(f"\n  {key}: {status}   len={len(parsed)}   [{used_by}]")
        print(f"    value: {mask(parsed, secret)}")
        for w in warn:
            problems += 1
            print(f"    !! {w}")
        for n in notes:
            print(f"    -  {n}")

    print()
    if problems:
        print(f"  {problems} formatting problem(s) found above.")
    else:
        print("  No formatting problems found.")
    return problems == 0


def probe(label: str, client_id: str, client_secret: str) -> None:
    """Ask Amazon whether this client_id/secret pair is valid."""
    print(f"\n  {label}")
    if not client_id or not client_secret:
        missing = " and ".join(n for n, v in (("id", client_id), ("secret", client_secret)) if not v)
        print(f"    skipped — {missing} not set")
        return
    try:
        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                # Deliberately invalid: we only want Amazon's verdict on the app.
                "refresh_token": "Atzr|CHECK-ENV-DIAGNOSTIC-NOT-A-REAL-TOKEN",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        err = ""
        try:
            err = (resp.json() or {}).get("error", "")
        except Exception:
            pass

        if err == "invalid_client":
            print("    ✗ FAIL — Amazon rejected the client id/secret (invalid_client).")
            print("      The pair is wrong, mismatched, or the secret was rotated.")
        elif err == "invalid_grant":
            print("    ✓ OK — credentials accepted (only the fake refresh token was rejected,")
            print("      which is exactly what this test expects).")
        elif err == "unauthorized_client":
            print("    ✗ FAIL — app is not authorized for refresh_token grants.")
        else:
            print(f"    ? HTTP {resp.status_code}, error={err or '(none)'} — {resp.text[:160]}")
    except Exception as exc:
        print(f"    ? could not reach Amazon: {exc}")


def check_live() -> None:
    print()
    print("=" * 72)
    print("2. LIVE CREDENTIAL CHECK  —  api.amazon.com")
    print("=" * 72)
    probe("SP-API app  (LWA_CLIENT_ID / LWA_CLIENT_SECRET) — Cancel Stuck Reports",
          os.environ.get("LWA_CLIENT_ID", ""), os.environ.get("LWA_CLIENT_SECRET", ""))
    probe("Ads app     (ADS_CLIENT_ID / ADS_CLIENT_SECRET) — Marketing Streams",
          os.environ.get("ADS_CLIENT_ID", ""), os.environ.get("ADS_CLIENT_SECRET", ""))

    lwa, ads = os.environ.get("LWA_CLIENT_ID", ""), os.environ.get("ADS_CLIENT_ID", "")
    if lwa and ads and lwa == ads:
        print("\n  !! LWA_CLIENT_ID and ADS_CLIENT_ID are identical — SP-API and the")
        print("     Advertising API normally use different apps.")


if __name__ == "__main__":
    ok = check_format()
    if "--no-network" not in sys.argv:
        check_live()
    print()

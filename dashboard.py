#!/usr/bin/env python3
"""
SQP Bot Dashboard
Run: python dashboard.py
Then open http://localhost:5000
"""

import datetime
import json
import os
import threading
import time
import uuid

import psycopg2
import psycopg2.extras
import requests as http_requests
from datetime import date, timedelta
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Env values are read lazily / non-fatally so the dashboard still boots when a
# single tool's credentials are absent (e.g. run the Reports PUT card without
# SQP's DB/LWA config, or vice-versa). Each route surfaces its own missing-cred
# error as JSON instead of crashing the whole app at startup.
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "136.112.125.16"),
    "dbname": os.environ.get("DB_NAME", "postgres"),
    "user": os.environ.get("DB_USER", "filip_read_only"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "connect_timeout": 10,
}

LWA_CLIENT_ID     = os.environ.get("LWA_CLIENT_ID", "")
LWA_CLIENT_SECRET = os.environ.get("LWA_CLIENT_SECRET", "")
LWA_TOKEN_URL     = "https://api.amazon.com/auth/o2/token"

SP_REGION_CONFIG = {
    1: {"endpoint": "sellingpartnerapi-na.amazon.com", "aws_region": "us-east-1"},
    2: {"endpoint": "sellingpartnerapi-eu.amazon.com", "aws_region": "eu-west-1"},
    3: {"endpoint": "sellingpartnerapi-fe.amazon.com", "aws_region": "us-west-2"},
}

PAGE_SIZE = 20

# ── Queries ──────────────────────────────────────────────────────────────────

_SQP_STUCK_QUERY = """
    SELECT
        ari.amazon_selling_partner_id,
        asp.name AS seller_name,
        COUNT(*)                                             AS stuck_reports_total,
        COUNT(*) FILTER (WHERE ari.amazon_region_id = 1)    AS region_1_count,
        COUNT(*) FILTER (WHERE ari.amazon_region_id = 2)    AS region_2_count,
        COUNT(*) FILTER (WHERE ari.amazon_region_id = 3)    AS region_3_count,
        asat.refresh_token
    FROM amazon_report_info ari
    JOIN amazon_selling_partner asp
        ON asp.id = ari.amazon_selling_partner_id
    JOIN amazon_selling_api_token asat
        ON asat.amazon_selling_partner_id = ari.amazon_selling_partner_id
    WHERE ari.report_type = 'SQP_BY_ASIN'
      AND status NOT IN ('DONE','UNABLE_TO_GENERATE','REPORT_GENERATED_BY_AMAZON','DOWNLOADING_REPORT')
      AND ari.amazon_requested_report_id IS NOT NULL
    GROUP BY
        ari.amazon_selling_partner_id,
        asp.name,
        asat.refresh_token
    ORDER BY stuck_reports_total DESC
"""

_SQP_STUCK_COUNT = """
    SELECT COUNT(*) FROM (
        SELECT ari.amazon_selling_partner_id
        FROM amazon_report_info ari
        JOIN amazon_selling_partner asp ON asp.id = ari.amazon_selling_partner_id
        JOIN amazon_selling_api_token asat ON asat.amazon_selling_partner_id = ari.amazon_selling_partner_id
        WHERE ari.report_type = 'SQP_BY_ASIN'
          AND status NOT IN ('DONE','UNABLE_TO_GENERATE','REPORT_GENERATED_BY_AMAZON','DOWNLOADING_REPORT')
          AND ari.amazon_requested_report_id IS NOT NULL
        GROUP BY ari.amazon_selling_partner_id, asp.name, asat.refresh_token
    ) sub
"""

_DB_REPORT_IDS = """
    SELECT amazon_requested_report_id
    FROM amazon_report_info
    WHERE report_type = 'SQP_BY_ASIN'
      AND amazon_selling_partner_id = %(sp_id)s
      AND amazon_region_id = %(region_id)s
      AND status NOT IN ('DONE','UNABLE_TO_GENERATE','REPORT_GENERATED_BY_AMAZON','DOWNLOADING_REPORT')
      AND amazon_requested_report_id IS NOT NULL
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(**DB_CONFIG)


def lwa_exchange(refresh_token: str) -> dict:
    resp = http_requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     LWA_CLIENT_ID,
            "client_secret": LWA_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def sp_auth(aws_region: str):
    from requests_aws4auth import AWS4Auth
    return AWS4Auth(
        os.environ.get("AWS_ACCESS_KEY_ID", ""),
        os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        aws_region,
        "execute-api",
        session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
    )


def sp_headers(access_token: str) -> dict:
    return {"Accept": "application/json", "x-amz-access-token": access_token}


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# Step 1 — DB query
@app.route("/api/step1/sqp-stuck")
def step1_sqp_stuck():
    load_all = request.args.get("all") == "true"
    page     = max(1, int(request.args.get("page", 1)))

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SQP_STUCK_COUNT)
                total = cur.fetchone()["count"]

                if load_all:
                    cur.execute(_SQP_STUCK_QUERY)
                else:
                    offset = (page - 1) * PAGE_SIZE
                    cur.execute(_SQP_STUCK_QUERY + " LIMIT %(limit)s OFFSET %(offset)s",
                                {"limit": PAGE_SIZE, "offset": offset})
                rows = cur.fetchall()

        rows_list = []
        for row in rows:
            r = dict(row)
            token = r.get("refresh_token") or ""
            r["refresh_token_preview"] = token[:12] + "..." if len(token) > 12 else token
            rows_list.append(r)

        return jsonify({
            "ok": True, "total": total, "page": page,
            "page_size": len(rows_list) if load_all else PAGE_SIZE,
            "total_pages": 1 if load_all else max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "rows": rows_list,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Step 2 — exchange refresh token → LWA access token (per seller)
@app.route("/api/step2/exchange-token")
def step2_exchange_token():
    sp_id = request.args.get("sp_id")
    if not sp_id:
        return jsonify({"ok": False, "error": "sp_id required"}), 400

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT refresh_token FROM amazon_selling_api_token "
                    "WHERE amazon_selling_partner_id = %s LIMIT 1",
                    (sp_id,),
                )
                row = cur.fetchone()

        if not row:
            return jsonify({"ok": False, "error": "Seller not found"}), 404

        refresh_token = (row["refresh_token"] or "").strip()
        if not refresh_token:
            return jsonify({"ok": False, "error": "No refresh token stored"}), 400

        result = lwa_exchange(refresh_token)
        if result["status_code"] == 200:
            return jsonify({
                "ok": True,
                "access_token": result["body"].get("access_token", ""),
                "expires_in":   result["body"].get("expires_in"),
            })
        return jsonify({"ok": False, "error": f"LWA {result['status_code']}: {result['body']}"}), 400

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Step 3 — check Amazon queue + compare with DB report IDs
@app.route("/api/step3/check-queue")
def step3_check_queue():
    access_token = request.args.get("access_token", "")
    region_id    = int(request.args.get("region_id", 1))
    sp_id        = request.args.get("sp_id", "")

    if not access_token:
        return jsonify({"ok": False, "error": "access_token required"}), 400

    config = SP_REGION_CONFIG.get(region_id)
    if not config:
        return jsonify({"ok": False, "error": f"Unknown region_id {region_id}"}), 400

    try:
        aws_region = os.environ.get("AWS_REGION", config["aws_region"])
        auth = sp_auth(aws_region)

        # 1. Get Amazon IN_QUEUE reports
        resp = http_requests.get(
            f"https://{config['endpoint']}/reports/2021-06-30/reports",
            params={
                "reportTypes":        "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT",
                "processingStatuses": "IN_QUEUE",
                "pageSize":           100,
            },
            auth=auth,
            headers=sp_headers(access_token),
            timeout=30,
        )

        if resp.status_code != 200:
            return jsonify({
                "ok": False, "status_code": resp.status_code,
                "error": str(resp.json().get("errors", resp.text)),
            }), 400

        data = resp.json()
        amazon_reports  = data.get("reports", [])
        amazon_id_set   = {r["reportId"] for r in amazon_reports}

        # 2. Get DB report IDs for this seller+region
        db_ids = []
        if sp_id:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(_DB_REPORT_IDS, {"sp_id": sp_id, "region_id": region_id})
                    db_ids = [row[0] for row in cur.fetchall()]

        db_id_set = set(db_ids)

        # 3. Compare
        stuck_in_amazon     = [i for i in db_ids if i in amazon_id_set]      # in DB + in Amazon → cancel
        cleared_from_amazon = [i for i in db_ids if i not in amazon_id_set]  # in DB, gone from Amazon

        return jsonify({
            "ok":                   True,
            "amazon_count":         len(amazon_reports),
            "db_count":             len(db_ids),
            "stuck_in_amazon":      stuck_in_amazon,
            "cleared_from_amazon":  cleared_from_amazon,
            "next_token":           data.get("nextToken"),
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Step 4 — cancel a report from Amazon queue
@app.route("/api/step4/cancel-report", methods=["POST"])
def step4_cancel_report():
    body         = request.get_json() or {}
    report_id    = body.get("report_id", "")
    access_token = body.get("access_token", "")
    region_id    = int(body.get("region_id", 1))

    if not report_id or not access_token:
        return jsonify({"ok": False, "error": "report_id and access_token required"}), 400

    config = SP_REGION_CONFIG.get(region_id)
    if not config:
        return jsonify({"ok": False, "error": f"Unknown region_id {region_id}"}), 400

    try:
        aws_region = os.environ.get("AWS_REGION", config["aws_region"])
        auth = sp_auth(aws_region)

        resp = http_requests.delete(
            f"https://{config['endpoint']}/reports/2021-06-30/reports/{report_id}",
            auth=auth,
            headers=sp_headers(access_token),
            timeout=30,
        )

        ok = resp.status_code in (200, 204)
        return jsonify({
            "ok":          ok,
            "status_code": resp.status_code,
            "error":       None if ok else str(resp.json().get("errors", resp.text)),
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Global SQP status ─────────────────────────────────────────────────────────

_SQP_GLOBAL_STATUS_QUERY = """
    SELECT
        start_date,
        COUNT(*) FILTER (WHERE status = 'DONE')                                  AS done_count,
        COUNT(*) FILTER (WHERE status NOT IN ('DONE','UNABLE_TO_GENERATE'))       AS not_done_count,
        COUNT(*) FILTER (WHERE status = 'UNABLE_TO_GENERATE')                    AS unable_count,
        COUNT(*)                                                                  AS total_count
    FROM amazon_report_info
    WHERE report_type = 'SQP_BY_ASIN_CONVERT'
      AND start_date >= %(cutoff)s
    GROUP BY start_date
    ORDER BY start_date DESC
"""


@app.route("/api/sqp-global-status")
def sqp_global_status():
    weeks = max(1, min(int(request.args.get("weeks", 4)), 52))

    # start_date is always Sunday; SQP for a given week is only triggered on Tuesday.
    # So the most recent week with data is the Sunday BEFORE the current one.
    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7   # Mon=0…Sun=6 → offset 1…0
    most_recent_sunday = today - timedelta(days=days_since_sunday)
    last_week_sunday = most_recent_sunday - timedelta(weeks=1)  # last completed week
    cutoff = last_week_sunday - timedelta(weeks=weeks - 1)

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SQP_GLOBAL_STATUS_QUERY, {"cutoff": cutoff})
                rows = cur.fetchall()

        return jsonify({
            "ok": True,
            "weeks": weeks,
            "rows": [
                {
                    "start_date":     str(r["start_date"]),
                    "done_count":     r["done_count"],
                    "not_done_count": r["not_done_count"],
                    "unable_count":   r["unable_count"],
                    "total_count":    r["total_count"],
                }
                for r in rows
            ],
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Reports PUT bot — batch PUT /reports/bi/<seller_id>/reports
# ══════════════════════════════════════════════════════════════════════════════

from reports_bot import (  # noqa: E402
    ACCESS_TOKEN as REPORTS_ACCESS_TOKEN,
    PROD_HOST as REPORTS_PROD_HOST,
    _parse_seller_ids,
    api_login as reports_api_login,
    put_reports,
)

_rep_lock = threading.Lock()
_rep_stop_event = threading.Event()
_rep_state = {
    "running": False,
    "dry_run": False,
    "logs": [],  # list of {"i", "t", "level", "msg"}
    "counters": {"total": 0, "done": 0, "ok": 0, "failed": 0},
}


def _rep_log(level: str, msg: str) -> None:
    with _rep_lock:
        _rep_state["logs"].append({
            "i": len(_rep_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _rep_worker(token: str, seller_ids: list, body: dict, dry_run: bool) -> None:
    try:
        mode = "DRY-RUN" if dry_run else "LIVE"
        report_names = [r.get("reportName", "?") for r in body.get("reports", [])]
        _rep_log("info", f"Start [{mode}] - {len(seller_ids)} seller(s), "
                         f"{len(report_names)} report(s): {', '.join(report_names) or '(none)'}")
        for idx, seller_id in enumerate(seller_ids, 1):
            if _rep_stop_event.is_set():
                _rep_log("warn", f"Stopped by user after {idx - 1}/{len(seller_ids)} seller(s).")
                break
            prefix = f"[{idx}/{len(seller_ids)}] seller {seller_id}"
            if dry_run:
                _rep_log("info", f"{prefix}: would PUT {REPORTS_PROD_HOST}/reports/bi/{seller_id}/reports")
                with _rep_lock:
                    _rep_state["counters"]["done"] += 1
                    _rep_state["counters"]["ok"] += 1
                continue
            try:
                resp = put_reports(token, seller_id, body)
                if resp.ok:
                    _rep_log("ok", f"{prefix}: OK ({resp.status_code})")
                    field = "ok"
                else:
                    snippet = (resp.text or "").strip().replace("\n", " ")[:200]
                    _rep_log("error", f"{prefix}: FAILED ({resp.status_code}) {snippet}")
                    field = "failed"
            except Exception as exc:
                _rep_log("error", f"{prefix}: ERROR {exc}")
                field = "failed"
            with _rep_lock:
                _rep_state["counters"]["done"] += 1
                _rep_state["counters"][field] += 1
        c = _rep_state["counters"]
        _rep_log("info", f"Finished. {c['ok']} ok, {c['failed']} failed, {c['done']}/{c['total']} processed.")
    finally:
        with _rep_lock:
            _rep_state["running"] = False


@app.route("/api/reports/start", methods=["POST"])
def reports_start():
    with _rep_lock:
        if _rep_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409

    payload = request.get_json(silent=True) or {}
    raw_body = (payload.get("body") or "").strip()
    raw_sellers = payload.get("sellers") or ""
    dry_run = bool(payload.get("dry_run"))

    if not raw_body:
        return jsonify({"ok": False, "error": "Body is empty."}), 400
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return jsonify({"ok": False, "error": f"Invalid JSON body: {exc}"}), 400

    seller_ids = _parse_seller_ids(raw_sellers)
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400

    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400

    # Authenticate up front so credential errors surface immediately.
    try:
        token = REPORTS_ACCESS_TOKEN if REPORTS_ACCESS_TOKEN else reports_api_login()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    _rep_stop_event.clear()
    with _rep_lock:
        _rep_state["logs"] = []
        _rep_state["counters"] = {"total": len(seller_ids), "done": 0, "ok": 0, "failed": 0}
        _rep_state["dry_run"] = dry_run
        _rep_state["running"] = True
    threading.Thread(
        target=_rep_worker, args=(token, seller_ids, body, dry_run), daemon=True
    ).start()
    return jsonify({"ok": True, "count": len(seller_ids), "sellers": seller_ids})


@app.route("/api/reports/stop", methods=["POST"])
def reports_stop():
    _rep_stop_event.set()
    _rep_log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.route("/api/reports/state")
def reports_state():
    since = request.args.get("since", default=0, type=int)
    with _rep_lock:
        logs = [e for e in _rep_state["logs"] if e["i"] >= since]
        return jsonify({
            "running": _rep_state["running"],
            "dry_run": _rep_state["dry_run"],
            "counters": _rep_state["counters"],
            "logs": logs,
            "next": len(_rep_state["logs"]),
            "prod_host": REPORTS_PROD_HOST,
        })


# ══════════════════════════════════════════════════════════════════════════════
# Marketing Streams — check / add sp-traffic + sp-conversion subscriptions
# ══════════════════════════════════════════════════════════════════════════════

ADS_HOST = os.environ.get("ADVERTISING_HOST", "https://advertising-api.amazon.com").rstrip("/")
# The Ads API app. Falls back to the SQP LWA app when no dedicated Ads app is set.
ADS_CLIENT_ID     = os.environ.get("ADS_CLIENT_ID") or LWA_CLIENT_ID
ADS_CLIENT_SECRET = os.environ.get("ADS_CLIENT_SECRET") or LWA_CLIENT_SECRET

MS_DESTINATION_ARN = os.environ.get(
    "MS_DESTINATION_ARN", "arn:aws:sqs:us-east-1:059267949095:marketing"
)
MS_DATASETS = ["sp-traffic", "sp-conversion"]
MS_MARKETPLACES = {
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
}

_MS_TOKENS_QUERY = """
    SELECT ap.amazon_selling_partner_id AS sp_id,
           aaat.token                   AS token,
           ap.profile_id                AS profile_id
    FROM advertising_profile ap
    LEFT JOIN amazon_advertising_api_token aaat
        ON ap.amazon_selling_partner_id = aaat.amazon_selling_partner_id
    WHERE ap.amazon_selling_partner_id = ANY(%(ids)s)
      AND ap.account_info_marketplace_id = %(marketplace_id)s
"""

_ms_lock = threading.Lock()
_ms_stop_event = threading.Event()
_ms_state = {
    "running": False,
    "mode": "",
    "logs": [],
    "rows": [],
    "counters": {"total": 0, "done": 0, "active": 0, "fixed": 0, "failed": 0},
}


def _ms_log(level: str, msg: str) -> None:
    with _ms_lock:
        _ms_state["logs"].append({
            "i": len(_ms_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _ms_put_row(row: dict) -> None:
    """Insert or replace a result row, keyed by seller+marketplace."""
    with _ms_lock:
        for i, existing in enumerate(_ms_state["rows"]):
            if existing["key"] == row["key"]:
                _ms_state["rows"][i] = row
                return
        _ms_state["rows"].append(row)


def _ads_lwa_exchange(refresh_token: str) -> str:
    """Exchange an Ads refresh token (Atzr|…) for a short-lived access token."""
    if not ADS_CLIENT_ID or not ADS_CLIENT_SECRET:
        raise ValueError(
            "Ads API credentials missing — set LWA_CLIENT_ID/LWA_CLIENT_SECRET "
            "(or ADS_CLIENT_ID/ADS_CLIENT_SECRET) in .env"
        )
    resp = http_requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": ADS_CLIENT_ID,
            "client_secret": ADS_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise ValueError(f"LWA {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("LWA response had no access_token")
    return token


def _ads_access_token(raw_token: str, cache: dict) -> str:
    """Resolve the DB-stored token to a usable bearer token.

    Amazon prefixes refresh tokens with 'Atzr|' and access tokens with 'Atza|'.
    Refresh tokens are exchanged (and cached for the run); access tokens are
    used as-is.
    """
    tok = (raw_token or "").strip()
    if not tok:
        raise ValueError("No advertising token stored for this seller")
    if tok in cache:
        return cache[tok]
    resolved = _ads_lwa_exchange(tok) if tok.startswith("Atzr|") else tok
    cache[tok] = resolved
    return resolved


def _ads_headers(access_token: str, profile_id) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": ADS_CLIENT_ID,
        "Amazon-Advertising-API-Scope": str(profile_id),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ms_get_subscriptions(access_token: str, profile_id) -> dict:
    """Return {dataset: status} for the profile's stream subscriptions."""
    resp = http_requests.get(
        f"{ADS_HOST}/streams/subscriptions",
        headers=_ads_headers(access_token, profile_id),
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise ValueError(
            f"GET subscriptions {resp.status_code}: {resp.text[:120]} "
            "— check Amazon-Advertising-API-ClientId matches the Ads app that "
            "issued this token (set ADS_CLIENT_ID in .env)"
        )
    if resp.status_code != 200:
        raise ValueError(f"GET subscriptions {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    subs = data.get("subscriptions", data) if isinstance(data, dict) else data
    statuses: dict = {}
    for sub in subs or []:
        ds = sub.get("dataSetId")
        status = (sub.get("status") or "").upper()
        if not ds:
            continue
        # An ACTIVE subscription always wins over an archived/older one.
        if statuses.get(ds) != "ACTIVE":
            statuses[ds] = status
    return statuses


def _ms_subscribe(access_token: str, profile_id, dataset: str) -> str:
    """POST a subscription, regenerating clientRequestToken on conflict."""
    last_err = ""
    for _ in range(3):
        body = {
            "clientRequestToken": str(uuid.uuid4()),  # 36 chars, within 22-36
            "dataSetId": dataset,
            "destinationArn": MS_DESTINATION_ARN,
        }
        resp = http_requests.post(
            f"{ADS_HOST}/streams/subscriptions",
            headers=_ads_headers(access_token, profile_id),
            json=body,
            timeout=30,
        )
        if resp.status_code in (200, 201, 202):
            try:
                return resp.json().get("subscriptionId", "created")
            except Exception:
                return "created"
        text = (resp.text or "")[:300]
        last_err = f"{resp.status_code}: {text}"
        # Conflicting clientRequestToken → try a fresh one.
        if resp.status_code == 409 or "clientRequestToken" in text:
            continue
        break
    raise ValueError(last_err or "subscribe failed")


def _ms_fetch_profiles(seller_ids: list, marketplace_id: str) -> dict:
    """Look up {sp_id: {token, profile_id}} for the given sellers."""
    ids = []
    for s in seller_ids:
        try:
            ids.append(int(s))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_MS_TOKENS_QUERY, {"ids": ids, "marketplace_id": marketplace_id})
            rows = cur.fetchall()
    return {str(r["sp_id"]): {"token": r["token"], "profile_id": r["profile_id"]} for r in rows}


def _ms_worker(targets: list, mode: str) -> None:
    """targets: list of (seller_id, marketplace_code). mode: 'check' | 'fix'."""
    token_cache: dict = {}
    try:
        _ms_log("info", f"Start [{mode.upper()}] — {len(targets)} seller/marketplace pair(s)")
        # Surface which Ads app is being used — a ClientId that doesn't match the
        # app that issued the token is the usual cause of a blanket 401.
        cid_src = "ADS_CLIENT_ID" if os.environ.get("ADS_CLIENT_ID") else "LWA_CLIENT_ID"
        cid = ADS_CLIENT_ID or "(unset)"
        cid_short = cid if len(cid) <= 24 else f"{cid[:18]}…{cid[-6:]}"
        _ms_log("info", f"Ads host {ADS_HOST} · ClientId {cid_short} (from {cid_src})")

        # Resolve DB tokens per marketplace in one query each.
        by_mp: dict = {}
        for sid, mp in targets:
            by_mp.setdefault(mp, []).append(sid)
        profiles: dict = {}
        for mp, sids in by_mp.items():
            mp_id = MS_MARKETPLACES.get(mp)
            try:
                found = _ms_fetch_profiles(sids, mp_id)
                profiles[mp] = found
                _ms_log("info", f"{mp}: found advertising profile for {len(found)}/{len(sids)} seller(s)")
            except Exception as exc:
                profiles[mp] = {}
                _ms_log("error", f"{mp}: DB lookup failed — {exc}")

        for idx, (sid, mp) in enumerate(targets, 1):
            if _ms_stop_event.is_set():
                _ms_log("warn", f"Stopped by user after {idx - 1}/{len(targets)}.")
                break

            key = f"{sid}|{mp}"
            row = {
                "key": key, "seller_id": sid, "marketplace": mp,
                "profile_id": None, "traffic": "-", "conversion": "-",
                "state": "error", "error": None, "note": "",
            }
            prefix = f"[{idx}/{len(targets)}] {sid} ({mp})"

            info = (profiles.get(mp) or {}).get(str(sid))
            if not info:
                row["error"] = "No advertising profile for this marketplace"
                _ms_log("error", f"{prefix}: no advertising profile / token in DB")
                _ms_put_row(row)
                with _ms_lock:
                    _ms_state["counters"]["done"] += 1
                    _ms_state["counters"]["failed"] += 1
                continue

            row["profile_id"] = info["profile_id"]
            try:
                access_token = _ads_access_token(info["token"], token_cache)
                statuses = _ms_get_subscriptions(access_token, info["profile_id"])
                row["traffic"] = statuses.get("sp-traffic", "MISSING")
                row["conversion"] = statuses.get("sp-conversion", "MISSING")

                missing = [d for d in MS_DATASETS if statuses.get(d) != "ACTIVE"]

                if mode == "fix" and missing:
                    added = []
                    for ds in missing:
                        if _ms_stop_event.is_set():
                            break
                        try:
                            _ms_subscribe(access_token, info["profile_id"], ds)
                            added.append(ds)
                            _ms_log("ok", f"{prefix}: subscribed {ds}")
                        except Exception as exc:
                            _ms_log("error", f"{prefix}: {ds} failed — {exc}")
                    if added:
                        # Re-read so the row shows Amazon's real post-add state.
                        try:
                            statuses = _ms_get_subscriptions(access_token, info["profile_id"])
                            row["traffic"] = statuses.get("sp-traffic", "MISSING")
                            row["conversion"] = statuses.get("sp-conversion", "MISSING")
                        except Exception:
                            for ds in added:
                                row["traffic" if ds == "sp-traffic" else "conversion"] = "PENDING"
                        row["note"] = "added: " + ", ".join(added)
                        with _ms_lock:
                            _ms_state["counters"]["fixed"] += 1
                    missing = [d for d in MS_DATASETS if statuses.get(d) != "ACTIVE"]

                if not missing:
                    row["state"] = "ok"
                    _ms_log("ok", f"{prefix}: active (traffic + conversion)")
                    with _ms_lock:
                        _ms_state["counters"]["active"] += 1
                else:
                    row["state"] = "missing" if len(missing) == len(MS_DATASETS) else "partial"
                    _ms_log("warn", f"{prefix}: not active — {', '.join(missing)}")

                with _ms_lock:
                    _ms_state["counters"]["done"] += 1

            except Exception as exc:
                row["error"] = str(exc)
                _ms_log("error", f"{prefix}: {exc}")
                with _ms_lock:
                    _ms_state["counters"]["done"] += 1
                    _ms_state["counters"]["failed"] += 1

            _ms_put_row(row)
            time.sleep(0.15)  # be gentle with the Ads API

        c = _ms_state["counters"]
        _ms_log("info", f"Finished. {c['active']} active, {c['fixed']} fixed, "
                        f"{c['failed']} failed, {c['done']}/{c['total']} processed.")
    finally:
        with _ms_lock:
            _ms_state["running"] = False


@app.route("/api/ms/start", methods=["POST"])
def ms_start():
    with _ms_lock:
        if _ms_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409

    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode") or "check"
    if mode not in ("check", "fix"):
        return jsonify({"ok": False, "error": f"Unknown mode {mode!r}"}), 400

    targets = []
    explicit = payload.get("targets")
    if explicit:
        for t in explicit:
            sid = str(t.get("seller_id", "")).strip()
            mp = t.get("marketplace")
            if sid and mp in MS_MARKETPLACES:
                targets.append((sid, mp))
    else:
        seller_ids = _parse_seller_ids(payload.get("sellers") or "")
        marketplaces = [m for m in (payload.get("marketplaces") or []) if m in MS_MARKETPLACES]
        if not seller_ids:
            return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
        if not marketplaces:
            return jsonify({"ok": False, "error": "Select at least one marketplace."}), 400
        for sid in seller_ids:
            for mp in marketplaces:
                targets.append((sid, mp))

    if not targets:
        return jsonify({"ok": False, "error": "Nothing to process."}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    if not ADS_CLIENT_ID:
        return jsonify({"ok": False, "error": "LWA_CLIENT_ID (or ADS_CLIENT_ID) is not set in .env."}), 400

    _ms_stop_event.clear()
    with _ms_lock:
        _ms_state["logs"] = []
        _ms_state["mode"] = mode
        _ms_state["counters"] = {"total": len(targets), "done": 0, "active": 0, "fixed": 0, "failed": 0}
        # A fix run keeps existing rows so unselected sellers stay visible.
        if mode == "check":
            _ms_state["rows"] = []
        _ms_state["running"] = True
    threading.Thread(target=_ms_worker, args=(targets, mode), daemon=True).start()
    return jsonify({"ok": True, "count": len(targets)})


@app.route("/api/ms/stop", methods=["POST"])
def ms_stop():
    _ms_stop_event.set()
    _ms_log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.route("/api/ms/state")
def ms_state():
    since = request.args.get("since", default=0, type=int)
    with _ms_lock:
        return jsonify({
            "running": _ms_state["running"],
            "mode": _ms_state["mode"],
            "counters": _ms_state["counters"],
            "logs": [e for e in _ms_state["logs"] if e["i"] >= since],
            "rows": _ms_state["rows"],
            "next": len(_ms_state["logs"]),
        })


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=True, host=host, port=port)

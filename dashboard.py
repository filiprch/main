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


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=True, host=host, port=port)

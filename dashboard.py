#!/usr/bin/env python3
"""
SQP Bot Dashboard
Run: python dashboard.py
Then open http://localhost:5000
"""

import datetime
import json
import os
import re
import sqlite3
import threading
import time
import uuid

import psycopg2
import psycopg2.extras
import requests as http_requests
from datetime import date, timedelta
from flask import Flask, Response, jsonify, render_template, request
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
    if not LWA_CLIENT_ID or not LWA_CLIENT_SECRET:
        missing = " and ".join(
            n for n, v in (("LWA_CLIENT_ID", LWA_CLIENT_ID),
                           ("LWA_CLIENT_SECRET", LWA_CLIENT_SECRET)) if not v
        )
        raise ValueError(f"{missing} not set in .env (needed for the SP-API token exchange)")
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
        body = result["body"] if isinstance(result["body"], dict) else {}
        if body.get("error") == "invalid_client":
            # The credentials reached Amazon but were rejected: wrong app.
            cid = LWA_CLIENT_ID or "(unset)"
            cid_short = cid if len(cid) <= 30 else f"{cid[:24]}…{cid[-6:]}"
            return jsonify({"ok": False, "error": (
                f"LWA rejected LWA_CLIENT_ID {cid_short} (invalid_client). These must be "
                "the SP-API app's credentials — if you replaced them with the Advertising "
                "app's values, restore LWA_CLIENT_ID/LWA_CLIENT_SECRET and keep the Ads "
                "ones in ADS_CLIENT_ID/ADS_CLIENT_SECRET."
            )}), 400
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


# Per-week drill-down. The three buckets mirror the counts above.
_SQP_BUCKETS = {
    "done":     ("DONE",     "ari.status = 'DONE'"),
    "not_done": ("NOT DONE", "ari.status NOT IN ('DONE','UNABLE_TO_GENERATE')"),
    "unable":   ("UNABLE",   "ari.status = 'UNABLE_TO_GENERATE'"),
}
SQP_DETAIL_DISPLAY_LIMIT = 500


def _sqp_detail_sql(start_date: str, bucket: str, limit=None, offset=None) -> str:
    """Build the drill-down SQL. Values are inlined so it can be pasted into
    pgAdmin as-is; start_date is validated as a date by the caller."""
    _, cond = _SQP_BUCKETS[bucket]
    sql = (
        "SELECT ari.amazon_selling_partner_id  AS seller_id,\n"
        "       asp.name                       AS seller_name,\n"
        "       ar.name                        AS region_name,\n"
        "       ari.status                     AS status,\n"
        "       ari.start_date                 AS start_date,\n"
        "       ari.amazon_requested_report_id AS amazon_requested_report_id\n"
        "FROM amazon_report_info ari\n"
        "LEFT JOIN amazon_selling_partner asp ON asp.id = ari.amazon_selling_partner_id\n"
        "LEFT JOIN amazon_region ar           ON ar.id  = ari.amazon_region_id\n"
        "WHERE ari.report_type = 'SQP_BY_ASIN_CONVERT'\n"
        f"  AND ari.start_date = DATE '{start_date}'\n"
        f"  AND {cond}\n"
        # Fully deterministic ordering, so paging never repeats or skips a row.
        "ORDER BY ari.amazon_selling_partner_id, ari.amazon_region_id,\n"
        "         ari.amazon_requested_report_id"
    )
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    if offset:
        sql += f"\nOFFSET {int(offset)}"
    return sql


# Accepts '2026-08-23' or '2026-08-23 00:00:00' (the form the table renders),
# and nothing else — anything trailing is rejected rather than silently trimmed.
_SQP_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?$")


def _sqp_detail_params():
    """Validate and return (start_date, bucket) from the request."""
    raw = (request.args.get("start_date") or "").strip()
    bucket = (request.args.get("bucket") or "").strip()
    if bucket not in _SQP_BUCKETS:
        raise ValueError(f"Unknown bucket {bucket!r}")
    m = _SQP_DATE_RE.match(raw)
    if not m:
        raise ValueError("start_date must be YYYY-MM-DD")
    # Re-serialised from a parsed date, so only a clean literal is ever inlined.
    start_date = str(datetime.date.fromisoformat(m.group(1)))
    return start_date, bucket


@app.route("/api/sqp-detail")
def sqp_detail():
    try:
        start_date, bucket = _sqp_detail_params()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400

    limit = max(1, min(request.args.get("limit", default=SQP_DETAIL_DISPLAY_LIMIT,
                                        type=int), 5000))
    page = max(1, request.args.get("page", default=1, type=int))
    _, cond = _SQP_BUCKETS[bucket]
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM amazon_report_info ari "
                    "WHERE ari.report_type = 'SQP_BY_ASIN_CONVERT' "
                    f"AND ari.start_date = DATE '{start_date}' AND {cond}"
                )
                total = cur.fetchone()["n"]
                pages = max(1, (total + limit - 1) // limit)
                page = min(page, pages)          # clamp past-the-end requests
                offset = (page - 1) * limit
                cur.execute(_sqp_detail_sql(start_date, bucket, limit, offset))
                rows = cur.fetchall()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({
        "ok": True,
        "bucket": bucket,
        "label": _SQP_BUCKETS[bucket][0],
        "start_date": start_date,
        "total": total,
        "shown": len(rows),
        "page": page,
        "pages": pages,
        "limit": limit,
        "first_row": offset + 1 if rows else 0,
        "last_row": offset + len(rows),
        "has_prev": page > 1,
        "has_next": page < pages,
        "sql": _sqp_detail_sql(start_date, bucket),
        "rows": [
            {
                "seller_id": r["seller_id"],
                "seller_name": r["seller_name"] or "",
                "region_name": r["region_name"] or "",
                "status": r["status"],
                "start_date": str(r["start_date"]),
                "amazon_requested_report_id": r["amazon_requested_report_id"] or "",
            }
            for r in rows
        ],
    })


@app.route("/api/sqp-detail.csv")
def sqp_detail_csv():
    """Full (untruncated) list as CSV — opens directly in Sheets/Excel."""
    try:
        start_date, bucket = _sqp_detail_params()
    except ValueError as exc:
        return Response(f"error,{exc}\n", mimetype="text/csv", status=400)

    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["seller_id", "seller_name", "region_name", "status",
                     "start_date", "amazon_requested_report_id"])
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_sqp_detail_sql(start_date, bucket))
                for r in cur.fetchall():
                    writer.writerow([
                        r["seller_id"], r["seller_name"] or "", r["region_name"] or "",
                        r["status"], r["start_date"], r["amazon_requested_report_id"] or "",
                    ])
    except Exception as exc:
        return Response(f"error,{exc}\n", mimetype="text/csv", status=500)

    filename = f"sqp_{start_date}_{bucket}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
# The Advertising API is a SEPARATE Amazon app from the SP-API one used by the
# SQP cards — deliberately no fallback to LWA_CLIENT_ID. Presenting a token with
# another app's ClientId is rejected with a blanket 401 Unauthorized.
ADS_CLIENT_ID     = os.environ.get("ADS_CLIENT_ID", "")
ADS_CLIENT_SECRET = os.environ.get("ADS_CLIENT_SECRET", "")

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

# Marketing Streams is only available to users on pricing plan 2. Sellers on any
# other plan are filtered out of the results entirely (not shown as a column).
MS_REQUIRED_PRICING_PLAN = 2

_MS_SELLER_INFO_QUERY = """
    SELECT asp.id                 AS sp_id,
           asp.name               AS name,
           mrpu.pricing_plan_id   AS pricing_plan_id
    FROM amazon_selling_partner asp
    JOIN my_real_profit_user mrpu
        ON asp.my_real_profit_user_id = mrpu.id
    WHERE asp.id = ANY(%(ids)s)
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
            "This token needs an LWA exchange — set ADS_CLIENT_ID and "
            "ADS_CLIENT_SECRET (the Advertising app's credentials) in .env"
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


def _ms_fetch_seller_info(seller_ids: list) -> dict:
    """Look up {sp_id: {name, pricing_plan_id}} for the given sellers."""
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
            cur.execute(_MS_SELLER_INFO_QUERY, {"ids": ids})
            rows = cur.fetchall()
    return {
        str(r["sp_id"]): {"name": r["name"], "pricing_plan_id": r["pricing_plan_id"]}
        for r in rows
    }


def _ms_worker(targets: list, mode: str) -> None:
    """targets: list of (seller_id, marketplace_code). mode: 'check' | 'fix'."""
    token_cache: dict = {}
    try:
        _ms_log("info", f"Start [{mode.upper()}] — {len(targets)} seller/marketplace pair(s)")
        # Surface which Ads app is being used — a ClientId that doesn't match the
        # app that issued the token is the usual cause of a blanket 401.
        cid = ADS_CLIENT_ID or "(unset)"
        cid_short = cid if len(cid) <= 30 else f"{cid[:24]}…{cid[-6:]}"
        _ms_log("info", f"Ads host {ADS_HOST} · ClientId {cid_short}")

        # Marketing Streams is a pricing-plan-2 feature: look up seller names and
        # plans once, then drop everyone who is not on the plan.
        try:
            info_map = _ms_fetch_seller_info(sorted({sid for sid, _ in targets}))
        except Exception as exc:
            _ms_log("error", f"Seller lookup failed — {exc}")
            with _ms_lock:
                _ms_state["counters"]["total"] = 0
            return

        allowed, skipped = [], set()
        for sid, mp in targets:
            info = info_map.get(str(sid))
            plan = info.get("pricing_plan_id") if info else None
            try:
                on_plan = int(plan) == MS_REQUIRED_PRICING_PLAN
            except (TypeError, ValueError):
                on_plan = False
            if on_plan:
                allowed.append((sid, mp))
            else:
                skipped.add(sid)
        if skipped:
            _ms_log("warn", f"Hidden — not on pricing plan {MS_REQUIRED_PRICING_PLAN}: "
                            f"{len(skipped)} seller(s): {', '.join(sorted(skipped)[:12])}"
                            f"{'…' if len(skipped) > 12 else ''}")
        targets = allowed
        with _ms_lock:
            _ms_state["counters"]["total"] = len(targets)
        if not targets:
            _ms_log("warn", "Nothing to process — no sellers on the required pricing plan.")
            return

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
                "name": (info_map.get(str(sid)) or {}).get("name") or "",
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
        return jsonify({"ok": False, "error": (
            "ADS_CLIENT_ID is not set in .env. The Advertising API uses a different "
            "Amazon app than SP-API — copy the Amazon-Advertising-API-ClientId value "
            "from your Postman request headers into ADS_CLIENT_ID."
        )}), 400

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


# ══════════════════════════════════════════════════════════════════════════════
# Subscriptions — add analytics schedulers, then track the initial backfill
# ══════════════════════════════════════════════════════════════════════════════

SUBS_DB_PATH = os.environ.get(
    "SUBS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriptions.db")
)
SUBS_RETENTION_HOURS = int(os.environ.get("SUBS_RETENTION_HOURS", "48"))
SUBS_LOOKBACK_DAYS = int(os.environ.get("SUBS_LOOKBACK_DAYS", "546"))
SUBS_REPORT_TYPES = ("AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL", "ORDER_GENERAL")

# Backfill window for ORDER_GENERAL: only sellers whose earliest existing report
# starts after the requested start date need filling in, and only up to the
# earlier of that first report and the seller's data access start.
_SUBS_ORDER_GENERAL_QUERY = """
    WITH vars AS (
        SELECT %(start_date)s::date AS start_date,
               %(seller_ids)s::bigint[] AS seller_ids
    ), seller_min_date_by_type AS (
        SELECT ari.amazon_selling_partner_id,
               ari.amazon_region_id,
               ari.report_type,
               MIN(ari.start_date) AS min_date,
               v.start_date
        FROM amazon_report_info ari
        JOIN vars v ON TRUE
        WHERE ari.amazon_selling_partner_id = ANY (v.seller_ids)
          AND ari.report_type = 'ORDER_GENERAL'
        GROUP BY ari.amazon_selling_partner_id, ari.amazon_region_id,
                 ari.report_type, v.start_date
        HAVING MIN(ari.start_date) > v.start_date
    )
    SELECT st.amazon_selling_partner_id                                   AS seller_id,
           st.report_type                                                 AS report_type,
           ar.name                                                        AS region_name,
           to_char(v.start_date, 'YYYY-MM-DD')                            AS start,
           to_char(LEAST(st.min_date, asp.data_access_start_from), 'YYYY-MM-DD') AS "end"
    FROM seller_min_date_by_type st
    JOIN vars v ON TRUE
    JOIN amazon_selling_partner asp ON asp.id = st.amazon_selling_partner_id
    LEFT JOIN amazon_region ar ON st.amazon_region_id = ar.id
    WHERE v.start_date < LEAST(st.min_date, asp.data_access_start_from)
"""

_SUBS_SHIPMENTS_QUERY = """
    WITH vars AS (
        SELECT %(start_interval)s::date AS start_interval,
               %(end_interval)s::date   AS end_interval,
               %(seller_ids)s::bigint[] AS seller_ids
    )
    SELECT asp.id                                        AS seller_id,
           'AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL'     AS report_type,
           asp.state                                     AS region_name,
           to_char(v.start_interval, 'YYYY-MM-DD')       AS start,
           to_char(v.end_interval, 'YYYY-MM-DD')         AS "end"
    FROM amazon_selling_partner asp
    JOIN vars v ON TRUE
    WHERE asp.id = ANY (v.seller_ids)
"""

# Progress of the initial backfill: anything not DONE/UNABLE_TO_GENERATE is
# still being fetched.
_SUBS_PROGRESS_QUERY = """
    SELECT amazon_selling_partner_id AS seller_id,
           COUNT(*)                                                            AS total,
           COUNT(*) FILTER (WHERE status NOT IN ('DONE','UNABLE_TO_GENERATE')) AS pending
    FROM amazon_report_info
    WHERE amazon_selling_partner_id = ANY (%(ids)s)
      AND report_type IN %(types)s
    GROUP BY amazon_selling_partner_id
"""


def _subs_db():
    conn = sqlite3.connect(SUBS_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _subs_init() -> None:
    with _subs_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_log (
                seller_id      TEXT PRIMARY KEY,
                name           TEXT,
                status         TEXT,
                added_at       TEXT,
                updated_at     TEXT,
                scheduler_rows INTEGER DEFAULT 0,
                total          INTEGER DEFAULT 0,
                pending        INTEGER DEFAULT 0,
                seen_pending   INTEGER DEFAULT 0,
                error          TEXT,
                payload        TEXT
            )
        """)


def _subs_purge() -> int:
    """Drop entries older than the retention window (48h by default)."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=SUBS_RETENTION_HOURS)).isoformat()
    with _subs_db() as conn:
        cur = conn.execute("DELETE FROM subscription_log WHERE added_at < ?", (cutoff,))
        return cur.rowcount or 0


def _subs_rows() -> list:
    _subs_purge()
    with _subs_db() as conn:
        rows = conn.execute(
            "SELECT * FROM subscription_log ORDER BY added_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        added = datetime.datetime.fromisoformat(d["added_at"])
        age = datetime.datetime.now() - added
        d["age_hours"] = round(age.total_seconds() / 3600, 1)
        d["expires_in_hours"] = round(SUBS_RETENTION_HOURS - d["age_hours"], 1)
        d["ready"] = d["status"] == "FETCHED"
        out.append(d)
    return out


def _subs_build_payload(seller_ids: list, lookback_days: int) -> tuple:
    """Return (payload_list, per_seller_counts) for the scheduler request."""
    ids = []
    for s in seller_ids:
        try:
            ids.append(int(s))
        except (TypeError, ValueError):
            continue
    if not ids:
        return [], {}

    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=lookback_days)
    end_interval = today - datetime.timedelta(days=1)

    payload, counts = [], {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_SUBS_ORDER_GENERAL_QUERY,
                        {"start_date": start_date, "seller_ids": ids})
            order_rows = cur.fetchall()
            cur.execute(_SUBS_SHIPMENTS_QUERY,
                        {"start_interval": start_date, "end_interval": end_interval,
                         "seller_ids": ids})
            ship_rows = cur.fetchall()

    # ORDER_GENERAL first, then shipments — same order as the manual process.
    for r in order_rows:
        sid = str(r["seller_id"])
        payload.append({
            "sellerId": sid,
            "reportType": r["report_type"],
            "regionName": r["region_name"],
            "start": r["start"],
            "end": r["end"],
        })
        counts[sid] = counts.get(sid, 0) + 1
    for r in ship_rows:
        sid = str(r["seller_id"])
        payload.append({
            "sellerId": int(r["seller_id"]),
            "reportType": r["report_type"],
            "regionName": r["region_name"],
            "start": r["start"],
            "end": r["end"],
        })
        counts[sid] = counts.get(sid, 0) + 1
    return payload, counts


def _subs_send(token: str, payload: list):
    return http_requests.post(
        f"{REPORTS_PROD_HOST}/admin/scheduler/add",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )


def _subs_refresh() -> dict:
    """Re-read backfill progress for every logged seller and update statuses."""
    rows = _subs_rows()
    if not rows:
        return {"checked": 0, "ready": 0}

    ids = []
    for r in rows:
        try:
            ids.append(int(r["seller_id"]))
        except (TypeError, ValueError):
            continue
    progress = {}
    if ids:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SUBS_PROGRESS_QUERY, {"ids": ids, "types": SUBS_REPORT_TYPES})
                for p in cur.fetchall():
                    progress[str(p["seller_id"])] = {"total": p["total"], "pending": p["pending"]}

    ready = 0
    now = datetime.datetime.now().isoformat()
    with _subs_db() as conn:
        for r in rows:
            sid = r["seller_id"]
            p = progress.get(sid, {"total": 0, "pending": 0})
            seen_pending = r["seen_pending"] or 0
            if p["pending"] > 0:
                status, seen_pending = "FETCHING", 1
            elif seen_pending or p["total"] > 0:
                # Pending work was observed and has now cleared, or reports
                # exist and none are outstanding → the backfill is complete.
                status = "FETCHED"
            else:
                # No reports yet — the scheduler has not produced them.
                status = r["status"] if r["status"] == "ERROR" else "SENT"
            if status == "FETCHED":
                ready += 1
            conn.execute(
                "UPDATE subscription_log SET status=?, total=?, pending=?, "
                "seen_pending=?, updated_at=? WHERE seller_id=?",
                (status, p["total"], p["pending"], seen_pending, now, sid),
            )
    return {"checked": len(rows), "ready": ready}


_subs_lock = threading.Lock()
_subs_state = {"running": False, "logs": []}


def _subs_log(level: str, msg: str) -> None:
    with _subs_lock:
        _subs_state["logs"].append({
            "i": len(_subs_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _subs_worker(seller_ids: list, lookback_days: int, token: str) -> None:
    try:
        _subs_log("info", f"Building scheduler payload for {len(seller_ids)} seller(s), "
                          f"lookback {lookback_days} days")
        try:
            payload, counts = _subs_build_payload(seller_ids, lookback_days)
        except Exception as exc:
            _subs_log("error", f"Query failed — {exc}")
            return
        if not payload:
            _subs_log("warn", "Queries returned no rows — nothing to schedule.")
            return

        names = _ms_fetch_seller_info(seller_ids) if seller_ids else {}
        _subs_log("info", f"Payload has {len(payload)} scheduler row(s): " +
                          ", ".join(f"{s}×{n}" for s, n in counts.items()))

        resp = _subs_send(token, payload)
        ok = 200 <= resp.status_code < 300
        body = (resp.text or "").strip().replace("\n", " ")[:200]
        if ok:
            _subs_log("ok", f"POST /admin/scheduler/add → {resp.status_code} {body}")
        else:
            _subs_log("error", f"POST /admin/scheduler/add → {resp.status_code} {body}")

        now = datetime.datetime.now().isoformat()
        with _subs_db() as conn:
            for sid in {str(s) for s in seller_ids}:
                if sid not in counts:
                    _subs_log("warn", f"{sid}: no scheduler rows produced — not logged")
                    continue
                conn.execute(
                    "INSERT INTO subscription_log (seller_id, name, status, added_at, "
                    "updated_at, scheduler_rows, total, pending, seen_pending, error, payload) "
                    "VALUES (?,?,?,?,?,?,0,0,0,?,?) "
                    "ON CONFLICT(seller_id) DO UPDATE SET status=excluded.status, "
                    "added_at=excluded.added_at, updated_at=excluded.updated_at, "
                    "scheduler_rows=excluded.scheduler_rows, seen_pending=0, "
                    "error=excluded.error, payload=excluded.payload",
                    (sid, (names.get(sid) or {}).get("name", ""), "SENT" if ok else "ERROR",
                     now, now, counts.get(sid, 0), None if ok else f"{resp.status_code} {body}",
                     json.dumps([p for p in payload if str(p["sellerId"]) == sid])),
                )
        if ok:
            _subs_log("info", "Logged. Statuses will move to FETCHING then FETCHED as reports run.")
            try:
                _subs_refresh()
            except Exception as exc:
                _subs_log("warn", f"Initial status refresh failed — {exc}")
    finally:
        with _subs_lock:
            _subs_state["running"] = False


def _subs_token():
    """Bearer token for the prod admin API (same auth as Batch Reports PUT)."""
    if REPORTS_ACCESS_TOKEN:
        return REPORTS_ACCESS_TOKEN
    return reports_api_login()


@app.route("/api/subs/preview", methods=["POST"])
def subs_preview():
    payload_in = request.get_json(silent=True) or {}
    seller_ids = _parse_seller_ids(payload_in.get("sellers") or "")
    lookback = int(payload_in.get("lookback_days") or SUBS_LOOKBACK_DAYS)
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    try:
        payload, counts = _subs_build_payload(seller_ids, lookback)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    missing = [s for s in seller_ids if s not in counts]
    return jsonify({"ok": True, "payload": payload, "counts": counts, "no_rows": missing})


@app.route("/api/subs/add", methods=["POST"])
def subs_add():
    with _subs_lock:
        if _subs_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409
    payload_in = request.get_json(silent=True) or {}
    seller_ids = _parse_seller_ids(payload_in.get("sellers") or "")
    lookback = int(payload_in.get("lookback_days") or SUBS_LOOKBACK_DAYS)
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400
    try:
        token = _subs_token()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    _subs_init()
    with _subs_lock:
        _subs_state["logs"] = []
        _subs_state["running"] = True
    threading.Thread(target=_subs_worker, args=(seller_ids, lookback, token), daemon=True).start()
    return jsonify({"ok": True, "count": len(seller_ids)})


@app.route("/api/subs/refresh", methods=["POST"])
def subs_refresh_route():
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    try:
        _subs_init()
        return jsonify({"ok": True, **_subs_refresh()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/subs/remove", methods=["POST"])
def subs_remove():
    sid = str((request.get_json(silent=True) or {}).get("seller_id", "")).strip()
    if not sid:
        return jsonify({"ok": False, "error": "seller_id required"}), 400
    with _subs_db() as conn:
        conn.execute("DELETE FROM subscription_log WHERE seller_id = ?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/subs/state")
def subs_state():
    since = request.args.get("since", default=0, type=int)
    try:
        _subs_init()
        rows = _subs_rows()
    except Exception as exc:
        rows = []
        _subs_log("error", f"Logbook read failed — {exc}")
    with _subs_lock:
        logs = [e for e in _subs_state["logs"] if e["i"] >= since]
        running = _subs_state["running"]
        nxt = len(_subs_state["logs"])
    return jsonify({
        "running": running, "logs": logs, "next": nxt, "rows": rows,
        "retention_hours": SUBS_RETENTION_HOURS,
        "default_lookback": SUBS_LOOKBACK_DAYS,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Remover — delete sellers from MRP. Destructive: there is no undo.
# ══════════════════════════════════════════════════════════════════════════════

_rm_lock = threading.Lock()
_rm_stop_event = threading.Event()
_rm_state = {
    "running": False,
    "logs": [],
    "counters": {"total": 0, "done": 0, "deleted": 0, "failed": 0},
}


def _rm_log(level: str, msg: str) -> None:
    with _rm_lock:
        _rm_state["logs"].append({
            "i": len(_rm_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _rm_delete(token: str, seller_id: str):
    return http_requests.delete(
        f"{REPORTS_PROD_HOST}/user/remove-seller/{seller_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )


def _rm_worker(token: str, seller_ids: list) -> None:
    try:
        _rm_log("warn", f"Deleting {len(seller_ids)} seller(s) from {REPORTS_PROD_HOST} "
                        "— this cannot be undone.")
        for idx, seller_id in enumerate(seller_ids, 1):
            if _rm_stop_event.is_set():
                _rm_log("warn", f"Stopped by user after {idx - 1}/{len(seller_ids)} seller(s).")
                break
            prefix = f"[{idx}/{len(seller_ids)}] seller {seller_id}"
            try:
                resp = _rm_delete(token, seller_id)
                if resp.ok:
                    _rm_log("ok", f"{prefix}: DELETED ({resp.status_code})")
                    field = "deleted"
                else:
                    snippet = (resp.text or "").strip().replace("\n", " ")[:200]
                    _rm_log("error", f"{prefix}: FAILED ({resp.status_code}) {snippet}")
                    field = "failed"
            except Exception as exc:
                _rm_log("error", f"{prefix}: ERROR {exc}")
                field = "failed"
            with _rm_lock:
                _rm_state["counters"]["done"] += 1
                _rm_state["counters"][field] += 1
            time.sleep(0.1)

        c = _rm_state["counters"]
        _rm_log("info", f"Finished. {c['deleted']}/{c['total']} deleted, {c['failed']} failed.")
    finally:
        with _rm_lock:
            _rm_state["running"] = False


@app.route("/api/remove/start", methods=["POST"])
def remove_start():
    with _rm_lock:
        if _rm_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409

    payload = request.get_json(silent=True) or {}
    seller_ids = _parse_seller_ids(payload.get("sellers") or "")
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400
    try:
        token = REPORTS_ACCESS_TOKEN if REPORTS_ACCESS_TOKEN else reports_api_login()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    _rm_stop_event.clear()
    with _rm_lock:
        _rm_state["logs"] = []
        _rm_state["counters"] = {"total": len(seller_ids), "done": 0, "deleted": 0, "failed": 0}
        _rm_state["running"] = True
    threading.Thread(target=_rm_worker, args=(token, seller_ids), daemon=True).start()
    return jsonify({"ok": True, "count": len(seller_ids), "sellers": seller_ids})


@app.route("/api/remove/stop", methods=["POST"])
def remove_stop():
    _rm_stop_event.set()
    _rm_log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.route("/api/remove/state")
def remove_state():
    since = request.args.get("since", default=0, type=int)
    with _rm_lock:
        return jsonify({
            "running": _rm_state["running"],
            "counters": _rm_state["counters"],
            "logs": [e for e in _rm_state["logs"] if e["i"] >= since],
            "next": len(_rm_state["logs"]),
            "prod_host": REPORTS_PROD_HOST,
        })


# ══════════════════════════════════════════════════════════════════════════════
# Upgrader — CORE → Advanced: add the advertising schedulers that were never
# activated, optionally with Marketing Streams and Subscriptions.
# ══════════════════════════════════════════════════════════════════════════════

# Every advertising report type an Advanced account should have scheduled.
UPGRADER_REPORT_TYPES = [
    "AD_GROUP_SPONSORED_PRODUCTS",
    "AD_GROUP_SPONSORED_BRANDS",
    "AD_GROUP_SPONSORED_VIDEO",
    "AD_GROUP_SPONSORED_DISPLAY",
    "PRODUCT_AD_SPONSORED_PRODUCTS",
    "PRODUCT_AD_SPONSORED_DISPLAY",
    "ASIN_SPONSORED_PRODUCTS",
    "ASIN_SPONSORED_DISPLAY",
    "SEARCH_TERM_SPONSORED_PRODUCTS",
    "SEARCH_TERM_SPONSORED_BRANDS_KEYWORDS",
    "SEARCH_TERM_SPONSORED_VIDEO_KEYWORDS",
    "PURCHASED_PRODUCT_SPONSORED_BRANDS",
    "LISTING_DETAILS",
    "SPONSORED_PRODUCTS",
    "SPONSORED_VIDEO_CAMPAIGN",
    "V4_SPONSORED_BRANDS_CAMPAIGNS",
    "SPONSORED_PRODUCTS_CAMPAIGNS",
    "SPONSORED_BRANDS",
    "SPONSORED_DISPLAY",
    "SPONSORED_VIDEO",
    "SPONSORED_PRODUCTS_PRODUCT_ADS",
    "SPONSORED_PORTFOLIOS",
    "SPONSORED_BRAND_CAMPAIGN",
]

# marketplace_id is needed to drive the Marketing Streams step, so it is
# selected alongside the seller/region/marketplace combination.
_UPG_COMBOS_QUERY = """
    SELECT ap.amazon_selling_partner_id AS selling_partner_id,
           ar.name                      AS region_name,
           am.name                      AS marketplace_name,
           am.id                        AS marketplace_id
    FROM advertising_profile ap
    LEFT JOIN amazon_marketplace am ON ap.account_info_marketplace_id = am.id
    LEFT JOIN amazon_region ar      ON am.amazon_region_id = ar.id
    WHERE ap.amazon_selling_partner_id = ANY (%(ids)s)
    ORDER BY 1, 2, 3
"""

_upg_lock = threading.Lock()
_upg_stop_event = threading.Event()
_upg_state = {
    "running": False,
    "logs": [],
    "combos": [],
    "counters": {"sellers": 0, "combos": 0, "ad_rows": 0, "ms_added": 0,
                 "subs": 0, "failed": 0},
}


def _upg_log(level: str, msg: str) -> None:
    with _upg_lock:
        _upg_state["logs"].append({
            "i": len(_upg_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _upg_fetch_combos(seller_ids: list) -> list:
    """Every (seller, region, marketplace) the seller has an ad profile in."""
    ids = []
    for s in seller_ids:
        try:
            ids.append(int(s))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_UPG_COMBOS_QUERY, {"ids": ids})
            rows = [dict(r) for r in cur.fetchall()]

    return _upg_annotate(rows)


def _upg_annotate(rows: list) -> list:
    """Attach the per-row result fields the UI tracks. Safe to call twice."""
    supported = set(MS_MARKETPLACES.values())
    for c in rows:
        if "key" in c:
            continue
        usable = bool(c.get("region_name") and c.get("marketplace_name"))
        c["key"] = f"{c.get('selling_partner_id')}|{c.get('marketplace_id')}"
        c["streams_supported"] = c.get("marketplace_id") in supported
        c["sched_total"] = len(UPGRADER_REPORT_TYPES) if usable else 0
        c["sched_sent"] = 0
        c["sched_status"] = "PENDING" if usable else "SKIPPED"
        c["streams_status"] = "" if c["streams_supported"] else "UNSUPPORTED"
        c["streams_detail"] = ""
        c["subs_status"] = ""
    return rows


def _upg_set(keys, **fields) -> None:
    """Update result fields on the matching combination rows."""
    keys = {keys} if isinstance(keys, str) else set(keys)
    with _upg_lock:
        for c in _upg_state["combos"]:
            if c.get("key") in keys:
                c.update(fields)


def _upg_set_by_seller(seller_id: str, **fields) -> None:
    with _upg_lock:
        for c in _upg_state["combos"]:
            if str(c.get("selling_partner_id")) == str(seller_id):
                c.update(fields)


def _upg_build_payload(combos: list) -> tuple:
    """One scheduler row per (combination × report type)."""
    payload, skipped = [], []
    for c in combos:
        sid = c.get("selling_partner_id")
        region = c.get("region_name")
        marketplace = c.get("marketplace_name")
        if not region or not marketplace:
            skipped.append(str(sid))
            continue
        for rt in UPGRADER_REPORT_TYPES:
            payload.append({
                "sellerId": int(sid),
                "marketplaceName": marketplace,
                "reportType": rt,
                "regionName": region,
            })
    return payload, skipped


def _upg_ms_ensure(seller_id: str, marketplace_id: str, cache: dict) -> dict:
    """Check the seller's stream subscriptions and add whatever is missing."""
    found = _ms_fetch_profiles([seller_id], marketplace_id)
    info = found.get(str(seller_id))
    if not info:
        return {"ok": False, "error": "no advertising profile/token for this marketplace"}
    token = _ads_access_token(info["token"], cache)
    statuses = _ms_get_subscriptions(token, info["profile_id"])
    missing = [d for d in MS_DATASETS if statuses.get(d) != "ACTIVE"]
    added = []
    for ds in missing:
        _ms_subscribe(token, info["profile_id"], ds)
        added.append(ds)
    return {"ok": True, "added": added, "already": [d for d in MS_DATASETS if d not in missing]}


def _upg_worker(seller_ids: list, do_ms: bool, do_subs: bool,
                lookback: int, token: str) -> None:
    try:
        _upg_log("info", f"Upgrader started for {len(seller_ids)} seller(s) — "
                         f"Marketing Streams {'ON' if do_ms else 'OFF'}, "
                         f"Subscriptions {'ON' if do_subs else 'OFF'}")

        # ── 1. Advertising schedulers for every seller/region/marketplace ──
        try:
            combos = _upg_annotate(_upg_fetch_combos(seller_ids))
        except Exception as exc:
            _upg_log("error", f"Combination lookup failed — {exc}")
            with _upg_lock:
                _upg_state["counters"]["failed"] += 1
            return
        with _upg_lock:
            _upg_state["combos"] = combos
            _upg_state["counters"]["sellers"] = len({str(c["selling_partner_id"]) for c in combos})
            _upg_state["counters"]["combos"] = len(combos)
        if not combos:
            _upg_log("warn", "No advertising profiles found for these sellers — nothing to upgrade.")
            return
        _upg_log("info", f"{len(combos)} combination(s) found: " + "; ".join(
            f"{c['selling_partner_id']}/{c['region_name']}/{c['marketplace_name']}" for c in combos[:6]
        ) + ("…" if len(combos) > 6 else ""))

        # Mark what the toggles have switched off, so an empty cell never reads
        # as work still pending.
        if not do_ms:
            _upg_set([c["key"] for c in combos if c.get("streams_supported")], streams_status="OFF")
        if not do_subs:
            _upg_set([c["key"] for c in combos], subs_status="OFF")

        payload, skipped = _upg_build_payload(combos)
        if skipped:
            _upg_log("warn", f"Skipped {len(skipped)} combination(s) missing region/marketplace: "
                             f"{', '.join(sorted(set(skipped)))}")
        if payload:
            _upg_log("info", f"Sending {len(payload)} advertising scheduler row(s) "
                             f"({len(UPGRADER_REPORT_TYPES)} report types × {len(combos) - len(skipped)} combination(s))")
            sent_keys = [c["key"] for c in combos if c.get("sched_total")]
            try:
                resp = _subs_send(token, payload)
                if 200 <= resp.status_code < 300:
                    body = (resp.text or "").strip().replace("\n", " ")[:160]
                    _upg_log("ok", f"Advertising schedulers → {resp.status_code} {body}")
                    _upg_set(sent_keys, sched_sent=len(UPGRADER_REPORT_TYPES),
                             sched_status="SENT")
                    with _upg_lock:
                        _upg_state["counters"]["ad_rows"] = len(payload)
                else:
                    body = (resp.text or "").strip().replace("\n", " ")[:200]
                    _upg_log("error", f"Advertising schedulers → {resp.status_code} {body}")
                    _upg_set(sent_keys, sched_status="FAILED")
                    with _upg_lock:
                        _upg_state["counters"]["failed"] += 1
            except Exception as exc:
                _upg_log("error", f"Advertising schedulers failed — {exc}")
                _upg_set(sent_keys, sched_status="FAILED")
                with _upg_lock:
                    _upg_state["counters"]["failed"] += 1

        # ── 2. Subscriptions (analytics backfill schedulers) ──
        if do_subs and not _upg_stop_event.is_set():
            _upg_log("info", f"Subscriptions: building backfill payload (lookback {lookback} days)")
            try:
                subs_payload, counts = _subs_build_payload(seller_ids, lookback)
                for sid in {str(s) for s in seller_ids}:
                    if sid not in counts:
                        _upg_set_by_seller(sid, subs_status="NO ROWS")
                if not subs_payload:
                    _upg_log("warn", "Subscriptions: queries returned no rows — nothing to schedule.")
                else:
                    resp = _subs_send(token, subs_payload)
                    ok = 200 <= resp.status_code < 300
                    body = (resp.text or "").strip().replace("\n", " ")[:160]
                    _upg_log("ok" if ok else "error",
                             f"Subscriptions → {resp.status_code} {body}")
                    for sid in counts:
                        _upg_set_by_seller(sid, subs_status="SENT" if ok else "FAILED")
                    if ok:
                        with _upg_lock:
                            _upg_state["counters"]["subs"] = len(subs_payload)
                        # Track them in the same 48h logbook the Subscriptions card uses.
                        names = _ms_fetch_seller_info(seller_ids)
                        now = datetime.datetime.now().isoformat()
                        _subs_init()
                        with _subs_db() as conn:
                            for sid in {str(s) for s in seller_ids}:
                                if sid not in counts:
                                    continue
                                conn.execute(
                                    "INSERT INTO subscription_log (seller_id, name, status, "
                                    "added_at, updated_at, scheduler_rows, total, pending, "
                                    "seen_pending, error, payload) VALUES (?,?,?,?,?,?,0,0,0,NULL,?) "
                                    "ON CONFLICT(seller_id) DO UPDATE SET status=excluded.status, "
                                    "added_at=excluded.added_at, updated_at=excluded.updated_at, "
                                    "scheduler_rows=excluded.scheduler_rows, seen_pending=0, "
                                    "error=NULL, payload=excluded.payload",
                                    (sid, (names.get(sid) or {}).get("name", ""), "SENT", now, now,
                                     counts.get(sid, 0),
                                     json.dumps([p for p in subs_payload if str(p["sellerId"]) == sid])),
                                )
                        _upg_log("info", "Subscriptions logged — track them in the Subscriptions card.")
                    else:
                        with _upg_lock:
                            _upg_state["counters"]["failed"] += 1
            except Exception as exc:
                _upg_log("error", f"Subscriptions failed — {exc}")
                with _upg_lock:
                    _upg_state["counters"]["failed"] += 1

        # ── 3. Marketing Streams, only where the marketplace supports them ──
        if do_ms and not _upg_stop_event.is_set():
            supported = {v: k for k, v in MS_MARKETPLACES.items()}
            targets = [c for c in combos if c.get("marketplace_id") in supported]
            if not targets:
                _upg_log("warn", "Marketing Streams: none of these marketplaces support streams "
                                 f"(supported: {', '.join(MS_MARKETPLACES)}) — skipped.")
            else:
                _upg_log("info", f"Marketing Streams: {len(targets)} eligible seller/marketplace pair(s)")
                cache = {}
                for c in targets:
                    sid = str(c["selling_partner_id"])
                    mp_id, mp_name, key = c["marketplace_id"], c["marketplace_name"], c["key"]
                    if _upg_stop_event.is_set():
                        _upg_log("warn", "Stopped by user.")
                        break
                    _upg_set(key, streams_status="CHECKING")
                    try:
                        res = _upg_ms_ensure(sid, mp_id, cache)
                        if not res["ok"]:
                            _upg_log("error", f"MS {sid} ({mp_name}): {res['error']}")
                            _upg_set(key, streams_status="FAILED", streams_detail=res["error"])
                            with _upg_lock:
                                _upg_state["counters"]["failed"] += 1
                        elif res["added"]:
                            short = ", ".join(d.replace("sp-", "") for d in res["added"])
                            _upg_log("ok", f"MS {sid} ({mp_name}): added {', '.join(res['added'])}")
                            _upg_set(key, streams_status="ADDED", streams_detail=short)
                            with _upg_lock:
                                _upg_state["counters"]["ms_added"] += 1
                        else:
                            _upg_log("info", f"MS {sid} ({mp_name}): already active")
                            _upg_set(key, streams_status="ACTIVE", streams_detail="traffic, conversion")
                    except Exception as exc:
                        _upg_log("error", f"MS {sid} ({mp_name}): {exc}")
                        _upg_set(key, streams_status="FAILED", streams_detail=str(exc)[:120])
                        with _upg_lock:
                            _upg_state["counters"]["failed"] += 1
                    time.sleep(0.15)

        c = _upg_state["counters"]
        _upg_log("info", f"Finished. {c['ad_rows']} ad scheduler row(s), {c['subs']} subscription "
                         f"row(s), {c['ms_added']} stream(s) added, {c['failed']} failure(s).")
    finally:
        with _upg_lock:
            _upg_state["running"] = False


@app.route("/api/upg/preview", methods=["POST"])
def upg_preview():
    payload_in = request.get_json(silent=True) or {}
    seller_ids = _parse_seller_ids(payload_in.get("sellers") or "")
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    try:
        combos = _upg_fetch_combos(seller_ids)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    payload, skipped = _upg_build_payload(combos)
    missing = [s for s in seller_ids
               if s not in {str(c["selling_partner_id"]) for c in combos}]
    return jsonify({
        "ok": True, "combos": combos, "skipped": skipped, "no_profile": missing,
        "report_types": len(UPGRADER_REPORT_TYPES),
        "row_count": len(payload), "sample": payload[:6],
    })


@app.route("/api/upg/start", methods=["POST"])
def upg_start():
    with _upg_lock:
        if _upg_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409
    payload_in = request.get_json(silent=True) or {}
    seller_ids = _parse_seller_ids(payload_in.get("sellers") or "")
    do_ms = bool(payload_in.get("marketing_streams"))
    do_subs = bool(payload_in.get("subscriptions"))
    lookback = int(payload_in.get("lookback_days") or SUBS_LOOKBACK_DAYS)
    if not seller_ids:
        return jsonify({"ok": False, "error": "No seller IDs provided."}), 400
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400
    if do_ms and not ADS_CLIENT_ID:
        return jsonify({"ok": False, "error": (
            "Marketing Streams is on but ADS_CLIENT_ID is not set in .env."
        )}), 400
    try:
        token = REPORTS_ACCESS_TOKEN if REPORTS_ACCESS_TOKEN else reports_api_login()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    _upg_stop_event.clear()
    with _upg_lock:
        _upg_state["logs"] = []
        _upg_state["combos"] = []
        _upg_state["counters"] = {"sellers": 0, "combos": 0, "ad_rows": 0,
                                  "ms_added": 0, "subs": 0, "failed": 0}
        _upg_state["running"] = True
    threading.Thread(target=_upg_worker,
                     args=(seller_ids, do_ms, do_subs, lookback, token), daemon=True).start()
    return jsonify({"ok": True, "count": len(seller_ids)})


@app.route("/api/upg/stop", methods=["POST"])
def upg_stop():
    _upg_stop_event.set()
    _upg_log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.route("/api/upg/state")
def upg_state():
    since = request.args.get("since", default=0, type=int)
    with _upg_lock:
        return jsonify({
            "running": _upg_state["running"],
            "counters": _upg_state["counters"],
            "combos": _upg_state["combos"],
            "logs": [e for e in _upg_state["logs"] if e["i"] >= since],
            "next": len(_upg_state["logs"]),
        })


# ══════════════════════════════════════════════════════════════════════════════
# Disable Fetching for Inactive Users (SPB-629) — shown in the UI under that
# name; the code keeps the shorter `leak` prefix. Finds Stripe-canceled accounts
# that are still consuming paid Amazon data. Produces review lists and ready-to-
# run SQL only; nothing is executed.
#
# Stripe subscription status does not exist in the database (stripe_detail holds
# only id/customer_id/trial_started, and pricing_plan_id is the sold tier, not
# proof of payment), so this has to join two sources: the DB says who is still
# consuming, Stripe says who is still paying.
# ══════════════════════════════════════════════════════════════════════════════

STRIPE_API = "https://api.stripe.com/v1/subscriptions"
STRIPE_KEY_FILE = os.environ.get(
    "STRIPE_KEY_FILE", os.path.expanduser("~/.stripe_key"))

# Any of these means the customer is still worth money — leave them alone.
# past_due and unpaid are deliberately here: they often recover on retry, and
# cutting off a customer who then pays is the expensive mistake.
LEAK_LIVE_STATUSES = {"active", "trialing", "past_due", "unpaid",
                      "incomplete", "incomplete_expired", "paused"}

LEAK_PLAN_NAMES = {"1": "basic", "2": "advanced", "3": "custom",
                   "2004": "custom_ads", "2005": "custom_ads_sqp"}

LEAK_SQP_TYPES = ("SQP_BY_ASIN", "SQP_BY_ASIN_CONVERT", "SQP_BY_ASIN_GENERATE")

_LEAK_MAPPING_QUERY = """
    select u.id as user_id, u.username, u.user_type, u.pricing_plan_id,
           sd.customer_id as stripe_customer, sd.trial_started,
           u.registration_date::date as registered,
           s.id as sp_id, s.selling_partner_id, s.name as seller_name,
           s.state as region,
           coalesce(string_agg(distinct ap.account_info_id, ','), '') as merchant_tokens,
           coalesce(string_agg(distinct ap.account_info_marketplace_id, ','), '') as ad_marketplaces
    from my_real_profit_user u
    join stripe_detail sd on sd.id = u.stripe_detail_id
    join amazon_selling_partner s on s.my_real_profit_user_id = u.id
    left join advertising_profile ap on ap.amazon_selling_partner_id = s.id
    group by 1,2,3,4,5,6,7,8,9,10,11
    order by 1,8
"""

_LEAK_SQP_SCHED_QUERY = """
    select amazon_selling_partner_id, type, marketplace_id, is_enabled
    from scheduler_config where type like 'SQP%' order by 1,2,3
"""

_LEAK_TRAFFIC_QUERY = """
    select advertiser_id, count(*) n, max(created_at) last_ingest
    from sp_traffic where created_at >= now() - interval '%(days)s days'
    group by 1 order by 2 desc
"""

_LEAK_SQP_INGEST_QUERY = """
    select amazon_selling_partner_id, count(*) n, max(created_at) last_ingest,
           max(start_date) last_period
    from sqp_by_asin where created_at >= now() - interval '%(days)s days'
    group by 1 order by 2 desc
"""

LEAK_TRAFFIC_DAYS = int(os.environ.get("LEAK_TRAFFIC_DAYS", "7"))
LEAK_SQP_DAYS = int(os.environ.get("LEAK_SQP_DAYS", "14"))
# "Protection window" in the UI. Accounts canceled within this many days are
# shown but held back from action, so someone who just canceled and may
# reconsider is not cut off immediately.
LEAK_GRACE_DAYS = int(os.environ.get("LEAK_GRACE_DAYS", "14"))
# Unverified: /admin/scheduler/add exists, this is the presumed counterpart.
LEAK_REMOVE_PATH = os.environ.get("LEAK_REMOVE_PATH", "/admin/scheduler/remove")

_leak_lock = threading.Lock()
_leak_stop_event = threading.Event()
_leak_state = {
    "running": False,
    "logs": [],
    "started_at": 0.0,
    "finished_at": "",
    "counters": {"customers": 0, "canceled": 0, "live": 0, "mapping_rows": 0,
                 "stream": 0, "sqp": 0, "no_sub": 0},
    "lists": {"stream": [], "sqp": [], "no_sub": []},
    "warnings": [],
}


def _leak_log(level: str, msg: str) -> None:
    with _leak_lock:
        _leak_state["logs"].append({
            "i": len(_leak_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })


def _leak_warn(msg: str) -> None:
    with _leak_lock:
        _leak_state["warnings"].append(msg)
    _leak_log("warn", msg)


def _leak_stripe_key() -> str:
    """Read the restricted Stripe key. Never logged, never put in argv."""
    key = (os.environ.get("STRIPE_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(STRIPE_KEY_FILE) as fh:
            key = fh.read().strip()
    except OSError:
        key = ""
    if not key:
        raise ValueError(
            f"No Stripe key. Set STRIPE_API_KEY or create {STRIPE_KEY_FILE} "
            "(chmod 600) holding the restricted read-only key."
        )
    return key


def _leak_scrub(text: str, key: str) -> str:
    """Belt and braces: never let a key reach a log line."""
    out = text or ""
    if key and key in out:
        out = out.replace(key, "***")
    return out


def _leak_ts(value):
    """Stripe epoch seconds → ISO date-time, or '' when absent."""
    if not value:
        return ""
    try:
        return datetime.datetime.utcfromtimestamp(int(value)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def _leak_period_end(sub: dict):
    """current_period_end moved to the item level in recent API versions."""
    val = sub.get("current_period_end")
    if val:
        return val
    for item in ((sub.get("items") or {}).get("data") or []):
        if item.get("current_period_end"):
            return item["current_period_end"]
    return None


def _leak_stripe_preflight() -> None:
    """One cheap call to prove the key works before the slow DB scans.

    Without this, a bad or wrongly-scoped key only fails after several minutes
    of scanning sp_traffic and sqp_by_asin.
    """
    key = _leak_stripe_key()
    resp = http_requests.get(STRIPE_API, params={"status": "all", "limit": 1},
                             auth=(key, ""), timeout=30)
    if resp.status_code == 401:
        raise ValueError("Stripe rejected the key (401). Check ~/.stripe_key holds a "
                         "live restricted key and that it has not been revoked.")
    if resp.status_code == 403:
        raise ValueError("Stripe returned 403 — the key lacks read access to "
                         "Subscriptions. Give it Customers: read and Subscriptions: read.")
    if resp.status_code != 200:
        raise ValueError(f"Stripe {resp.status_code}: "
                         f"{_leak_scrub(resp.text, key)[:200]}")
    _leak_log("ok", "Stripe key accepted.")


def _leak_fetch_stripe() -> list:
    """Every subscription, all statuses, via cursor pagination."""
    key = _leak_stripe_key()
    subs, starting_after, page = [], None, 0
    while True:
        if _leak_stop_event.is_set():
            raise ValueError("stopped by user")
        params = {"status": "all", "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        # auth= puts the key in an Authorization header, never in argv.
        resp = http_requests.get(STRIPE_API, params=params, auth=(key, ""), timeout=60)
        if resp.status_code != 200:
            raise ValueError(
                f"Stripe {resp.status_code}: {_leak_scrub(resp.text, key)[:300]}")
        body = resp.json()
        data = body.get("data") or []
        for s in data:
            subs.append({
                "customer_id": s.get("customer") or "",
                "subscription_id": s.get("id") or "",
                "status": (s.get("status") or "").lower(),
                "cancel_at_period_end": bool(s.get("cancel_at_period_end")),
                "created": _leak_ts(s.get("created")),
                "canceled_at": _leak_ts(s.get("canceled_at")),
                "ended_at": _leak_ts(s.get("ended_at")),
                "current_period_end": _leak_ts(_leak_period_end(s)),
                "price_ids": ",".join(
                    i.get("price", {}).get("id", "")
                    for i in ((s.get("items") or {}).get("data") or [])
                ),
            })
        page += 1
        _leak_log("info", f"Stripe page {page}: {len(subs)} subscription(s) so far")
        if not body.get("has_more") or not data:
            break
        starting_after = data[-1].get("id")
        if not starting_after:
            break
    return subs


def _leak_query(cur, sql, label, params=None):
    _leak_log("info", f"Querying {label}…")
    cur.execute(sql if params is None else sql % params)
    rows = [dict(r) for r in cur.fetchall()]
    _leak_log("ok", f"{label}: {len(rows):,} row(s)")
    return rows


def _leak_db_side() -> dict:
    """The four DB exports the cross-check needs."""
    out = {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # The two big scans need more than the default timeout; both are
            # bounded on created_at (an unbounded group-by on sp_traffic times out).
            cur.execute("SET statement_timeout = '20min'")
            out["mapping"] = _leak_query(cur, _LEAK_MAPPING_QUERY, "identity mapping")
            out["sched"] = _leak_query(cur, _LEAK_SQP_SCHED_QUERY, "SQP scheduler config")
            _leak_log("info", f"Scanning sp_traffic ({LEAK_TRAFFIC_DAYS}d) — this takes a few minutes…")
            out["traffic"] = _leak_query(cur, _LEAK_TRAFFIC_QUERY,
                                         f"sp_traffic last {LEAK_TRAFFIC_DAYS}d",
                                         {"days": LEAK_TRAFFIC_DAYS})
            _leak_log("info", f"Scanning sqp_by_asin ({LEAK_SQP_DAYS}d)…")
            out["sqp"] = _leak_query(cur, _LEAK_SQP_INGEST_QUERY,
                                     f"sqp_by_asin last {LEAK_SQP_DAYS}d",
                                     {"days": LEAK_SQP_DAYS})
    return out


def _leak_classify(subs: list) -> tuple:
    """Per customer: live (any live status) / canceled (all canceled) / neither."""
    by_customer = {}
    for s in subs:
        by_customer.setdefault(s["customer_id"], []).append(s)

    canceled, live = {}, set()
    for cust, rows in by_customer.items():
        statuses = {r["status"] for r in rows}
        if statuses & LEAK_LIVE_STATUSES:
            live.add(cust)
        elif statuses == {"canceled"}:
            stamps = [r["canceled_at"] or r["ended_at"] for r in rows]
            canceled[cust] = max([x for x in stamps if x], default="")
    return by_customer, canceled, live


def _leak_days_since(stamp: str):
    """Whole days since an ISO timestamp, or None when it is absent."""
    if not stamp:
        return None
    try:
        dt = datetime.datetime.strptime(stamp[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (datetime.datetime.now() - dt).days


def _leak_grace(row: dict, grace_days: int) -> dict:
    """Tag a row with how long ago it was canceled and whether it is protected.

    `in_grace` is what the UI shows as PROTECTED.
    """
    days = _leak_days_since(row.get("canceled_at", ""))
    row["canceled_days"] = days
    # An unknown cancellation date is treated as in-grace: better to hold an
    # account back than to cut one whose timing we cannot establish.
    row["in_grace"] = True if days is None else days < grace_days
    return row


def _leak_history_init() -> None:
    with _subs_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leak_runs (
                run_at TEXT PRIMARY KEY, mapping_rows INTEGER, customers INTEGER,
                canceled INTEGER, stream INTEGER, sqp INTEGER, no_sub INTEGER
            )
        """)


def _leak_last_mapping_rows():
    try:
        _leak_history_init()
        with _subs_db() as conn:
            row = conn.execute(
                "SELECT mapping_rows FROM leak_runs ORDER BY run_at DESC LIMIT 1"
            ).fetchone()
        return row["mapping_rows"] if row else None
    except Exception:
        return None


def _leak_worker() -> None:
    try:
        _leak_log("info", "Disable Fetching for Inactive Users — started (read-only; nothing is executed).")

        # Check Stripe first: it costs one request, and the DB scans that follow
        # take minutes that are wasted if the key turns out to be unusable.
        try:
            _leak_stripe_preflight()
        except Exception as exc:
            _leak_log("error", f"Stripe check failed — {exc}")
            return
        if _leak_stop_event.is_set():
            _leak_log("warn", "Stopped by user.")
            return

        # ── DB side ──────────────────────────────────────────────────────────
        try:
            db = _leak_db_side()
        except Exception as exc:
            _leak_log("error", f"Database step failed — {exc}")
            return
        if _leak_stop_event.is_set():
            _leak_log("warn", "Stopped by user.")
            return

        mapping = db["mapping"]
        previous = _leak_last_mapping_rows()
        if previous:
            swing = abs(len(mapping) - previous) / max(previous, 1) * 100
            if swing > 10:
                _leak_warn(f"Identity mapping changed {swing:.1f}% since the last run "
                           f"({previous:,} → {len(mapping):,}) — check the join before acting.")

        # ── Stripe side ──────────────────────────────────────────────────────
        try:
            subs = _leak_fetch_stripe()
        except Exception as exc:
            _leak_log("error", f"Stripe step failed — {exc}")
            return
        if _leak_stop_event.is_set():
            _leak_log("warn", "Stopped by user.")
            return

        by_customer, canceled, live = _leak_classify(subs)
        _leak_log("ok", f"Stripe: {len(subs):,} subscription(s) across {len(by_customer):,} customer(s)")

        # Guardrail: Stripe defaults to active-only. Zero canceled rows among a
        # non-empty result means status=all was lost, and every list would be
        # silently empty.
        if subs and not any(s["status"] == "canceled" for s in subs):
            _leak_log("error", "No canceled subscriptions in the Stripe response at all — "
                               "the status=all filter was not applied. Aborting rather than "
                               "reporting empty lists.")
            return

        # ── Cross-check ──────────────────────────────────────────────────────
        streaming = {r["advertiser_id"]: r for r in db["traffic"] if r.get("advertiser_id")}
        sqp_ingest = {str(r["amazon_selling_partner_id"]): r for r in db["sqp"]
                      if r.get("amazon_selling_partner_id") is not None}
        sqp_enabled = {str(r["amazon_selling_partner_id"]) for r in db["sched"]
                       if r.get("is_enabled") is True or r.get("is_enabled") == "t"}

        stream_hits, sqp_hits, no_sub = [], [], []
        for m in mapping:
            cust = m.get("stripe_customer") or ""
            sp_id = str(m.get("sp_id"))
            plan = str(m.get("pricing_plan_id") or "")
            base = {
                "user_id": m.get("user_id"), "username": m.get("username") or "",
                "stripe_customer": cust, "sp_id": m.get("sp_id"),
                "selling_partner_id": m.get("selling_partner_id") or "",
                "seller_name": m.get("seller_name") or "",
                "region": m.get("region") or "",
                "pricing_plan_id": plan,
                "plan_name": LEAK_PLAN_NAMES.get(plan, plan),
            }

            if cust not in by_customer:
                row = dict(base)
                row["trial_started"] = str(m.get("trial_started") or "")
                row["registered"] = str(m.get("registered") or "")
                no_sub.append(row)
                continue
            if cust not in canceled:
                continue   # live, or a status we deliberately leave alone

            tokens = [t for t in (m.get("merchant_tokens") or "").split(",") if t]
            hot = [t for t in tokens if t in streaming]
            if hot:
                row = dict(base)
                row["canceled_at"] = canceled[cust]
                row["merchant_tokens_streaming"] = ",".join(hot)
                row["ad_marketplaces"] = m.get("ad_marketplaces") or ""
                row["stream_rows_7d"] = sum(int(streaming[t]["n"]) for t in hot)
                row["stream_last_ingest"] = max(str(streaming[t]["last_ingest"]) for t in hot)
                stream_hits.append(row)

            if sp_id in sqp_ingest or sp_id in sqp_enabled:
                row = dict(base)
                row["canceled_at"] = canceled[cust]
                ing = sqp_ingest.get(sp_id)
                row["sqp_scheduler_enabled"] = "yes" if sp_id in sqp_enabled else "no"
                row["sqp_rows_14d"] = int(ing["n"]) if ing else 0
                row["sqp_last_ingest"] = str(ing["last_ingest"]) if ing else ""
                row["sqp_last_period"] = str(ing["last_period"]) if ing else ""
                sqp_hits.append(row)

        stream_hits.sort(key=lambda r: -r["stream_rows_7d"])
        sqp_hits.sort(key=lambda r: -r["sqp_rows_14d"])
        grace_days = _leak_setting_int("grace_days", LEAK_GRACE_DAYS)
        for r in stream_hits:
            _leak_grace(r, grace_days)
        for r in sqp_hits:
            _leak_grace(r, grace_days)
        held = sum(1 for r in sqp_hits if r["in_grace"]) + \
               sum(1 for r in stream_hits if r["in_grace"])

        with _leak_lock:
            _leak_state["lists"] = {"stream": stream_hits, "sqp": sqp_hits, "no_sub": no_sub}
            _leak_state["counters"] = {
                "customers": len(by_customer), "canceled": len(canceled),
                "live": len(live), "mapping_rows": len(mapping),
                "stream": len(stream_hits), "sqp": len(sqp_hits), "no_sub": len(no_sub),
                "held_in_grace": held, "grace_days": grace_days,
            }
            _leak_state["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        _leak_save_results()

        try:
            _leak_history_init()
            with _subs_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO leak_runs VALUES (?,?,?,?,?,?,?)",
                    (datetime.datetime.now().isoformat(), len(mapping), len(by_customer),
                     len(canceled), len(stream_hits), len(sqp_hits), len(no_sub)))
        except Exception as exc:
            _leak_log("warn", f"Could not record run history — {exc}")

        _leak_log("ok", f"A) canceled + Marketing Stream still arriving: {len(stream_hits):,} seller row(s)")
        _leak_log("ok", f"B) canceled + SQP enabled or arriving:         {len(sqp_hits):,} seller row(s)")
        _leak_log("info", f"Not actioned — no Stripe subscription at all: {len(no_sub):,} seller row(s)")
        _leak_log("info", "Marketing Stream rows name the account but not the SNS subscription: "
                          "the link table keys on advertising ENTITY ids, which do not join to the "
                          "merchant tokens we hold. Resolving that needs the Ads API or AWS console.")
        _leak_log("info", "Review the lists, then hand the SQP disable statements to a backend dev. "
                          "Nothing here writes to the database.")
    finally:
        with _leak_lock:
            _leak_state["running"] = False


def _leak_disable_sql(rows: list) -> str:
    ids = sorted({str(r["sp_id"]) for r in rows if r.get("sqp_scheduler_enabled") == "yes"})
    if not ids:
        return "-- No selected seller has an enabled SQP scheduler."
    types = ", ".join(f"'{t}'" for t in LEAK_SQP_TYPES)
    lines = ["-- SPB-629: disable SQP for Stripe-canceled accounts.",
             "-- REVIEW FIRST. All three SQP types move together.",
             "-- Requires write access; this dashboard is read-only.",
             ""]
    for sp in ids:
        lines.append("update scheduler_config set is_enabled = false")
        lines.append(f"where amazon_selling_partner_id = {sp}")
        lines.append(f"  and type in ({types});")
    return "\n".join(lines)


@app.route("/api/leak/start", methods=["POST"])
def leak_start():
    with _leak_lock:
        if _leak_state["running"]:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409
    if not DB_CONFIG["password"]:
        return jsonify({"ok": False, "error": "DB_PASSWORD is not set in .env."}), 400
    try:
        _leak_stripe_key()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    _leak_stop_event.clear()
    with _leak_lock:
        _leak_state["logs"] = []
        _leak_state["warnings"] = []
        # Clear previous results: a run that aborts must not leave last week's
        # lists on screen where they could be read as current.
        _leak_state["lists"] = {"stream": [], "sqp": [], "no_sub": []}
        _leak_state["counters"] = {"customers": 0, "canceled": 0, "live": 0,
                                   "mapping_rows": 0, "stream": 0, "sqp": 0, "no_sub": 0}
        _leak_state["finished_at"] = ""
        _leak_state["started_at"] = time.time()
        _leak_state["running"] = True
    threading.Thread(target=_leak_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/leak/stop", methods=["POST"])
def leak_stop():
    _leak_stop_event.set()
    _leak_log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.route("/api/leak/state")
def leak_state():
    since = request.args.get("since", default=0, type=int)
    with _leak_lock:
        return jsonify({
            "running": _leak_state["running"],
            "counters": _leak_state["counters"],
            "warnings": _leak_state["warnings"],
            "finished_at": _leak_state["finished_at"],
            "elapsed": int(time.time() - _leak_state["started_at"])
                       if _leak_state["running"] and _leak_state["started_at"] else 0,
            "logs": [e for e in _leak_state["logs"] if e["i"] >= since],
            "next": len(_leak_state["logs"]),
            "lists": _leak_state["lists"],
            "traffic_days": LEAK_TRAFFIC_DAYS,
            "sqp_days": LEAK_SQP_DAYS,
            "grace_days": _leak_setting_int("grace_days", LEAK_GRACE_DAYS),
            "include_trials": _leak_setting("include_trials", "0") == "1",
            "auto_enabled": _leak_setting("auto_enabled", "0") == "1",
            "remove_path": LEAK_REMOVE_PATH,
        })


def _leak_settings_init() -> None:
    with _subs_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS leak_settings "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS leak_results "
                     "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, finished_at TEXT)")


def _leak_setting(key: str, default: str = "") -> str:
    try:
        _leak_settings_init()
        with _subs_db() as conn:
            row = conn.execute("SELECT value FROM leak_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def _leak_setting_int(key: str, default: int) -> int:
    try:
        return int(_leak_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def _leak_set_setting(key: str, value) -> None:
    _leak_settings_init()
    with _subs_db() as conn:
        conn.execute("INSERT OR REPLACE INTO leak_settings VALUES (?,?)", (key, str(value)))


def _leak_save_results() -> None:
    """Persist the last run so the card shows findings without re-scanning."""
    try:
        _leak_settings_init()
        with _leak_lock:
            payload = json.dumps({"lists": _leak_state["lists"],
                                  "counters": _leak_state["counters"],
                                  "warnings": _leak_state["warnings"]})
            finished = _leak_state["finished_at"]
        with _subs_db() as conn:
            conn.execute("INSERT OR REPLACE INTO leak_results VALUES (1,?,?)",
                         (payload, finished))
    except Exception as exc:
        _leak_log("warn", f"Could not save results — {exc}")


def _leak_load_results() -> None:
    try:
        _leak_settings_init()
        with _subs_db() as conn:
            row = conn.execute("SELECT payload, finished_at FROM leak_results WHERE id=1").fetchone()
        if not row:
            return
        data = json.loads(row["payload"])
        with _leak_lock:
            _leak_state["lists"] = data.get("lists", _leak_state["lists"])
            _leak_state["counters"] = data.get("counters", _leak_state["counters"])
            _leak_state["warnings"] = data.get("warnings", [])
            _leak_state["finished_at"] = row["finished_at"] or ""
    except Exception:
        pass


def _leak_selected_rows(sp_ids: list, include_trials: bool) -> list:
    """Resolve selected seller ids to rows that are allowed to be actioned."""
    wanted = {str(s) for s in sp_ids}
    with _leak_lock:
        pool = list(_leak_state["lists"]["sqp"])
        if include_trials:
            pool += list(_leak_state["lists"]["no_sub"])
    out, blocked = [], []
    for r in pool:
        if str(r.get("sp_id")) not in wanted:
            continue
        # no_sub rows have no cancellation date and are only actionable when the
        # trials toggle is on; canceled rows must be past the protection window.
        if r.get("in_grace") and r.get("canceled_at"):
            blocked.append(r)
            continue
        out.append(r)
    return out, blocked


_LEAK_SCHED_COUNT_QUERY = """
    select amazon_selling_partner_id as sp_id, count(*) as n
    from scheduler_config
    where amazon_selling_partner_id = ANY (%(ids)s)
      and type like 'SQP%%'
    group by 1
"""


def _leak_sched_counts(sp_ids: list) -> dict:
    """SQP scheduler_config rows per seller — used to prove a removal landed."""
    ids = []
    for s in sp_ids:
        try:
            ids.append(int(s))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_LEAK_SCHED_COUNT_QUERY, {"ids": ids})
            return {str(r["sp_id"]): int(r["n"]) for r in cur.fetchall()}


def _leak_selection_is_trials(sp_ids: list) -> bool:
    """True when the selection only matches rows in the trials bucket."""
    wanted = {str(s) for s in sp_ids}
    with _leak_lock:
        return any(str(r.get("sp_id")) in wanted for r in _leak_state["lists"]["no_sub"])


def _leak_remove_via_api(token: str, sp_id, method: str = "POST"):
    """Call the presumed scheduler-remove endpoint for one seller.

    The payload mirrors /admin/scheduler/add, which takes a JSON array of
    scheduler rows. The shape is unverified — this is what the probe is for.
    """
    body = [{"sellerId": int(sp_id), "reportType": t} for t in LEAK_SQP_TYPES]
    url = f"{REPORTS_PROD_HOST}{LEAK_REMOVE_PATH}"
    kwargs = {"headers": {"Authorization": f"Bearer {token}",
                          "Content-Type": "application/json"},
              "json": body, "timeout": 60}
    fn = http_requests.delete if method.upper() == "DELETE" else http_requests.post
    resp = fn(url, **kwargs)
    return url, body, resp


@app.route("/api/leak/settings", methods=["GET", "POST"])
def leak_settings_route():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        for key in ("grace_days", "auto_enabled", "auto_weekday", "include_trials"):
            if key in payload:
                _leak_set_setting(key, payload[key])
        return jsonify({"ok": True})
    return jsonify({
        "ok": True,
        "grace_days": _leak_setting_int("grace_days", LEAK_GRACE_DAYS),
        "auto_enabled": _leak_setting("auto_enabled", "0") == "1",
        "auto_weekday": _leak_setting_int("auto_weekday", 0),   # 0 = Monday
        "include_trials": _leak_setting("include_trials", "0") == "1",
        "remove_path": LEAK_REMOVE_PATH,
        "last_auto_run": _leak_setting("last_auto_run", ""),
    })


@app.route("/api/leak/remove-test", methods=["POST"])
def leak_remove_test():
    """Probe the scheduler-remove endpoint against ONE seller, chosen by the user.

    This is a real call against production — it is deliberately one seller at a
    time so the shape can be confirmed before anything is done in bulk.
    """
    payload = request.get_json(silent=True) or {}
    sp_id = str(payload.get("sp_id", "")).strip()
    method = (payload.get("method") or "POST").upper()
    if not sp_id.isdigit():
        return jsonify({"ok": False, "error": "A numeric seller id is required."}), 400
    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400
    try:
        token = REPORTS_ACCESS_TOKEN if REPORTS_ACCESS_TOKEN else reports_api_login()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    try:
        before = _leak_sched_counts([sp_id]).get(str(sp_id), 0)
    except Exception:
        before = None

    try:
        url, body, resp = _leak_remove_via_api(token, sp_id, method)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    text = (resp.text or "")[:800]
    hint = ""
    if resp.status_code == 404:
        hint = ("404 — no endpoint at this path. The remove route either does not "
                "exist or lives elsewhere; fall back to generating SQL.")
    elif resp.status_code == 405:
        hint = f"405 — the path exists but does not accept {method}. Try the other method."
    elif resp.status_code in (400, 422):
        hint = ("The endpoint exists but rejected this body shape. The response "
                "should say which fields it wants.")

    # A 2xx is not proof. Count the seller's SQP scheduler rows before and after.
    after = None
    if 200 <= resp.status_code < 300:
        try:
            after = _leak_sched_counts([sp_id]).get(str(sp_id), 0)
        except Exception:
            after = None
        if before is None or after is None:
            hint = "Accepted, but scheduler_config could not be read to confirm it."
        elif before and not after:
            hint = (f"Confirmed: {before} SQP scheduler_config row(s) deleted for this "
                    "seller. amazon_report_info is untouched.")
        elif not before:
            hint = "Accepted, but this seller had no SQP scheduler rows to begin with."
        elif after == before:
            hint = (f"Accepted ({resp.status_code}) but the {before} scheduler_config "
                    "row(s) are still there — nothing was actually removed. This is the "
                    "case the backend team warned about, where it depends on when the "
                    "seller was added.")
        else:
            hint = f"Partially removed: {before} row(s) before, {after} after."

    return jsonify({"ok": True, "status": resp.status_code, "method": method,
                    "url": url, "sent": body, "response": text, "hint": hint,
                    "rows_before": before, "rows_after": after})


@app.route("/api/leak/disable", methods=["POST"])
def leak_disable():
    """Disable SQP for the selected sellers, or emit the SQL for review."""
    payload = request.get_json(silent=True) or {}
    sp_ids = payload.get("sp_ids") or []
    mode = (payload.get("mode") or "sql").lower()
    method = (payload.get("method") or "POST").upper()
    include_trials = bool(payload.get("include_trials"))
    if not sp_ids:
        return jsonify({"ok": False, "error": "Nothing selected."}), 400

    rows, blocked = _leak_selected_rows(sp_ids, include_trials)
    if not rows:
        if blocked:
            reason = (f"All {len(blocked)} selected account(s) are still protected "
                      "(canceled too recently) and cannot be actioned yet.")
        elif not include_trials and _leak_selection_is_trials(sp_ids):
            reason = ("The selection is trial accounts with no Stripe subscription. "
                      "Turn on 'Trials actionable' to include them.")
        else:
            reason = "Selected sellers are not in the current results — re-run the check."
        return jsonify({"ok": False, "error": reason}), 400

    if mode == "sql":
        return jsonify({"ok": True, "mode": "sql", "sql": _leak_disable_sql(rows),
                        "count": len(rows), "blocked": len(blocked)})

    if not REPORTS_PROD_HOST:
        return jsonify({"ok": False, "error": "PROD_HOST is not set in .env."}), 400
    try:
        token = REPORTS_ACCESS_TOKEN if REPORTS_ACCESS_TOKEN else reports_api_login()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Login failed: {exc}"}), 502

    # The endpoint deletes scheduler_config rows, and the backend team flagged
    # that whether it matches depends on when the seller was added. A 2xx is
    # therefore not proof: count the rows before and after and report the truth.
    targets = [str(r.get("sp_id")) for r in rows]
    try:
        before = _leak_sched_counts(targets)
    except Exception:
        before = {}

    results, ok_n, fail_n = [], 0, 0
    for r in rows:
        sp_id = r.get("sp_id")
        try:
            _, _, resp = _leak_remove_via_api(token, sp_id, method)
            good = 200 <= resp.status_code < 300
            results.append({"sp_id": sp_id, "seller_name": r.get("seller_name", ""),
                            "status": resp.status_code,
                            "detail": (resp.text or "").strip()[:160]})
            ok_n += 1 if good else 0
            fail_n += 0 if good else 1
        except Exception as exc:
            results.append({"sp_id": sp_id, "seller_name": r.get("seller_name", ""),
                            "status": 0, "detail": str(exc)[:160]})
            fail_n += 1
        time.sleep(0.1)

    try:
        after = _leak_sched_counts(targets)
        verified = 0
        for item in results:
            sp = str(item["sp_id"])
            b, a = before.get(sp, 0), after.get(sp, 0)
            item["rows_before"], item["rows_after"] = b, a
            if b and not a:
                item["verified"] = "REMOVED"
                verified += 1
            elif a and a < b:
                item["verified"] = "PARTIAL"
            elif a:
                item["verified"] = "STILL PRESENT"
            else:
                item["verified"] = "NOTHING TO REMOVE"
        verify_note = (f"{verified}/{len(results)} confirmed gone from scheduler_config. "
                       "amazon_report_info is untouched, so reports already queued can "
                       "still land.")
    except Exception as exc:
        verify_note = f"Could not verify against scheduler_config — {exc}"

    return jsonify({"ok": True, "mode": "api", "disabled": ok_n, "failed": fail_n,
                    "blocked": len(blocked), "results": results,
                    "verify_note": verify_note})


# ── Weekly auto-run ───────────────────────────────────────────────────────────

def _leak_scheduler_loop() -> None:
    """Run the check once a week on the configured weekday.

    Only fires while the dashboard is running; a machine that is off on Monday
    simply picks it up the next time the dashboard is open that day.
    """
    while True:
        try:
            time.sleep(300)
            if _leak_setting("auto_enabled", "0") != "1":
                continue
            with _leak_lock:
                if _leak_state["running"]:
                    continue
            now = datetime.datetime.now()
            if now.weekday() != _leak_setting_int("auto_weekday", 0):
                continue
            if _leak_setting("last_auto_run", "")[:10] == now.strftime("%Y-%m-%d"):
                continue      # already ran today
            _leak_set_setting("last_auto_run", now.isoformat())
            _leak_log("info", "Weekly auto-run starting.")
            _leak_stop_event.clear()
            with _leak_lock:
                _leak_state["logs"] = []
                _leak_state["started_at"] = time.time()
                _leak_state["running"] = True
            _leak_worker()
        except Exception:
            continue


def _leak_start_scheduler() -> None:
    threading.Thread(target=_leak_scheduler_loop, daemon=True).start()


@app.route("/api/leak/sql")
def leak_sql():
    with _leak_lock:
        rows = list(_leak_state["lists"]["sqp"])
    # Only offer statements for accounts that are actually actionable.
    rows = [r for r in rows if not r.get("in_grace")]
    return jsonify({"ok": True, "sql": _leak_disable_sql(rows)})


@app.route("/api/leak/export.csv")
def leak_export_csv():
    which = (request.args.get("list") or "").strip()
    columns = {
        "stream": ["user_id", "username", "stripe_customer", "canceled_at", "sp_id",
                   "selling_partner_id", "seller_name", "region", "pricing_plan_id",
                   "plan_name", "merchant_tokens_streaming", "ad_marketplaces",
                   "stream_rows_7d", "stream_last_ingest"],
        "sqp": ["user_id", "username", "stripe_customer", "canceled_at", "sp_id",
                "selling_partner_id", "seller_name", "region", "pricing_plan_id",
                "plan_name", "sqp_scheduler_enabled", "sqp_rows_14d",
                "sqp_last_ingest", "sqp_last_period"],
        "no_sub": ["user_id", "username", "stripe_customer", "trial_started",
                   "registered", "sp_id", "selling_partner_id", "seller_name",
                   "region", "pricing_plan_id", "plan_name"],
    }
    if which not in columns:
        return Response("error,unknown list\n", mimetype="text/csv", status=400)

    import csv
    import io
    with _leak_lock:
        rows = list(_leak_state["lists"][which])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns[which], extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    stamp = datetime.date.today().isoformat()
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="spb629_{which}_{stamp}.csv"'})


def _config_summary() -> None:
    """Print which card is configured, so missing .env values surface at boot."""
    def state(*names):
        missing = [n for n in names if not os.environ.get(n)]
        return "ok" if not missing else "MISSING " + ", ".join(missing)

    print("Config:")
    print(f"  Cancel Stuck Reports  : {state('DB_PASSWORD', 'LWA_CLIENT_ID', 'LWA_CLIENT_SECRET')}")
    print(f"  Global SQP Status     : {state('DB_PASSWORD')}")
    print(f"  Batch Reports PUT     : {state('PROD_HOST')}"
          f"{'' if os.environ.get('ACCESS_TOKEN') or os.environ.get('API_PASSWORD') else ' + ACCESS_TOKEN or API_PASSWORD'}")
    print(f"  Marketing Streams     : {state('DB_PASSWORD', 'ADS_CLIENT_ID')}")
    if LWA_CLIENT_ID and ADS_CLIENT_ID and LWA_CLIENT_ID == ADS_CLIENT_ID:
        print("  ! LWA_CLIENT_ID and ADS_CLIENT_ID are identical — SP-API and the "
              "Advertising API normally use different apps.")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    # debug=True runs a reloader; only the child process should hold the timer.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _leak_load_results()
        _leak_start_scheduler()
    _config_summary()
    print(f"Dashboard on http://{host}:{port}")
    app.run(debug=True, host=host, port=port)

#!/usr/bin/env python3
"""
SQP Bot Dashboard
Run: python dashboard.py
Then open http://localhost:5000
"""

import os
import psycopg2
import psycopg2.extras
import requests as http_requests
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "136.112.125.16"),
    "dbname": os.environ.get("DB_NAME", "postgres"),
    "user": os.environ.get("DB_USER", "filip_read_only"),
    "password": os.environ["DB_PASSWORD"],
    "connect_timeout": 10,
}

LWA_CLIENT_ID     = os.environ["LWA_CLIENT_ID"]
LWA_CLIENT_SECRET = os.environ["LWA_CLIENT_SECRET"]
LWA_TOKEN_URL     = "https://api.amazon.com/auth/o2/token"

SP_REGION_CONFIG = {
    1: {"endpoint": "sellingpartnerapi-na.amazon.com", "aws_region": "us-east-1"},
    2: {"endpoint": "sellingpartnerapi-eu.amazon.com", "aws_region": "eu-west-1"},
    3: {"endpoint": "sellingpartnerapi-fe.amazon.com", "aws_region": "us-west-2"},
}

PAGE_SIZE = 20

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


@app.route("/")
def index():
    return render_template("index.html")


# ── Step 1: DB query ────────────────────────────────────────────────────────

@app.route("/api/step1/sqp-stuck")
def step1_sqp_stuck():
    page = max(1, int(request.args.get("page", 1)))
    load_all = request.args.get("all") == "true"

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
            "ok": True,
            "total": total,
            "page": page,
            "page_size": len(rows_list) if load_all else PAGE_SIZE,
            "total_pages": 1 if load_all else max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "rows": rows_list,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Step 2: exchange refresh token → LWA access token ───────────────────────

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
            access_token = result["body"].get("access_token", "")
            return jsonify({
                "ok": True,
                "access_token": access_token,
                "expires_in": result["body"].get("expires_in"),
            })
        return jsonify({
            "ok": False,
            "error": f"LWA {result['status_code']}: {result['body']}",
        }), 400

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Step 3: check Amazon SP-API reports queue ────────────────────────────────

@app.route("/api/step3/check-queue")
def step3_check_queue():
    access_token = request.args.get("access_token", "")
    region_id    = int(request.args.get("region_id", 1))

    if not access_token:
        return jsonify({"ok": False, "error": "access_token required"}), 400

    config = SP_REGION_CONFIG.get(region_id)
    if not config:
        return jsonify({"ok": False, "error": f"Unknown region_id {region_id}"}), 400

    try:
        from requests_aws4auth import AWS4Auth

        aws_key    = os.environ.get("AWS_ACCESS_KEY_ID",     "")
        aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        aws_token  = os.environ.get("AWS_SESSION_TOKEN")
        aws_region = os.environ.get("AWS_REGION", config["aws_region"])

        auth = AWS4Auth(aws_key, aws_secret, aws_region, "execute-api",
                        session_token=aws_token or None)

        url = f"https://{config['endpoint']}/reports/2021-06-30/reports"
        resp = http_requests.get(
            url,
            params={
                "reportTypes":        "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT",
                "processingStatuses": "IN_QUEUE",
                "pageSize":           100,
            },
            auth=auth,
            headers={
                "Accept":             "application/json",
                "x-amz-access-token": access_token,
            },
            timeout=30,
        )

        data = resp.json()
        reports = data.get("reports", [])

        return jsonify({
            "ok":         resp.status_code == 200,
            "status_code": resp.status_code,
            "count":      len(reports),
            "next_token": data.get("nextToken"),
            "error":      str(data.get("errors", "")) if resp.status_code != 200 else None,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

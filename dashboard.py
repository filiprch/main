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
    LIMIT %(limit)s OFFSET %(offset)s
"""

_SQP_STUCK_COUNT = """
    SELECT COUNT(*) FROM (
        SELECT ari.amazon_selling_partner_id
        FROM amazon_report_info ari
        JOIN amazon_selling_partner asp
            ON asp.id = ari.amazon_selling_partner_id
        JOIN amazon_selling_api_token asat
            ON asat.amazon_selling_partner_id = ari.amazon_selling_partner_id
        WHERE ari.report_type = 'SQP_BY_ASIN'
          AND status NOT IN ('DONE','UNABLE_TO_GENERATE','REPORT_GENERATED_BY_AMAZON','DOWNLOADING_REPORT')
          AND ari.amazon_requested_report_id IS NOT NULL
        GROUP BY ari.amazon_selling_partner_id, asp.name, asat.refresh_token
    ) sub
"""

# Fetch all sellers across all pages (no pagination limit) for Step 2
_SQP_STUCK_ALL = """
    SELECT
        ari.amazon_selling_partner_id,
        asp.name AS seller_name,
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
    ORDER BY ari.amazon_selling_partner_id
"""


def get_db():
    return psycopg2.connect(**DB_CONFIG)


def exchange_token(refresh_token: str) -> dict:
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


@app.route("/api/step1/sqp-stuck")
def step1_sqp_stuck():
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PAGE_SIZE

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SQP_STUCK_COUNT)
                total = cur.fetchone()["count"]

                cur.execute(_SQP_STUCK_QUERY, {"limit": PAGE_SIZE, "offset": offset})
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
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "rows": rows_list,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/step2/exchange-tokens")
def step2_exchange_tokens():
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SQP_STUCK_ALL)
                sellers = cur.fetchall()

        results = []
        for seller in sellers:
            sp_id        = seller["amazon_selling_partner_id"]
            seller_name  = seller["seller_name"]
            refresh_token = seller["refresh_token"] or ""

            if not refresh_token:
                results.append({
                    "amazon_selling_partner_id": sp_id,
                    "seller_name": seller_name,
                    "ok": False,
                    "error": "No refresh token in DB",
                    "access_token": None,
                })
                continue

            try:
                resp = exchange_token(refresh_token)
                if resp["status_code"] == 200:
                    access_token = resp["body"].get("access_token", "")
                    results.append({
                        "amazon_selling_partner_id": sp_id,
                        "seller_name": seller_name,
                        "ok": True,
                        "access_token": access_token,
                        "access_token_preview": access_token[:20] + "..." if len(access_token) > 20 else access_token,
                        "expires_in": resp["body"].get("expires_in"),
                    })
                else:
                    results.append({
                        "amazon_selling_partner_id": sp_id,
                        "seller_name": seller_name,
                        "ok": False,
                        "error": f"HTTP {resp['status_code']}: {resp['body']}",
                        "access_token": None,
                    })
            except Exception as exc:
                results.append({
                    "amazon_selling_partner_id": sp_id,
                    "seller_name": seller_name,
                    "ok": False,
                    "error": str(exc),
                    "access_token": None,
                })

        success = sum(1 for r in results if r["ok"])
        return jsonify({
            "ok": True,
            "total": len(results),
            "success": success,
            "failed": len(results) - success,
            "results": results,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

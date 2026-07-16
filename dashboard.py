#!/usr/bin/env python3
"""
Batch Reports PUT Bot - Web Dashboard

A small local web UI on top of reports_bot.py. Paste the JSON body and the
seller IDs, press Go, and watch the PUT actions stream into a live log with
running counters. Press Stop to abort between sellers.

Run:
    python dashboard.py
    # then open http://127.0.0.1:5000

Uses the same environment / auth as reports_bot.py (PROD_HOST, API_USERNAME,
API_PASSWORD, or an ACCESS_TOKEN passthrough). See .env.example.
"""

import datetime
import json
import threading

from flask import Flask, Response, jsonify, request

from reports_bot import (
    ACCESS_TOKEN,
    PROD_HOST,
    _parse_seller_ids,
    api_login,
    put_reports,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory run state (single run at a time)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_stop_event = threading.Event()
_state = {
    "running": False,
    "dry_run": False,
    "logs": [],  # list of {"i": int, "t": str, "level": str, "msg": str}
    "counters": {"total": 0, "done": 0, "ok": 0, "failed": 0},
}


def _log(level: str, msg: str) -> None:
    with _lock:
        entry = {
            "i": len(_state["logs"]),
            "t": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        }
        _state["logs"].append(entry)


def _reset(total: int, dry_run: bool) -> None:
    with _lock:
        _state["logs"] = []
        _state["counters"] = {"total": total, "done": 0, "ok": 0, "failed": 0}
        _state["dry_run"] = dry_run
        _state["running"] = True


def _bump(field: str) -> None:
    with _lock:
        _state["counters"]["done"] += 1
        _state["counters"][field] += 1


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(token: str, seller_ids: list[str], body: dict, dry_run: bool) -> None:
    try:
        mode = "DRY-RUN" if dry_run else "LIVE"
        report_names = [r.get("reportName", "?") for r in body.get("reports", [])]
        _log("info", f"Start [{mode}] - {len(seller_ids)} seller(s), "
                     f"{len(report_names)} report(s): {', '.join(report_names) or '(none)'}")

        for idx, seller_id in enumerate(seller_ids, 1):
            if _stop_event.is_set():
                _log("warn", f"Stopped by user after {idx - 1}/{len(seller_ids)} seller(s).")
                break
            prefix = f"[{idx}/{len(seller_ids)}] seller {seller_id}"
            if dry_run:
                _log("info", f"{prefix}: would PUT {PROD_HOST}/reports/bi/{seller_id}/reports")
                _bump("ok")
                continue
            try:
                resp = put_reports(token, seller_id, body)
                if resp.ok:
                    _log("ok", f"{prefix}: OK ({resp.status_code})")
                    _bump("ok")
                else:
                    snippet = (resp.text or "").strip().replace("\n", " ")[:200]
                    _log("error", f"{prefix}: FAILED ({resp.status_code}) {snippet}")
                    _bump("failed")
            except Exception as exc:
                _log("error", f"{prefix}: ERROR {exc}")
                _bump("failed")

        c = _state["counters"]
        _log("info", f"Finished. {c['ok']} ok, {c['failed']} failed, "
                     f"{c['done']}/{c['total']} processed.")
    finally:
        with _lock:
            _state["running"] = False


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/start")
def api_start():
    with _lock:
        if _state["running"]:
            return jsonify({"error": "A run is already in progress."}), 409

    payload = request.get_json(silent=True) or {}
    raw_body = (payload.get("body") or "").strip()
    raw_sellers = payload.get("sellers") or ""
    dry_run = bool(payload.get("dry_run"))

    if not raw_body:
        return jsonify({"error": "Body is empty."}), 400
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Invalid JSON body: {exc}"}), 400

    seller_ids = _parse_seller_ids(raw_sellers)
    if not seller_ids:
        return jsonify({"error": "No seller IDs provided."}), 400

    # Authenticate up front so credential errors surface immediately.
    try:
        token = ACCESS_TOKEN if ACCESS_TOKEN else api_login()
    except Exception as exc:
        return jsonify({"error": f"Login failed: {exc}"}), 502

    _stop_event.clear()
    _reset(len(seller_ids), dry_run)
    threading.Thread(
        target=_worker, args=(token, seller_ids, body, dry_run), daemon=True
    ).start()
    return jsonify({"ok": True, "count": len(seller_ids), "sellers": seller_ids})


@app.post("/api/stop")
def api_stop():
    _stop_event.set()
    _log("warn", "Stop requested...")
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state():
    since = request.args.get("since", default=0, type=int)
    with _lock:
        logs = [e for e in _state["logs"] if e["i"] >= since]
        return jsonify({
            "running": _state["running"],
            "dry_run": _state["dry_run"],
            "counters": _state["counters"],
            "logs": logs,
            "next": len(_state["logs"]),
            "prod_host": PROD_HOST,
        })


@app.get("/")
def index():
    return Response(_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Frontend (single page, no external assets)
# ---------------------------------------------------------------------------

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reports PUT Dashboard</title>
<style>
  :root {
    --bg:#0f1115; --panel:#181b22; --border:#2a2f3a; --text:#e6e9ef;
    --muted:#8b93a3; --accent:#3b82f6; --ok:#22c55e; --err:#ef4444; --warn:#f59e0b;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f7; --panel:#fff; --border:#dde1e8; --text:#1a1d24;
            --muted:#616875; --accent:#2563eb; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--border);
    display:flex; align-items:baseline; gap:12px; }
  header h1 { font-size:16px; margin:0; }
  header .host { color:var(--muted); font-size:13px; font-family:monospace; }
  .wrap { display:grid; grid-template-columns:380px 1fr; gap:16px; padding:16px 24px; }
  @media (max-width:820px){ .wrap{ grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted);
    text-transform:uppercase; letter-spacing:.04em; margin:0 0 6px; }
  textarea { width:100%; background:var(--bg); color:var(--text);
    border:1px solid var(--border); border-radius:8px; padding:10px;
    font-family:monospace; font-size:13px; resize:vertical; }
  #body { min-height:220px; } #sellers { min-height:90px; }
  .row { display:flex; gap:10px; align-items:center; margin-top:14px; }
  button { font-size:14px; font-weight:600; border:none; border-radius:8px;
    padding:10px 20px; cursor:pointer; color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  #go { background:var(--accent); } #stop { background:var(--err); }
  .chk { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; margin-left:auto; }
  .counters { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:14px; }
  .stat { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px; text-align:center; }
  .stat .n { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
  .stat .k { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; margin-top:2px; }
  .stat.ok .n{ color:var(--ok);} .stat.err .n{ color:var(--err);}
  #log { background:#0b0d11; border:1px solid var(--border); border-radius:10px;
    padding:12px; height:52vh; overflow-y:auto; font-family:monospace; font-size:12.5px; line-height:1.55; }
  @media (prefers-color-scheme: light){ #log{ background:#0f1115; color:#e6e9ef; } }
  .line { white-space:pre-wrap; word-break:break-word; }
  .line .t { color:#6b7280; margin-right:8px; }
  .lv-ok{ color:#4ade80; } .lv-error{ color:#f87171; } .lv-warn{ color:#fbbf24; } .lv-info{ color:#93c5fd; }
  .status { font-size:13px; color:var(--muted); margin-left:8px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--muted); margin-right:6px; }
  .dot.run{ background:var(--ok); animation:pulse 1s infinite; }
  @keyframes pulse{ 50%{ opacity:.35; } }
</style>
</head>
<body>
<header>
  <h1>Reports PUT Dashboard</h1>
  <span class="host" id="host"></span>
  <span class="status"><span class="dot" id="dot"></span><span id="statusText">idle</span></span>
</header>

<div class="wrap">
  <div class="panel">
    <label for="body">Body (JSON)</label>
    <textarea id="body" placeholder='{ "reports": [ { "reportId": "...", "reportName": "Day-to-Day", "type": "DAY_TO_DAY" } ] }'></textarea>
    <div style="height:14px"></div>
    <label for="sellers">Seller ID list (space, comma or newline separated)</label>
    <textarea id="sellers" placeholder="27760726877, 12345&#10;67890"></textarea>
    <div class="row">
      <button id="go">Go</button>
      <button id="stop" disabled>Stop</button>
      <label class="chk"><input type="checkbox" id="dry"> dry-run</label>
    </div>
    <div id="err" style="color:var(--err);font-size:13px;margin-top:10px;"></div>
  </div>

  <div>
    <div class="counters">
      <div class="stat"><div class="n" id="c-done">0</div><div class="k">Performed</div></div>
      <div class="stat ok"><div class="n" id="c-ok">0</div><div class="k">OK</div></div>
      <div class="stat err"><div class="n" id="c-failed">0</div><div class="k">Failed</div></div>
      <div class="stat"><div class="n" id="c-total">0</div><div class="k">Total</div></div>
    </div>
    <div id="log"></div>
  </div>
</div>

<script>
let since = 0;
const $ = (id) => document.getElementById(id);

async function poll() {
  try {
    const r = await fetch('/api/state?since=' + since);
    const s = await r.json();
    $('host').textContent = s.prod_host;
    for (const e of s.logs) {
      const div = document.createElement('div');
      div.className = 'line';
      div.innerHTML = '<span class="t">' + e.t + '</span>' +
                      '<span class="lv-' + e.level + '">' + escapeHtml(e.msg) + '</span>';
      $('log').appendChild(div);
      since = e.i + 1;
    }
    if (s.logs.length) $('log').scrollTop = $('log').scrollHeight;
    $('c-done').textContent = s.counters.done;
    $('c-ok').textContent = s.counters.ok;
    $('c-failed').textContent = s.counters.failed;
    $('c-total').textContent = s.counters.total;
    setRunning(s.running, s.dry_run);
  } catch (_) {}
}

function setRunning(running, dry) {
  $('go').disabled = running;
  $('stop').disabled = !running;
  $('dot').className = 'dot' + (running ? ' run' : '');
  $('statusText').textContent = running ? (dry ? 'running (dry-run)' : 'running') : 'idle';
}

function escapeHtml(s){ return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

$('go').onclick = async () => {
  $('err').textContent = '';
  const dry = $('dry').checked;
  if (!dry && !confirm('Run LIVE against ' + $('host').textContent + '? This writes to production.')) return;
  since = 0;
  $('log').innerHTML = '';
  const r = await fetch('/api/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ body:$('body').value, sellers:$('sellers').value, dry_run:dry })
  });
  const d = await r.json();
  if (!r.ok) { $('err').textContent = d.error || 'Failed to start.'; return; }
  poll();
};

$('stop').onclick = async () => { await fetch('/api/stop', {method:'POST'}); };

setInterval(poll, 1000);
poll();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os as _os
    host = _os.environ.get("HOST", "127.0.0.1")
    port = int(_os.environ.get("PORT", "5000"))
    print(f"Dashboard on http://{host}:{port}  (target: {PROD_HOST})")
    app.run(host=host, port=port, threaded=True)

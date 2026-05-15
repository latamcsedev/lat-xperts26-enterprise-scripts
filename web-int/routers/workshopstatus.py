import html
import json
import os
import re
import threading
import time
from typing import List, Optional

import requests
import urllib3
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

router = APIRouter()
CACHE_PATH = "workshopstatus_cache.json"
MAX_HOSTS = 100
MAX_POLLS = 30
POLL_INTERVAL = 10

job_lock = threading.Lock()
job_state = {
    "running": False,
    "phase": "idle",
    "poll_round": 0,
    "message": "Idle",
    "hosts": [],
    "hosts_text": "",
    "last_run": None,
    "completion_time": None,
    "error": None,
}


def normalize_host(raw_host: str) -> str:
    host = raw_host.strip()
    host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
    host = host.rstrip("/")
    return host


def parse_hosts(value: str) -> List[str]:
    if not value:
        return []
    items = re.split(r"[\n\r,]+", value)
    hosts = []
    seen = set()
    for item in items:
        host = normalize_host(item)
        if not host:
            continue
        if host in seen:
            continue
        seen.add(host)
        hosts.append(host)
        if len(hosts) >= MAX_HOSTS:
            break
    return hosts


def make_host_entry(host: str) -> dict:
    return {
        "host": host,
        "state": "pending",
        "failed_count": None,
        "power_results": [],
        "license_results": [],
        "last_run": None,
        "error": None,
        "cached": False,
    }


def request_recalculate(host: str) -> Optional[str]:
    url = f"https://{host}:13015/labstatus/recalculate"
    for attempt in range(2):
        try:
            resp = requests.post(url, timeout=20, verify=False, proxies={"http": None, "https": None})
            resp.raise_for_status()
            return None
        except requests.RequestException as exc:
            if attempt < 1:
                time.sleep(2 ** attempt)
            else:
                return str(exc)
    return None


def request_labstatus(host: str):
    url = f"https://{host}:13015/api/labstatus"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=25, verify=False, proxies={"http": None, "https": None})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "failed_count" in data:
                return data, None
            return None, "Invalid response: missing failed_count"
        except requests.RequestException as exc:
            if attempt < 1:
                time.sleep(2 ** attempt)
            else:
                return None, str(exc)
        except ValueError as exc:
            return None, f"Invalid JSON: {exc}"
    return None, "Failed after retries"


def _set_state(**kwargs):
    with job_lock:
        job_state.update(kwargs)


def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def current_data():
    with job_lock:
        if job_state["hosts"]:
            return {
                "hosts": [h.copy() for h in job_state["hosts"]],
                "hosts_text": job_state.get("hosts_text", ""),
                "last_run": job_state["last_run"],
                "phase": job_state["phase"],
            }
    cached = _load_cache()
    if cached:
        return cached
    return {"hosts": [], "hosts_text": "", "last_run": None, "phase": "idle"}


def run_job(hosts: List[str], hosts_text: str):
    host_entries = [make_host_entry(h) for h in hosts]
    total = len(host_entries)

    _set_state(running=True, phase="recalculating", poll_round=0,
               message=f"Triggering recalculate on {total} host(s)...",
               hosts=[e.copy() for e in host_entries], hosts_text=hosts_text,
               last_run=None, completion_time=None, error=None)

    # Phase 1: trigger recalculate on all hosts
    for entry in host_entries:
        err = request_recalculate(entry["host"])
        if err:
            entry["state"] = "error"
            entry["error"] = err
        else:
            entry["state"] = "recalculating"

    with job_lock:
        job_state["hosts"] = [e.copy() for e in host_entries]

    # Phase 2: wait then poll, up to MAX_POLLS rounds.
    # Sleep FIRST so the remote portal has time to finish recalculating before we read.
    for poll_round in range(MAX_POLLS):
        _set_state(phase="polling", poll_round=poll_round,
                   message=f"Waiting {POLL_INTERVAL}s before poll {poll_round + 1}/{MAX_POLLS}…",
                   hosts=[e.copy() for e in host_entries])
        for _ in range(POLL_INTERVAL):
            time.sleep(1)
            with job_lock:
                if not job_state["running"]:
                    break

        pending = [e for e in host_entries if e["state"] == "recalculating"]
        if not pending:
            break

        _set_state(poll_round=poll_round + 1,
                   message=f"Poll {poll_round + 1}/{MAX_POLLS} — reading {len(pending)} host(s)…",
                   hosts=[e.copy() for e in host_entries])

        for entry in pending:
            data, err = request_labstatus(entry["host"])
            if err is None and data is not None:
                if not data.get("last_run"):
                    # Remote job still running — partial data, skip until next poll
                    continue
                entry["state"] = "done"
                entry["failed_count"] = int(data.get("failed_count", 0))
                entry["power_results"] = data.get("power_results", []) or []
                entry["license_results"] = data.get("license_results", []) or []
                entry["last_run"] = data.get("last_run")
                entry["cached"] = bool(data.get("cached", False))

        with job_lock:
            job_state["hosts"] = [e.copy() for e in host_entries]

        all_settled = all(e["state"] in ("done", "error") for e in host_entries)
        if all_settled:
            break

    # Mark anything still recalculating as timed out
    for entry in host_entries:
        if entry["state"] == "recalculating":
            entry["state"] = "timed_out"

    any_timed_out = any(e["state"] == "timed_out" for e in host_entries)
    final_phase = "timed_out" if any_timed_out else "done"
    last_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    with job_lock:
        job_state.update({
            "running": False,
            "phase": final_phase,
            "message": "Timed out — some hosts did not respond" if any_timed_out else "Complete",
            "hosts": [e.copy() for e in host_entries],
            "hosts_text": hosts_text,
            "last_run": last_run,
            "completion_time": time.time(),
        })

    _save_cache({
        "hosts": [e.copy() for e in host_entries],
        "hosts_text": hosts_text,
        "last_run": last_run,
        "phase": final_phase,
    })


def refresh_pending_job(hosts_text: str):
    with job_lock:
        host_entries = [e.copy() for e in job_state["hosts"]]
        hosts_text_existing = job_state.get("hosts_text", hosts_text)

    pending = [e for e in host_entries if e["state"] in ("timed_out", "error")]
    if not pending:
        return

    _set_state(running=True, phase="polling", poll_round=0,
               message=f"Refreshing {len(pending)} pending host(s)...",
               hosts=[e.copy() for e in host_entries])

    # Re-trigger recalculate only on timed_out hosts (not connectivity errors)
    for entry in host_entries:
        if entry["state"] == "timed_out":
            err = request_recalculate(entry["host"])
            if err:
                entry["state"] = "error"
                entry["error"] = err
            else:
                entry["state"] = "recalculating"

    with job_lock:
        job_state["hosts"] = [e.copy() for e in host_entries]

    for poll_round in range(MAX_POLLS):
        _set_state(poll_round=poll_round,
                   message=f"Waiting {POLL_INTERVAL}s before refresh poll {poll_round + 1}/{MAX_POLLS}…",
                   hosts=[e.copy() for e in host_entries])
        for _ in range(POLL_INTERVAL):
            time.sleep(1)
            with job_lock:
                if not job_state["running"]:
                    break

        pending_now = [e for e in host_entries if e["state"] == "recalculating"]
        if not pending_now:
            break

        _set_state(poll_round=poll_round + 1,
                   message=f"Refresh poll {poll_round + 1}/{MAX_POLLS} — reading {len(pending_now)} host(s)…",
                   hosts=[e.copy() for e in host_entries])

        for entry in pending_now:
            data, err = request_labstatus(entry["host"])
            if err is None and data is not None:
                if not data.get("last_run"):
                    # Remote job still running — partial data, skip until next poll
                    continue
                entry["state"] = "done"
                entry["failed_count"] = int(data.get("failed_count", 0))
                entry["power_results"] = data.get("power_results", []) or []
                entry["license_results"] = data.get("license_results", []) or []
                entry["last_run"] = data.get("last_run")
                entry["cached"] = bool(data.get("cached", False))

        with job_lock:
            job_state["hosts"] = [e.copy() for e in host_entries]

        all_settled = all(e["state"] in ("done", "error") for e in host_entries)
        if all_settled:
            break

    for entry in host_entries:
        if entry["state"] == "recalculating":
            entry["state"] = "timed_out"

    any_timed_out = any(e["state"] == "timed_out" for e in host_entries)
    final_phase = "timed_out" if any_timed_out else "done"
    last_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    with job_lock:
        job_state.update({
            "running": False,
            "phase": final_phase,
            "message": "Timed out — some hosts did not respond" if any_timed_out else "Complete",
            "hosts": [e.copy() for e in host_entries],
            "last_run": last_run,
            "completion_time": time.time(),
        })

    _save_cache({
        "hosts": [e.copy() for e in host_entries],
        "hosts_text": hosts_text_existing,
        "last_run": last_run,
        "phase": final_phase,
    })


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _summary_counts(hosts):
    passed = sum(1 for h in hosts if h["state"] == "done" and h.get("failed_count") == 0)
    failed = sum(1 for h in hosts if h["state"] == "done" and h.get("failed_count", 0) > 0)
    issues = sum(1 for h in hosts if h["state"] in ("error", "timed_out"))
    pending = sum(1 for h in hosts if h["state"] in ("pending", "recalculating"))
    return passed, failed, issues, pending


def _render_failed_detail(entry):
    power = [r["name"] for r in entry.get("power_results", []) if not r.get("ok")]
    lic = [r["name"] for r in entry.get("license_results", []) if not r.get("ok")]
    parts = []
    if power:
        parts.append("Power: " + ", ".join(html.escape(n) for n in power))
    if lic:
        parts.append("License: " + ", ".join(html.escape(n) for n in lic))
    sep = "<br>" if power else " &nbsp;·&nbsp; "
    return sep.join(parts) if parts else ""


def _render_host_row(entry):
    host = html.escape(entry["host"])
    state = entry["state"]
    failed_count = entry.get("failed_count")
    error_msg = html.escape(entry.get("error") or "")

    labstatus_url = f"https://{html.escape(entry['host'])}:13015/labstatus"
    fabric_url = f"https://{html.escape(entry['host'])}"
    btn_lab = f'<a class="row-btn" href="{labstatus_url}" target="_blank">Lab Status</a>'
    btn_fab = f'<a class="row-btn secondary" href="{fabric_url}" target="_blank">Fabric Studio</a>'

    if state == "done":
        if failed_count == 0:
            bg = "#e8f5e9"
            failed_cell = '<span class="badge green">0 passed</span>'
        else:
            bg = "#fdecea"
            detail = _render_failed_detail(entry)
            detail_html = (
                f'<details class="fail-detail"><summary>{failed_count} failed</summary>'
                f'<span class="detail-text">{detail}</span></details>'
            ) if detail else f'<span class="badge red">{failed_count} failed</span>'
            failed_cell = detail_html
        status_cell = '<span class="state-done">Done</span>'
    elif state in ("error", "timed_out"):
        bg = "#f8f9fa"
        label = "Timed out" if state == "timed_out" else f'<span title="{error_msg}">Connection error</span>'
        failed_cell = "—"
        status_cell = f'<span class="state-issue">{label}</span>'
    else:
        bg = "#fff8e1"
        failed_cell = "—"
        status_cell = '<span class="state-pending">Pending</span>'

    return (
        f'<tr style="background:{bg};">'
        f'<td class="host-cell">{host}</td>'
        f'<td class="failed-cell">{failed_cell}</td>'
        f'<td>{status_cell}</td>'
        f'<td>{btn_lab}</td>'
        f'<td>{btn_fab}</td>'
        f'</tr>'
    )


def render_page(message: Optional[str] = None, error_msg: Optional[str] = None):
    data = current_data()
    hosts = data.get("hosts", [])
    hosts_text = data.get("hosts_text", "")
    last_run = html.escape(str(data.get("last_run") or "Never"))

    with job_lock:
        running = job_state["running"]
        phase = job_state["phase"]
        poll_round = job_state["poll_round"]
        status_msg = job_state["message"]

    passed, failed_h, issues, pending = _summary_counts(hosts)
    host_rows = "".join(_render_host_row(e) for e in hosts)
    has_timed_out = any(e["state"] in ("timed_out",) for e in hosts)

    progress_display = "block" if running else "none"
    refresh_display = "inline-flex" if (has_timed_out and not running) else "none"

    notice_html = ""
    if message:
        notice_html = f'<div class="notice success">{html.escape(message)}</div>'
    elif error_msg:
        notice_html = f'<div class="notice error">{html.escape(error_msg)}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Workshop Status</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
           background: #f0f2f5; margin: 0; padding: 24px 32px; color: #1a1a2e; }}
    h1 {{ font-size: 24px; font-weight: 700; margin: 0 0 20px; }}

    /* Summary bar */
    .summary-bar {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
    .summary-card {{ flex: 1; min-width: 130px; background: white; border-radius: 10px;
                     padding: 14px 18px; box-shadow: 0 2px 6px rgba(0,0,0,.07);
                     border-top: 4px solid #ccc; }}
    .summary-card.passed {{ border-color: #2e7d32; }}
    .summary-card.failed {{ border-color: #c62828; }}
    .summary-card.issues {{ border-color: #e65100; }}
    .summary-card.pending {{ border-color: #1565c0; }}
    .summary-card .num {{ font-size: 28px; font-weight: 700; line-height: 1; }}
    .summary-card.passed .num {{ color: #2e7d32; }}
    .summary-card.failed .num {{ color: #c62828; }}
    .summary-card.issues .num {{ color: #e65100; }}
    .summary-card.pending .num {{ color: #1565c0; }}
    .summary-card .label {{ font-size: 12px; color: #666; margin-top: 4px; text-transform: uppercase;
                            letter-spacing: .5px; font-weight: 600; }}

    /* Cards */
    .card {{ background: white; border-radius: 12px; padding: 20px 24px;
             box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 20px; }}
    .card h2 {{ font-size: 15px; font-weight: 600; margin: 0 0 14px; color: #333; }}

    /* Input */
    textarea {{ width: 100%; min-height: 140px; resize: vertical; font-family: monospace;
               font-size: 13px; padding: 10px 12px; border-radius: 8px;
               border: 1px solid #d0d5dd; outline: none; margin-bottom: 12px; }}
    textarea:focus {{ border-color: #1677ff; box-shadow: 0 0 0 2px rgba(22,119,255,.15); }}

    /* Buttons */
    .btn-row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 10px 18px;
            border-radius: 8px; border: none; cursor: pointer; font-size: 14px;
            font-weight: 600; text-decoration: none; transition: opacity .15s; }}
    .btn:disabled {{ opacity: .5; cursor: not-allowed; }}
    .btn.primary {{ background: #1677ff; color: white; }}
    .btn.primary:hover:not(:disabled) {{ background: #0d5fe0; }}
    .btn.warning {{ background: #e65100; color: white; }}
    .btn.warning:hover:not(:disabled) {{ background: #bf360c; }}
    .btn.ghost {{ background: transparent; color: #555; border: 1px solid #d0d5dd; }}
    .btn.ghost:hover {{ background: #f5f5f5; }}

    /* Progress */
    .progress-card {{ display: none; }}
    .progress-card.active {{ display: block; }}
    .progress-info {{ display: flex; justify-content: space-between; font-size: 13px;
                      color: #555; margin-bottom: 6px; }}
    .progress-track {{ background: #e9ecef; border-radius: 999px; height: 10px; overflow: hidden; }}
    .progress-fill {{ background: linear-gradient(90deg, #1677ff, #0d47a1); height: 100%;
                      width: 0%; transition: width .4s ease; border-radius: 999px; }}
    .progress-msg {{ font-size: 12px; color: #777; margin-top: 6px; }}

    /* Table */
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th {{ background: #f8f9fa; color: #444; font-size: 12px; font-weight: 700;
          text-transform: uppercase; letter-spacing: .4px; padding: 10px 14px;
          border-bottom: 2px solid #e9ecef; text-align: left; white-space: nowrap; }}
    td {{ padding: 11px 14px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    .host-cell {{ font-family: monospace; font-size: 13px; }}
    .failed-cell {{ min-width: 140px; }}

    /* Badges & states */
    .badge {{ display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 12px;
              font-weight: 700; }}
    .badge.green {{ background: #e8f5e9; color: #2e7d32; }}
    .badge.red {{ background: #fdecea; color: #c62828; }}
    .state-done {{ color: #2e7d32; font-weight: 600; font-size: 13px; }}
    .state-issue {{ color: #e65100; font-weight: 600; font-size: 13px; }}
    .state-pending {{ color: #1565c0; font-weight: 600; font-size: 13px; }}

    /* Expandable failure detail */
    .fail-detail summary {{ cursor: pointer; font-size: 13px; font-weight: 700;
                            color: #c62828; list-style: none; display: inline-flex;
                            align-items: center; gap: 4px; }}
    .fail-detail summary::-webkit-details-marker {{ display: none; }}
    .fail-detail summary::after {{ content: " ▾"; font-size: 10px; }}
    .fail-detail[open] summary::after {{ content: " ▴"; }}
    .detail-text {{ display: block; margin-top: 5px; font-size: 12px; color: #555;
                    line-height: 1.5; }}

    /* Row buttons */
    .row-btn {{ display: inline-block; padding: 5px 10px; border-radius: 6px;
                background: #1677ff; color: white; font-size: 12px; font-weight: 600;
                text-decoration: none; margin-right: 4px; }}
    .row-btn:hover {{ background: #0d5fe0; }}
    .row-btn.secondary {{ background: #6c757d; }}
    .row-btn.secondary:hover {{ background: #545b62; }}

    /* Notices */
    .notice {{ padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-top: 10px; }}
    .notice.success {{ background: #e8f7e7; border: 1px solid #c8e6ca; color: #1a6f3a; }}
    .notice.error {{ background: #fdecea; border: 1px solid #f5c2c0; color: #9f3a2b; }}

    .meta {{ font-size: 12px; color: #888; margin-bottom: 12px; }}

    @media (max-width: 700px) {{
      body {{ padding: 12px; }}
      .summary-bar {{ gap: 8px; }}
      .summary-card {{ min-width: 100px; padding: 10px 12px; }}
    }}
  </style>
</head>
<body>
  <h1>Workshop Status</h1>

  <!-- Summary -->
  <div class="summary-bar" id="summary-bar">
    <div class="summary-card passed">
      <div class="num" id="sum-passed">{passed}</div>
      <div class="label">Passed</div>
    </div>
    <div class="summary-card failed">
      <div class="num" id="sum-failed">{failed_h}</div>
      <div class="label">Failed</div>
    </div>
    <div class="summary-card issues">
      <div class="num" id="sum-issues">{issues}</div>
      <div class="label">Connectivity Issues</div>
    </div>
    <div class="summary-card pending">
      <div class="num" id="sum-pending">{pending}</div>
      <div class="label">Pending</div>
    </div>
  </div>

  <!-- Input -->
  <div class="card">
    <h2>Hosts</h2>
    <form id="start-form" action="/workshopstatus/start" method="post">
      <textarea id="hosts-input" name="hosts" maxlength="8000"
                placeholder="10.254.1.6&#10;lab1.example.com&#10;lab2.example.com">{html.escape(hosts_text)}</textarea>
      <div class="btn-row">
        <button id="start-btn" class="btn primary" type="submit"
                {'disabled' if running else ''}>
          {'Running…' if running else 'Start'}
        </button>
        <button id="refresh-btn" class="btn warning" type="button"
                style="display:{refresh_display};"
                onclick="refreshPending()">
          Refresh Pending
        </button>
        <a class="btn ghost" href="/">&#8592; Home</a>
      </div>
      {notice_html}
    </form>
  </div>

  <!-- Progress -->
  <div class="card progress-card {'active' if running else ''}" id="progress-card">
    <div class="progress-info">
      <span id="prog-label">Starting…</span>
      <span id="prog-pct">0%</span>
    </div>
    <div class="progress-track">
      <div class="progress-fill" id="prog-fill" style="width:0%;"></div>
    </div>
    <div class="progress-msg" id="prog-msg">{html.escape(status_msg) if running else ''}</div>
  </div>

  <!-- Results table -->
  <div class="card" id="results-card" style="{'display:none' if not hosts else ''}">
    <div class="meta">Last run: <strong>{last_run}</strong></div>
    <div class="table-wrap">
      <table id="results-table">
        <thead>
          <tr>
            <th>Host</th>
            <th>Failed Checks</th>
            <th>Status</th>
            <th>Lab Status</th>
            <th>Fabric Studio</th>
          </tr>
        </thead>
        <tbody id="results-body">
          {host_rows if hosts else '<tr><td colspan="5" style="color:#999;text-align:center;padding:24px;">No results yet — enter hosts above and click Start.</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const MAX_POLLS = {MAX_POLLS};
    const POLL_INTERVAL_MS = {POLL_INTERVAL * 1000};
    let pollTimer = null;
    let isPolling = false;

    function escHtml(s) {{
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    function renderRow(h) {{
      var host = escHtml(h.host);
      var labUrl = 'https://' + host + ':13015/labstatus';
      var fabUrl = 'https://' + host;
      var btnLab = '<a class="row-btn" href="' + labUrl + '" target="_blank">Lab Status</a>';
      var btnFab = '<a class="row-btn secondary" href="' + fabUrl + '" target="_blank">Fabric Studio</a>';
      var bg, failedCell, statusCell;

      if (h.state === 'done') {{
        if (h.failed_count === 0) {{
          bg = '#e8f5e9';
          failedCell = '<span class="badge green">0 passed</span>';
        }} else {{
          bg = '#fdecea';
          var powerFailed = (h.power_results || []).filter(r => !r.ok).map(r => escHtml(r.name));
          var licFailed = (h.license_results || []).filter(r => !r.ok).map(r => escHtml(r.name));
          var parts = [];
          if (powerFailed.length) parts.push('Power: ' + powerFailed.join(', '));
          if (licFailed.length) parts.push('License: ' + licFailed.join(', '));
          var sep = powerFailed.length ? '<br>' : ' &nbsp;·&nbsp; ';
          var detail = parts.join(sep);
          if (detail) {{
            failedCell = '<details class="fail-detail"><summary>' + h.failed_count + ' failed</summary>' +
              '<span class="detail-text">' + detail + '</span></details>';
          }} else {{
            failedCell = '<span class="badge red">' + h.failed_count + ' failed</span>';
          }}
        }}
        statusCell = '<span class="state-done">Done</span>';
      }} else if (h.state === 'error' || h.state === 'timed_out') {{
        bg = '#f8f9fa';
        failedCell = '—';
        var label = h.state === 'timed_out' ? 'Timed out' :
          '<span title="' + escHtml(h.error || '') + '">Connection error</span>';
        statusCell = '<span class="state-issue">' + label + '</span>';
      }} else {{
        bg = '#fff8e1';
        failedCell = '—';
        statusCell = '<span class="state-pending">Pending</span>';
      }}

      return '<tr style="background:' + bg + ';">' +
        '<td class="host-cell">' + host + '</td>' +
        '<td class="failed-cell">' + failedCell + '</td>' +
        '<td>' + statusCell + '</td>' +
        '<td>' + btnLab + '</td>' +
        '<td>' + btnFab + '</td>' +
        '</tr>';
    }}

    function updateSummary(hosts) {{
      var passed = hosts.filter(h => h.state === 'done' && h.failed_count === 0).length;
      var failed = hosts.filter(h => h.state === 'done' && h.failed_count > 0).length;
      var issues = hosts.filter(h => h.state === 'error' || h.state === 'timed_out').length;
      var pend = hosts.filter(h => h.state === 'pending' || h.state === 'recalculating').length;
      document.getElementById('sum-passed').textContent = passed;
      document.getElementById('sum-failed').textContent = failed;
      document.getElementById('sum-issues').textContent = issues;
      document.getElementById('sum-pending').textContent = pend;
    }}

    function updateTable(hosts) {{
      var tbody = document.getElementById('results-body');
      if (!hosts || !hosts.length) return;
      tbody.innerHTML = hosts.map(renderRow).join('');
      document.getElementById('results-card').style.display = '';
    }}

    function updateProgress(status) {{
      var running = status.running;
      var phase = status.phase;
      var round = status.poll_round || 0;
      var pct = running ? Math.round((round / MAX_POLLS) * 90) : 100;
      document.getElementById('prog-fill').style.width = pct + '%';
      document.getElementById('prog-pct').textContent = pct + '%';
      document.getElementById('prog-label').textContent = status.message || '';
      document.getElementById('prog-msg').textContent = status.message || '';
      document.getElementById('progress-card').classList.toggle('active', running);
      document.getElementById('start-btn').disabled = running;
      document.getElementById('start-btn').textContent = running ? 'Running…' : 'Start';
      var hasTimed = phase === 'timed_out' && !running;
      document.getElementById('refresh-btn').style.display = hasTimed ? 'inline-flex' : 'none';
    }}

    async function poll() {{
      try {{
        var [statusResp, dataResp] = await Promise.all([
          fetch('/api/workshopstatus/status'),
          fetch('/api/workshopstatus/data'),
        ]);
        var status = await statusResp.json();
        var data = await dataResp.json();
        updateProgress(status);
        updateSummary(data.hosts || []);
        updateTable(data.hosts || []);
        if (!status.running) {{
          stopPolling();
        }}
      }} catch (e) {{
        console.error('Poll error:', e);
      }}
    }}

    function startPolling() {{
      if (isPolling) return;
      isPolling = true;
      poll();
      pollTimer = setInterval(poll, 5000);
    }}

    function stopPolling() {{
      isPolling = false;
      if (pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
    }}

    function refreshPending() {{
      fetch('/workshopstatus/refresh_pending', {{ method: 'POST' }})
        .then(() => startPolling())
        .catch(e => console.error('Refresh pending error:', e));
      document.getElementById('refresh-btn').style.display = 'none';
      document.getElementById('start-btn').disabled = true;
      document.getElementById('start-btn').textContent = 'Running…';
      document.getElementById('progress-card').classList.add('active');
    }}

    document.getElementById('start-form').addEventListener('submit', () => {{
      startPolling();
    }});

    // Auto-start polling if job is running on page load
    fetch('/api/workshopstatus/status')
      .then(r => r.json())
      .then(s => {{ if (s.running) startPolling(); }})
      .catch(() => {{}});
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/workshopstatus", response_class=HTMLResponse)
def workshopstatus_page():
    return HTMLResponse(render_page())


@router.post("/workshopstatus/start")
def workshopstatus_start(hosts: str = Form(...)):
    parsed = parse_hosts(hosts)
    if not parsed:
        return HTMLResponse(render_page(error_msg="Enter at least one valid host (up to 100)."))
    if len(parsed) > MAX_HOSTS:
        return HTMLResponse(render_page(error_msg=f"Maximum {MAX_HOSTS} hosts allowed."))

    with job_lock:
        already_running = job_state["running"]

    if already_running:
        return HTMLResponse(render_page(error_msg="A job is already running. Wait for it to finish."))

    threading.Thread(target=run_job, args=(parsed, hosts), daemon=True).start()
    return RedirectResponse(url="/workshopstatus", status_code=303)


@router.post("/workshopstatus/refresh_pending")
def workshopstatus_refresh_pending():
    with job_lock:
        already_running = job_state["running"]
        hosts_text = job_state.get("hosts_text", "")
        has_pending = any(h["state"] == "timed_out" for h in job_state["hosts"])

    if already_running:
        return JSONResponse({"ok": False, "message": "Job already running."}, status_code=409)
    if not has_pending:
        return JSONResponse({"ok": False, "message": "No timed-out hosts to refresh."}, status_code=400)

    threading.Thread(target=refresh_pending_job, args=(hosts_text,), daemon=True).start()
    return JSONResponse({"ok": True, "message": "Refresh started."})


@router.get("/api/workshopstatus/status")
def workshopstatus_status():
    with job_lock:
        return JSONResponse({
            "running": job_state["running"],
            "phase": job_state["phase"],
            "poll_round": job_state["poll_round"],
            "message": job_state["message"],
            "host_count": len(job_state["hosts"]),
            "done_count": sum(1 for h in job_state["hosts"] if h["state"] in ("done", "error", "timed_out")),
            "completion_time": job_state.get("completion_time"),
        })


@router.get("/api/workshopstatus/data")
def workshopstatus_data():
    data = current_data()
    return JSONResponse(data)

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
from fastapi.responses import HTMLResponse, JSONResponse

from utils import load_inventory

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

router = APIRouter()
CACHE_PATH = "workshop_status_cache.json"
MAX_HOSTS = 100
POLL_INTERVAL = 30
POLL_TIMEOUT = 600

job_lock = threading.Lock()
job_state = {
    "running": False,
    "progress": 0,
    "message": "Idle",
    "hosts": [],
    "hosts_text": "",
    "last_run": None,
    "inventory_sum_state": None,
    "poll_count": 0,
    "error": None,
}


def normalize_host(raw_host: str) -> str:
    host = raw_host.strip()
    host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
    host = host.rstrip("/")
    return host


def normalize_power_status(status: str) -> str:
    if not status:
        return "unknown"
    value = status.lower().strip()
    if value == "running":
        return "power-on"
    if value in ["power-off", "stopped", "shutdown", "shut off", "shut-off", "powered off"]:
        return "power-off"
    return value


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


def get_inventory_sum_state() -> Optional[int]:
    inventory = load_inventory()
    sum_state = inventory.get("sum_state")
    if sum_state is None:
        sum_state = inventory.get("sum_sate")
    if isinstance(sum_state, str) and sum_state.isdigit():
        return int(sum_state)
    if isinstance(sum_state, int):
        return sum_state
    try:
        return int(sum_state)
    except Exception:
        return None


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return None

    try:
        with open(CACHE_PATH, "r") as cache_file:
            return json.load(cache_file)
    except Exception:
        return None


def save_cache(data):
    with open(CACHE_PATH, "w") as cache_file:
        json.dump(data, cache_file, indent=2)


def set_job_state(**kwargs):
    with job_lock:
        job_state.update(kwargs)


def make_host_entry(host: str, inventory_sum_state: Optional[int]):
    return {
        "host": host,
        "error": None,
        "last_run": None,
        "sum_state": None,
        "inventory_sum_state": inventory_sum_state,
        "match": None,
        "done": False,
        "message": "Queued",
        "power_results": [],
        "license_results": [],
    }


def request_recalculate(host: str) -> Optional[str]:
    url = f"https://{host}:13015/labstatus/recalculate"
    try:
        response = requests.post(url, timeout=15, verify=False)
        response.raise_for_status()
        return None
    except requests.RequestException as exc:
        return str(exc)


def request_labstatus(host: str):
    url = f"https://{host}:13015/api/labstatus"
    try:
        response = requests.get(url, timeout=20, verify=False)
        response.raise_for_status()
        return response.json(), None
    except requests.RequestException as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, f"Invalid JSON response: {str(exc)}"


def current_status():
    with job_lock:
        return {
            "running": job_state["running"],
            "progress": job_state["progress"],
            "message": job_state["message"],
            "host_count": len(job_state["hosts"]),
            "completed_count": sum(1 for host in job_state["hosts"] if host.get("done")),
            "last_run": job_state["last_run"],
            "poll_count": job_state["poll_count"],
            "error": job_state["error"],
        }


def current_data():
    if job_state["running"]:
        with job_lock:
            return {
                "last_run": job_state["last_run"],
                "hosts": [host.copy() for host in job_state["hosts"]],
                "hosts_text": job_state.get("hosts_text", ""),
                "inventory_sum_state": job_state["inventory_sum_state"],
                "poll_count": job_state["poll_count"],
            }

    cached = load_cache()
    if cached:
        return cached
    return {
        "last_run": None,
        "hosts": [],
        "hosts_text": "",
        "inventory_sum_state": get_inventory_sum_state(),
        "poll_count": 0,
    }


def update_host_results(host_entries, host_index, data):
    entry = host_entries[host_index]
    entry["last_run"] = data.get("last_run")
    entry["sum_state"] = data.get("sum_state")
    entry["power_results"] = data.get("power_results", []) or []
    entry["license_results"] = data.get("license_results", []) or []
    entry["done"] = True
    entry["match"] = entry["sum_state"] == entry["inventory_sum_state"] if entry["inventory_sum_state"] is not None else None
    entry["message"] = "Completed"


def refresh_lab_status(hosts: List[str]):
    inventory_sum_state = get_inventory_sum_state()
    host_entries = [make_host_entry(host, inventory_sum_state) for host in hosts]
    start_time = time.time()
    run_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    with job_lock:
        job_state.update({
            "running": True,
            "progress": 0,
            "message": "Starting labstatus refresh",
            "hosts": [host.copy() for host in host_entries],
            "hosts_text": job_state.get("hosts_text", ""),
            "last_run": run_time,
            "inventory_sum_state": inventory_sum_state,
            "poll_count": 0,
            "error": None,
        })

    save_cache({
        "last_run": run_time,
        "hosts": [host.copy() for host in host_entries],
        "hosts_text": job_state.get("hosts_text", ""),
        "inventory_sum_state": inventory_sum_state,
        "poll_count": 0,
    })

    for index, entry in enumerate(host_entries):
        entry["message"] = "Sending recalculation request"
        with job_lock:
            job_state["hosts"][index] = entry.copy()
            job_state["message"] = f"Recalculating {entry['host']}"
            job_state["progress"] = int(index / max(1, len(host_entries)) * 100)
        save_cache({
            "last_run": run_time,
            "hosts": [host.copy() for host in host_entries],
            "inventory_sum_state": inventory_sum_state,
            "poll_count": job_state["poll_count"],
        })

        error = request_recalculate(entry["host"])
        if error:
            entry["message"] = f"Recalculate failed: {error}"
            entry["error"] = error
        else:
            entry["message"] = "Recalculate requested"

        with job_lock:
            job_state["hosts"][index] = entry.copy()
            job_state["message"] = f"Recalculate phase complete for {entry['host']}"
            job_state["progress"] = int((index + 1) / max(1, len(host_entries)) * 10)
        save_cache({
            "last_run": run_time,
            "hosts": [host.copy() for host in host_entries],
            "inventory_sum_state": inventory_sum_state,
            "poll_count": job_state["poll_count"],
        })

    while time.time() - start_time < POLL_TIMEOUT:
        if all(entry["done"] for entry in host_entries):
            break

        for index, entry in enumerate(host_entries):
            if entry["done"]:
                continue

            entry["message"] = "Polling labstatus result"
            with job_lock:
                job_state["hosts"][index] = entry.copy()
                job_state["message"] = f"Polling {entry['host']}"
            save_cache({
                "last_run": run_time,
                "hosts": [host.copy() for host in host_entries],
                "inventory_sum_state": inventory_sum_state,
                "poll_count": job_state["poll_count"],
            })

            data, error = request_labstatus(entry["host"])
            if error:
                entry["message"] = f"Poll failed: {error}"
                entry["error"] = error
            elif isinstance(data, dict) and data.get("sum_state") is not None:
                update_host_results(host_entries, index, data)
            else:
                entry["message"] = "Waiting for labstatus result"

            with job_lock:
                completed = sum(1 for h in host_entries if h["done"])
                job_state["hosts"][index] = entry.copy()
                job_state["progress"] = int(completed / max(1, len(host_entries)) * 100)
                job_state["poll_count"] += 1
            save_cache({
                "last_run": run_time,
                "hosts": [host.copy() for host in host_entries],
                "inventory_sum_state": inventory_sum_state,
                "poll_count": job_state["poll_count"],
            })

        if all(entry["done"] for entry in host_entries):
            break

        time_remaining = POLL_TIMEOUT - (time.time() - start_time)
        if time_remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL, time_remaining))

    for entry in host_entries:
        if not entry["done"]:
            entry["message"] = entry.get("message") or "Timed out waiting for labstatus"

    run_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    save_cache({
        "last_run": run_time,
        "hosts": [host.copy() for host in host_entries],
        "hosts_text": job_state.get("hosts_text", ""),
        "inventory_sum_state": inventory_sum_state,
        "poll_count": job_state["poll_count"],
    })

    with job_lock:
        job_state.update({
            "running": False,
            "progress": 100,
            "message": "Refresh complete",
            "hosts": [host.copy() for host in host_entries],
            "last_run": run_time,
            "inventory_sum_state": inventory_sum_state,
            "poll_count": job_state["poll_count"],
            "error": None,
        })


def start_refresh_in_background(hosts: List[str], hosts_text: str) -> bool:
    with job_lock:
        if job_state["running"]:
            return False
        job_state["running"] = True
        job_state["hosts_text"] = hosts_text

    thread = threading.Thread(target=refresh_lab_status, args=(hosts,), daemon=True)
    thread.start()
    return True


def render_host_row(entry):
    host_name = html.escape(entry["host"])
    sum_state = html.escape(str(entry["sum_state"])) if entry.get("sum_state") is not None else "pending"
    inventory_state = html.escape(str(entry["inventory_sum_state"])) if entry.get("inventory_sum_state") is not None else "N/A"
    comparison = "Match" if entry.get("match") else "Mismatch"
    comparison_color = "#d4edda" if entry.get("match") else "#f8d7da"
    open_url = f"https://{html.escape(entry['host'])}:13015/labstatus"
    status_text = html.escape(entry.get("message", "Pending"))

    return f"""
        <tr style=\"background:{comparison_color};\">
            <td>{host_name}</td>
            <td>{sum_state}</td>
            <td>{inventory_state}</td>
            <td>{comparison}</td>
            <td><a class=\"button-link\" href=\"{open_url}\" target=\"_blank\">Open</a></td>
            <td>{status_text}</td>
        </tr>
    """


def render_host_details(entry):
    host_name = html.escape(entry["host"])
    sum_state = html.escape(str(entry["sum_state"])) if entry.get("sum_state") is not None else "pending"
    status_text = html.escape(entry.get("message", "Pending"))

    power_rows = ""
    if entry.get("power_results"):
        for row in entry["power_results"]:
            expected_norm = normalize_power_status(str(row.get("expected_state", "")))
            actual_norm = normalize_power_status(str(row.get("status", "")))
            row_match = expected_norm == actual_norm
            row_color = "#e8f5e9" if row_match else "#fdecea"
            power_rows += f"<tr style=\"background:{row_color};\">"
            power_rows += f"<td>{html.escape(str(row.get('name', '')))}</td>"
            power_rows += f"<td>{html.escape(str(row.get('expected_state', '')))}</td>"
            power_rows += f"<td>{html.escape(str(row.get('status', '')))}</td>"
            power_rows += "</tr>"
    else:
        power_rows = "<tr><td colspan=3>No power results available.</td></tr>"

    license_rows = ""
    if entry.get("license_results"):
        for row in entry["license_results"]:
            status_value = str(row.get("status", "")).lower()
            if status_value == "valid":
                row_color = "#e8f5e9"
            elif status_value == "warning":
                row_color = "#fff8e1"
            else:
                row_color = "#fdecea"
            license_rows += f"<tr style=\"background:{row_color};\">"
            license_rows += f"<td>{html.escape(str(row.get('name', '')))}</td>"
            license_rows += f"<td>{html.escape(str(row.get('status', '')))}</td>"
            license_rows += "</tr>"
    else:
        license_rows = "<tr><td colspan=2>No license results available.</td></tr>"

    return f"""
        <details class=\"host-details\">
            <summary>{host_name} — sum_state: {sum_state} — {status_text}</summary>
            <div class=\"details-content\">
                <h3>Power Results</h3>
                <table>
                    <thead>
                        <tr><th>Name</th><th>Expected</th><th>Status</th></tr>
                    </thead>
                    <tbody>{power_rows}</tbody>
                </table>
                <h3>License Results</h3>
                <table>
                    <thead>
                        <tr><th>Name</th><th>Status</th></tr>
                    </thead>
                    <tbody>{license_rows}</tbody>
                </table>
            </div>
        </details>
    """


def render_dashboard_page(message: Optional[str] = None, error_message: Optional[str] = None, hosts_text: Optional[str] = None):
    data = current_data()
    if hosts_text is not None:
        data["hosts_text"] = hosts_text
    progress = 0
    status = "Idle"
    running = False
    error = None
    with job_lock:
        progress = job_state.get("progress", 0)
        status = job_state.get("message", "Idle")
        running = job_state.get("running", False)
        error = job_state.get("error")

    if error_message:
        error = error_message

    host_rows = ""
    host_details = ""
    for host_entry in data.get("hosts", []):
        host_rows += render_host_row(host_entry)
        host_details += render_host_details(host_entry)

    inventory_sum = html.escape(str(data.get("inventory_sum_state", "N/A")))
    last_run = html.escape(str(data.get("last_run") or "None"))
    host_count = len(data.get("hosts", []))
    poll_count = data.get("poll_count", 0)
    message_text = html.escape(str(message or ""))
    error_text = html.escape(str(error or ""))

    return f"""
    <html>
    <head>
        <title>Workshop Status</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 30px; }}
            h1 {{ margin-bottom: 10px; }}
            .section {{ background: white; padding: 24px; border-radius: 12px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); margin-bottom: 24px; }}
            textarea {{ width: 100%; min-height: 180px; resize: vertical; font-family: monospace; font-size: 14px; padding: 12px; border-radius: 10px; border: 1px solid #ccd0d5; }}
            .btn {{ padding: 12px 18px; border-radius: 8px; border: none; cursor: pointer; font-size: 15px; font-weight: 600; }}
            .primary {{ background: #1677ff; color: white; }}
            .secondary {{ background: #6c757d; color: white; }}
            .danger {{ background: #dc3545; color: white; }}
            .button-link {{ text-decoration: none; display: inline-block; background: #495057; color: white; padding: 8px 14px; border-radius: 6px; }}
            .status-card {{ display: grid; grid-template-columns: repeat(3, minmax(200px, 1fr)); gap: 16px; margin-top: 18px; }}
            .status-card .box {{ background: #f8f9fa; padding: 16px; border-radius: 10px; border: 1px solid #e2e5eb; }}
            .status-row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 18px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th, td {{ border: 1px solid #e2e5eb; padding: 10px 12px; text-align: left; }}
            th {{ background: #f1f3f5; }}
            .details-content {{ padding: 16px 0; }}
            details {{ background: white; border: 1px solid #dde2e8; border-radius: 10px; margin-bottom: 16px; padding: 12px 14px; }}
            summary {{ font-weight: 700; cursor: pointer; outline: none; }}
            .notice {{ margin-top: 12px; padding: 12px; border-radius: 10px; }}
            .notice.success {{ background: #e8f7e7; border: 1px solid #c8e6ca; color: #1a6f3a; }}
            .notice.error {{ background: #fdecea; border: 1px solid #f5c2c0; color: #9f3a2b; }}
            @media (max-width: 900px) {{ .status-card {{ grid-template-columns: 1fr; }} }}
        </style>
    </head>
    <body>
        <h1>Workshop Status</h1>
        <div class="section">
            <form id="run-form" action="/workshop_status/run" method="post">
                <label for="hosts"><strong>Enter up to 100 IPs or FQDNs</strong> (one per line or comma separated)</label>
                <textarea id="hosts" name="hosts" maxlength="8000" placeholder="10.254.1.6\nlab1.example.com\nlab2.example.com">{html.escape(data.get('hosts_text', ''))}</textarea>
                <div class="status-row">
                    <button class="btn primary" type="submit">Run</button>
                    <a class="button-link" href="/">Back to Home</a>
                </div>
            </form>
            {f'<div class="notice success">{message_text}</div>' if message_text else ''}
            {f'<div class="notice error">{error_text}</div>' if error_text else ''}
        </div>

        <div class="section">
            <div class="status-card">
                <div class="box"><strong>Last run</strong><div>{last_run}</div></div>
                <div class="box"><strong>Hosts configured</strong><div>{host_count}</div></div>
                <div class="box"><strong>Inventory sum_state</strong><div>{inventory_sum}</div></div>
                <div class="box"><strong>Progress</strong><div>{progress}%</div></div>
                <div class="box"><strong>Status</strong><div>{html.escape(str(status))}</div></div>
                <div class="box"><strong>Poll count</strong><div>{poll_count}</div></div>
            </div>

            <table>
                <thead>
                    <tr><th>Host</th><th>Host sum_state</th><th>Inventory sum_state</th><th>Result</th><th>Open Labstatus</th><th>Message</th></tr>
                </thead>
                <tbody>
                    {host_rows or '<tr><td colspan="6">No hosts have been run yet.</td></tr>'}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Host details</h2>
            {host_details or '<p>No host results available yet.</p>'}
        </div>

        <script>
            function refreshStatus() {{
                fetch('/api/workshop_status/status')
                    .then(response => response.json())
                    .then(state => {{
                        if (state.running) {{
                            setTimeout(() => window.location.reload(), 10000);
                        }} else {{
                            setTimeout(refreshStatus, 10000);
                        }}
                    }})
                    .catch(() => {{
                        setTimeout(refreshStatus, 10000);
                    }});
            }}

            window.addEventListener('load', () => {{
                refreshStatus();
            }});
        </script>
    </body>
    </html>
    """


@router.get("/workshop_status", response_class=HTMLResponse)
def workshop_status():
    return HTMLResponse(render_dashboard_page())


@router.post("/workshop_status/run", response_class=HTMLResponse)
def run_workshop_status(hosts: str = Form(...)):
    parsed_hosts = parse_hosts(hosts)
    if not parsed_hosts:
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message="Enter at least one valid host, up to 100 hosts."))

    if len(parsed_hosts) > MAX_HOSTS:
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message=f"Maximum allowed hosts is {MAX_HOSTS}."))

    if not start_refresh_in_background(parsed_hosts, hosts):
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message="A refresh is already running. Please wait for it to complete."))

    return HTMLResponse(render_dashboard_page(hosts_text=hosts, message=f"Started refresh for {len(parsed_hosts)} host(s)."))


@router.get("/api/workshop_status/status")
def workshop_status_status():
    return JSONResponse(current_status())


@router.get("/api/workshop_status/data")
def workshop_status_data():
    return JSONResponse(current_data())

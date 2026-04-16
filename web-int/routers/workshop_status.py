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
        "cached": False,
    }


def request_recalculate(host: str) -> Optional[str]:
    url = f"https://{host}:13015/labstatus/recalculate"
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.post(url, timeout=20, verify=False, proxies={"http": None, "https": None})
            response.raise_for_status()
            return None
        except requests.RequestException as exc:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s exponential backoff
                time.sleep(wait_time)
            else:
                return str(exc)
    return None


def request_labstatus(host: str):
    url = f"https://{host}:13015/api/labstatus"
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=25, verify=False, proxies={"http": None, "https": None})
            response.raise_for_status()
            data = response.json()
            # Ensure sum_state exists and is an integer
            if isinstance(data, dict) and "sum_state" in data:
                return data, None
            return None, "Invalid labstatus response: missing sum_state"
        except requests.RequestException as exc:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s exponential backoff
                time.sleep(wait_time)
            else:
                return None, str(exc)
        except ValueError as exc:
            return None, f"Invalid JSON response: {str(exc)}"
    return None, "Failed after retries"


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
    with job_lock:
        if job_state["hosts"]:
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
    entry["cached"] = data.get("cached", False)
    entry["done"] = True
    entry["match"] = entry["sum_state"] == entry["inventory_sum_state"] if entry["inventory_sum_state"] is not None else None
    entry["message"] = "Completed"


def save_dashboard_state(host_entries, hosts_text: str, status_message: str):
    last_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    inventory_sum_state = get_inventory_sum_state()

    with job_lock:
        job_state.update({
            "running": False,
            "progress": 100,
            "message": status_message,
            "hosts": [host.copy() for host in host_entries],
            "hosts_text": hosts_text,
            "last_run": last_run,
            "inventory_sum_state": inventory_sum_state,
            "poll_count": len(host_entries),
            "error": None,
        })

    save_cache({
        "last_run": last_run,
        "hosts": [host.copy() for host in host_entries],
        "hosts_text": hosts_text,
        "inventory_sum_state": inventory_sum_state,
        "poll_count": len(host_entries),
    })


def run_recalculate_for_hosts(hosts: List[str], hosts_text: str):
    inventory_sum_state = get_inventory_sum_state()
    host_entries = [make_host_entry(host, inventory_sum_state) for host in hosts]
    total_hosts = len(host_entries)
    
    set_job_state(running=True, progress=0, message="Starting recalculate...", error=None)

    for index, entry in enumerate(host_entries):
        error = request_recalculate(entry["host"])
        if error:
            entry["message"] = f"Recalculate failed: {error}"
            entry["error"] = error
        else:
            entry["message"] = "Recalculate requested"
        
        # Update progress
        progress = int(((index + 1) / total_hosts) * 100)
        status_msg = f"Processing host {index + 1} of {total_hosts}: {entry['host']}"
        set_job_state(progress=progress, message=status_msg, hosts=host_entries.copy())
        
        # Small delay between requests to avoid overwhelming remote server
        if index < len(host_entries) - 1:
            time.sleep(0.5)

    save_dashboard_state(host_entries, hosts_text, "Recalculate complete")
    return host_entries


def read_results_for_hosts(hosts: List[str], hosts_text: str):
    inventory_sum_state = get_inventory_sum_state()
    host_entries = [make_host_entry(host, inventory_sum_state) for host in hosts]
    total_hosts = len(host_entries)
    
    set_job_state(running=True, progress=0, message="Starting results read...", error=None)

    for index, entry in enumerate(host_entries):
        data, error = request_labstatus(entry["host"])
        if error:
            entry["message"] = f"Read failed: {error}"
            entry["error"] = error
            # Continue to next host, but wait a bit before retrying
            if index < len(host_entries) - 1:
                time.sleep(0.5)
        else:
            if isinstance(data, dict) and data.get("sum_state") is not None:
                update_host_results(host_entries, index, data)
                entry["message"] = "Results loaded"
            else:
                entry["message"] = "Invalid labstatus response"
            
            # Small delay between requests to avoid overwhelming remote server
            if index < len(host_entries) - 1:
                time.sleep(0.5)
        
        # Update progress
        progress = int(((index + 1) / total_hosts) * 100)
        status_msg = f"Processing host {index + 1} of {total_hosts}: {entry['host']}"
        set_job_state(progress=progress, message=status_msg, hosts=host_entries.copy())

    save_dashboard_state(host_entries, hosts_text, "Results loaded")
    return host_entries


def render_host_row(entry):
    host_name = html.escape(entry["host"])
    sum_state = html.escape(str(entry["sum_state"])) if entry.get("sum_state") is not None else "pending"
    comparison = "Match" if entry.get("match") else "Mismatch"
    comparison_color = "#d4edda" if entry.get("match") else "#f8d7da"
    open_url = f"https://{html.escape(entry['host'])}:13015/labstatus"
    status_text = html.escape(entry.get("message", "Pending"))
    finished = "Yes" if entry.get("cached") else "No"

    return f"""
        <tr style=\"background:{comparison_color};\">
            <td>{host_name}</td>
            <td>{sum_state}</td>
            <td>{comparison}</td>
            <td><a class=\"button-link\" href=\"{open_url}\" target=\"_blank\">Open</a></td>
            <td>{status_text}</td>
            <td>{finished}</td>
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

    # Calculate summary counts
    hosts = data.get("hosts", [])
    good_instances = sum(1 for h in hosts if h.get("match") is True)
    pending = sum(1 for h in hosts if h.get("sum_state") is None)
    bad_instances = sum(1 for h in hosts if h.get("sum_state") is not None and h.get("match") is False)

    inventory_sum = html.escape(str(data.get("inventory_sum_state", "N/A")))
    last_run = html.escape(str(data.get("last_run") or "None"))
    host_count = len(data.get("hosts", []))
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
            .btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
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
            .progress-section {{ display: none; }}
            .progress-section.active {{ display: block; }}
            .progress-bar-container {{ background: #e9ecef; border-radius: 10px; height: 30px; overflow: hidden; margin-bottom: 16px; }}
            .progress-bar-fill {{ background: linear-gradient(90deg, #1677ff, #0d47a1); height: 100%; width: 0%; transition: width 0.3s ease; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; color: white; }}
            .progress-text {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 14px; }}
            .progress-message {{ color: #495057; margin-top: 8px; font-size: 13px; }}
            @media (max-width: 900px) {{ .status-card {{ grid-template-columns: 1fr; }} }}
        </style>
    </head>
    <body>
        <h1>Workshop Status</h1>
        <div class="section">
            <form id="run-form" action="/workshop_status/action" method="post">
                <label for="hosts"><strong>Enter up to 100 IPs or FQDNs</strong> (one per line or comma separated)</label>
                <textarea id="hosts" name="hosts" maxlength="8000" placeholder="10.254.1.6\nlab1.example.com\nlab2.example.com">{html.escape(data.get('hosts_text', ''))}</textarea>
                <div class="status-row">
                    <button class="btn primary" type="submit" name="action" value="run" id="run-btn">Run</button>
                    <button class="btn secondary" type="submit" name="action" value="read_results" id="read-btn">Read Results</button>
                    <a class="button-link" href="/">Back to Home</a>
                </div>
            </form>
            {f'<div class="notice success">{message_text}</div>' if message_text else ''}
            {f'<div class="notice error">{error_text}</div>' if error_text else ''}
        </div>

        <div class="section progress-section" id="progress-section">
            <h2>Operation Progress</h2>
            <div class="progress-text">
                <span id="progress-label">Starting...</span>
                <span id="progress-percentage">0%</span>
            </div>
            <div class="progress-bar-container">
                <div class="progress-bar-fill" id="progress-bar-fill" style="width: 0%;"></div>
            </div>
            <div class="progress-message" id="progress-message">Initializing operation...</div>
        </div>

        <div class="section">
            <div class="status-card">
                <div class="box"><strong>Last run</strong><div>{last_run}</div></div>
                <div class="box"><strong>Hosts configured</strong><div>{host_count}</div></div>
                <div class="box"><strong>Inventory sum_state</strong><div>{inventory_sum}</div></div>
                <div class="box"><strong>Good Instances</strong><div>{good_instances}</div></div>
                <div class="box"><strong>Pending</strong><div>{pending}</div></div>
                <div class="box"><strong>Bad Instances</strong><div>{bad_instances}</div></div>
            </div>

            <table>
                <thead>
                    <tr><th>Host</th><th>Host sum_state</th><th>Result</th><th>Open Labstatus</th><th>Message</th><th>Finished</th></tr>
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
            let pollInterval = null;
            let isPolling = false;

            async function pollStatus() {{
                try {{
                    const response = await fetch('/api/workshop_status/status');
                    const status = await response.json();
                    
                    if (status.running) {{
                        document.getElementById('progress-section').classList.add('active');
                        document.getElementById('progress-bar-fill').style.width = status.progress + '%';
                        document.getElementById('progress-percentage').textContent = status.progress + '%';
                        document.getElementById('progress-label').textContent = 'Completed: ' + status.completed_count + ' of ' + status.host_count + ' hosts';
                        document.getElementById('progress-message').textContent = status.message;
                        document.getElementById('run-btn').disabled = true;
                        document.getElementById('read-btn').disabled = true;
                    }} else {{
                        if (isPolling) {{
                            document.getElementById('progress-section').classList.remove('active');
                            document.getElementById('run-btn').disabled = false;
                            document.getElementById('read-btn').disabled = false;
                            stopPolling();
                            setTimeout(() => {{
                                location.reload();
                            }}, 1000);
                        }}
                    }}
                }} catch (error) {{
                    console.error('Error polling status:', error);
                }}
            }}

            function startPolling() {{
                isPolling = true;
                pollStatus();
                pollInterval = setInterval(pollStatus, 2000);
            }}

            function stopPolling() {{
                isPolling = false;
                if (pollInterval) {{
                    clearInterval(pollInterval);
                    pollInterval = null;
                }}
            }}

            document.getElementById('run-form').addEventListener('submit', (e) => {{
                startPolling();
            }});

            // Check if operation is running on page load
            fetch('/api/workshop_status/status')
                .then(response => response.json())
                .then(status => {{
                    if (status.running) {{
                        startPolling();
                    }}
                }})
                .catch(error => console.error('Error on load:', error));
        </script>
    </body>
    </html>
    """


@router.get("/workshop_status", response_class=HTMLResponse)
def workshop_status():
    return HTMLResponse(render_dashboard_page())


@router.post("/workshop_status/action", response_class=HTMLResponse)
def workshop_status_action(hosts: str = Form(...), action: str = Form(...)):
    parsed_hosts = parse_hosts(hosts)
    if not parsed_hosts:
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message="Enter at least one valid host, up to 100 hosts."))

    if len(parsed_hosts) > MAX_HOSTS:
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message=f"Maximum allowed hosts is {MAX_HOSTS}."))

    if action == "run":
        thread = threading.Thread(target=run_recalculate_for_hosts, args=(parsed_hosts, hosts), daemon=True)
        thread.start()
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, message=f"Recalculate requested for {len(parsed_hosts)} host(s)."))

    if action == "read_results":
        thread = threading.Thread(target=read_results_for_hosts, args=(parsed_hosts, hosts), daemon=True)
        thread.start()
        return HTMLResponse(render_dashboard_page(hosts_text=hosts, message=f"Results loaded for {len(parsed_hosts)} host(s)."))

    return HTMLResponse(render_dashboard_page(hosts_text=hosts, error_message="Unknown action."))


@router.get("/api/workshop_status/status")
def workshop_status_status():
    return JSONResponse(current_status())


@router.get("/api/workshop_status/data")
def workshop_status_data():
    return JSONResponse(current_data())

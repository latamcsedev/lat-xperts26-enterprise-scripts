import html
import json
import os
import paramiko
import re
import threading
import time

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from utils import api_delete, api_get, api_post, load_inventory

router = APIRouter()
CACHE_PATH = "labstatus_cache.json"
PARTIAL_CACHE_PATH = "labstatus_partial.json"
REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
}

job_lock = threading.Lock()
job_state = {
    "running": False,
    "progress": 0,
    "phase": "idle",
    "message": "Idle",
    "error": None,
}


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


def save_partial_cache(data):
    """Save partial results during refresh for real-time display."""
    with open(PARTIAL_CACHE_PATH, "w") as cache_file:
        json.dump(data, cache_file, indent=2)


def load_partial_cache():
    """Load partial results if refresh is in progress."""
    if not os.path.exists(PARTIAL_CACHE_PATH):
        return None
    try:
        with open(PARTIAL_CACHE_PATH, "r") as cache_file:
            return json.load(cache_file)
    except Exception:
        return None


def clear_partial_cache():
    """Clear partial cache when refresh completes."""
    if os.path.exists(PARTIAL_CACHE_PATH):
        try:
            os.remove(PARTIAL_CACHE_PATH)
        except Exception:
            pass


def get_runtime_device_map():
    try:
        response = api_get("/api/v1/runtime/device")
        objects = response.get("object", [])
        return {obj.get("name"): obj.get("id") for obj in objects if obj.get("name") and obj.get("id")}
    except Exception:
        return {}


def clean_license_output(output):
    if output is None:
        return None
    output = output.strip()
    if not output:
        return None

    lines = []
    prompt_start = re.compile(r"^[A-Za-z0-9_.-]+(?:VMSTM|VM|EXT|80)?\s*#\s*", re.IGNORECASE)
    prompt_end = re.compile(r"\s+[A-Za-z0-9_.-]+(?:VMSTM|VM|EXT|80)?\s*#\s*$", re.IGNORECASE)

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = prompt_start.sub("", stripped)
        stripped = prompt_end.sub("", stripped)
        if stripped:
            lines.append(stripped)

    cleaned = " ".join(lines).strip()
    return cleaned or None


def get_license_status(host, username, password, timeout=15):
    def try_login(passwd, ssh_timeout=10):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(hostname=host, username=username, password=passwd, timeout=ssh_timeout)
        except (paramiko.ssh_exception.NoValidConnectionsError, paramiko.ssh_exception.AuthenticationException, TimeoutError) as e:
            raise e
        return ssh

    ssh = None
    start_time = time.time()
    try:
        try:
            ssh = try_login(password, ssh_timeout=10)
        except Exception as e:
            # Try with empty password if first attempt fails
            if time.time() - start_time > timeout:
                return {"status": "ssh timeout", "output": None}
            try:
                ssh = try_login("", ssh_timeout=8)
                shell = ssh.invoke_shell(timeout=5)
                time.sleep(0.5)
                output = shell.recv(5000).decode(errors="ignore")
                if "change your password" in output.lower():
                    shell.send(password + "\n")
                    time.sleep(0.5)
                    shell.send(password + "\n")
                    time.sleep(1)
                ssh.close()
                ssh = try_login(password, ssh_timeout=8)
            except Exception:
                raise e

        # Set timeout for command execution
        if time.time() - start_time > timeout:
            return {"status": "ssh timeout", "output": None}
        time.sleep(2)
        stdin, stdout, stderr = ssh.exec_command("get system status\n", timeout=10)
        time.sleep(2)
        output = stdout.read().decode(errors="ignore") + stderr.read().decode(errors="ignore")
        output = clean_license_output(output)

        ssh.close()
    except Exception as exc:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        error_msg = str(exc)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {"status": "ssh timeout", "output": error_msg}
        return {"status": f"ssh error: {error_msg[:60]}", "output": error_msg}
    
    with open('/root/log.txt','a+') as f:
        f.write(output)
    license_match = re.search(r"(Valid|Warning|Expired|Invalid|Unknown|Error)", output, re.IGNORECASE)
    return {"status": license_match.group(1).lower() if license_match else "unknown", "output": output.strip() or None}

def normalize_power_status(status):
    if not status:
        return "unknown"
    status = status.lower().strip()
    if status == "running":
        return "power-on"
    if status in ["power-off", "stopped", "shutdown", "shut off", "shut-off", "powered off"]:
        return "power-off"
    return status


def refresh_lab_status():
    set_job_state(True, 0, "starting", "Starting lab validation refresh")
    inventory = load_inventory()
    device_map = get_runtime_device_map()
    power_results = []
    license_results = []
    sum_state = 0

    powercheck = inventory.get("powercheck", {})
    licensecheck = inventory.get("licensecheck", {})
    total_steps = max(1, len(powercheck or {}) + len(licensecheck or {}) + 1)
    current_step = 0

    try:
        for name, config in (powercheck or {}).items():
            expected_state = str(config.get("expected_state", "")).lower().strip()
            row = {
                "name": name,
                "expected_state": expected_state,
                "status": "unknown",
                "device_id": None,
                "ok": False,
                "action": None,
                "action_label": None,
                "message": None,
            }

            device_id = device_map.get(name)
            if not device_id:
                row["status"] = "not found"
                row["message"] = "Device not present in runtime backend"
                power_results.append(row)
                current_step += 1
                set_job_state(True, int(current_step / total_steps * 100), "power", f"Checked power device {name}")
                save_partial_cache({"last_run": None, "sum_state": sum_state, "power_results": power_results, "license_results": license_results})
                continue

            row["device_id"] = device_id
            try:
                response = api_get(f"/api/v1/runtime/vm/{device_id}/status")
                status = response.get("object", {}).get("status", "unknown")
                row["status"] = status
            except Exception as exc:
                row["status"] = f"error: {str(exc)[:120]}"
                power_results.append(row)
                current_step += 1
                set_job_state(True, int(current_step / total_steps * 100), "power", f"Checked power device {name}")
                save_partial_cache({"last_run": None, "sum_state": sum_state, "power_results": power_results, "license_results": license_results})
                continue

            normalized = normalize_power_status(row["status"])
            row["ok"] = normalized == expected_state
            if row["ok"]:
                sum_state += 1

            if row["status"].lower() == "running":
                row["action"] = "power-off"
                row["action_label"] = "Power Off"
            else:
                row["action"] = "power-on"
                row["action_label"] = "Power On"

            power_results.append(row)
            current_step += 1
            set_job_state(True, int(current_step / total_steps * 100), "power", f"Checked power device {name}")
            save_partial_cache({"last_run": None, "sum_state": sum_state, "power_results": power_results, "license_results": license_results})

        username = inventory.get("fgt_user")
        password = inventory.get("fgt_password")
        for name, config in (licensecheck or {}).items():
            ip = config.get("ip") if isinstance(config, dict) else None
            row = {
                "name": name,
                "ip": ip,
                "status": "unknown",
                "license_output": None,
                "device_id": device_map.get(name),
                "ok": False,
                "color": "red",
                "action": "reinstall",
                "action_label": "Reinstall",
                "message": None,
            }

            if not ip:
                row["status"] = "no ip configured"
                row["message"] = "License check entry missing IP"
                license_results.append(row)
                current_step += 1
                set_job_state(True, int(current_step / total_steps * 100), "license", f"Checked license device {name}")
                continue

            if not username or not password:
                row["status"] = "no credentials"
                row["message"] = "Inventory missing fgt_user or fgt_password"
                license_results.append(row)
                current_step += 1
                set_job_state(True, int(current_step / total_steps * 100), "license", f"Checked license device {name}")
                continue

            license_status = get_license_status(ip, username, password)
            parsed_status = license_status.get("status") if isinstance(license_status, dict) else license_status
            raw_output = license_status.get("output") if isinstance(license_status, dict) else None
            row["status"] = raw_output or parsed_status or "unknown"
            row["license_output"] = parsed_status
            if parsed_status == "valid":
                row["ok"] = True
                row["color"] = "green"
                sum_state += 1
            elif parsed_status == "warning":
                row["ok"] = True
                row["color"] = "yellow"
                sum_state += 1
            else:
                row["ok"] = False
                row["color"] = "red"

            license_results.append(row)
            current_step += 1
            set_job_state(True, int(current_step / total_steps * 100), "license", f"Checked license device {name}")
            save_partial_cache({"last_run": None, "sum_state": sum_state, "power_results": power_results, "license_results": license_results})

        result = {
            "last_run": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "sum_state": sum_state,
            "power_results": power_results,
            "license_results": license_results,
        }

        save_cache(result)
        clear_partial_cache()
        set_job_state(False, 100, "complete", "Lab validation refresh complete")
        return result
    except Exception as exc:
        set_job_state(False, job_state["progress"], "error", f"Refresh failed: {str(exc)}", str(exc))
        raise


def start_refresh_in_background():
    with job_lock:
        if job_state["running"]:
            return False
        job_state["running"] = True
        job_state["progress"] = 0
        job_state["phase"] = "queued"
        job_state["message"] = "Queued for refresh"
        job_state["error"] = None

    thread = threading.Thread(target=refresh_lab_status, daemon=True)
    thread.start()
    return True


def set_job_state(running, progress, phase, message, error=None):
    with job_lock:
        job_state["running"] = running
        job_state["progress"] = progress
        job_state["phase"] = phase
        job_state["message"] = message
        job_state["error"] = error


def load_or_refresh_lab_status():
    # If refresh is running, show partial results
    with job_lock:
        if job_state["running"]:
            partial = load_partial_cache()
            if partial:
                return partial
    
    # Otherwise return cached or empty results
    cached = load_cache()
    if cached:
        return cached
    return {
        "last_run": None,
        "sum_state": 0,
        "power_results": [],
        "license_results": [],
    }


def render_lab_status_page(data):
    power_rows = []
    for row in data["power_results"]:
        color = "#e8f5e9" if row.get("ok") else "#fdecea"
        status_text = html.escape(row.get("status", "unknown"))
        expected_text = html.escape(row.get("expected_state", ""))
        action_html = ""
        if row.get("device_id") and row.get("action"):
            action_html = f'''
                <form action="/labstatus/{row['device_id']}/power/{row['action']}" method="post" style="display:inline; margin:0;">
                    <button class="btn {'danger' if row['action'] == 'power-off' else 'primary'}" type="submit">{html.escape(row['action_label'])}</button>
                </form>
            '''
        else:
            action_html = '<span class="muted">Action unavailable</span>'

        power_rows.append(f'''
            <tr style="background:{color};">
                <td>{html.escape(row['name'])}</td>
                <td>{status_text}</td>
                <td>{expected_text}</td>
                <td>{action_html}</td>
            </tr>
        ''')

    license_rows = []
    for row in data["license_results"]:
        color = "#e8f5e9" if row.get("color") == "green" else "#fff8e1" if row.get("color") == "yellow" else "#fdecea"
        status_text = html.escape(str(row.get("status", "unknown")))
        action_html = ""
        if row.get("device_id"):
            action_html = f'''
                <form action="/labstatus/{row['device_id']}/reinstall" method="post" style="display:inline; margin:0;">
                    <button class="btn secondary" type="submit">{html.escape(row['action_label'])}</button>
                </form>
            '''
        else:
            action_html = '<span class="muted">No runtime ID</span>'

        license_rows.append(f'''
            <tr style="background:{color};">
                <td>{html.escape(row['name'])}</td>
                <td>{html.escape(str(row.get('ip', '')))}</td>
                <td>{status_text}</td>
                <td>{action_html}</td>
            </tr>
        ''')

    cache_note = html.escape(data.get('last_run') and 'Cached results available' or 'No cached results yet')
    last_run = html.escape(data.get('last_run') or 'N/A')
    sum_state_str = str(data.get('sum_state', 0))
    power_html = ''.join(power_rows)
    license_html = ''.join(license_rows)
    cache_bool = 'true' if data.get('last_run') else 'false'

    return (
        """
    <html>
    <head>
        <title>Lab Validation Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; background: #f4f6f9; padding: 30px; }
            h1 { margin-bottom: 10px; }
            .summary { margin-bottom: 30px; padding: 20px; background: white; border-radius: 12px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); }
            .btn { padding: 10px 16px; border-radius: 6px; border: none; cursor: pointer; color: white; font-size: 14px; font-weight: 600; }
            .primary { background: #1677ff; }
            .secondary { background: #6c757d; }
            .danger { background: #dc3545; }
            .muted { color: #6c757d; font-size: 13px; }
            .table-card { background: white; border-radius: 12px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); overflow: hidden; margin-bottom: 30px; }
            table { border-collapse: collapse; width: 100%; min-width: 720px; }
            th, td { padding: 14px 16px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #f8f9fa; color: #333; }
            td { vertical-align: middle; }
            .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
            a.button-link { text-decoration: none; display: inline-block; background: #495057; color: white; padding: 10px 16px; border-radius: 6px; }
            .progress-container { margin-top: 20px; max-width: 720px; }
            .progress-bar { background: #e9ecef; border-radius: 999px; overflow: hidden; height: 18px; margin-top: 8px; }
            .progress-fill { background: #1677ff; height: 100%; width: 0%; transition: width 0.3s ease; }
            .progress-meta { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 14px; color: #333; }
        </style>
    </head>
    <body>
        <h1>Lab Validation Dashboard</h1>
        <div class="summary">
            <div class="actions">
                <button id="recalc-button" class="btn primary" type="button">Recalculate Status</button>
                <a class="button-link" href="/">Back to Home</a>
            </div>
            <div class="progress-container">
                <div><strong>Progress:</strong> <span id="status-message">Idle</span></div>
                <div class="progress-bar"><div id="progress-fill" class="progress-fill"></div></div>
                <div class="progress-meta"><span id="progress-percent">0%</span><span id="cache-note">"""
    + cache_note +
    """</span></div>
            </div>
            <p><strong>Last run:</strong> """
    + last_run +
    """</p>
            <p><strong>sum_state:</strong> """
    + sum_state_str +
    """</p>
        </div>

        <div class="table-card">
            <table>
                <thead>
                    <tr><th>Power Device</th><th>Current State</th><th>Expected State</th><th>Action</th></tr>
                </thead>
                <tbody>
                    """
    + power_html +
    """
                </tbody>
            </table>
        </div>

        <div class="table-card">
            <table>
                <thead>
                    <tr><th>License Device</th><th>IP</th><th>License Status</th><th>Action</th></tr>
                </thead>
                <tbody>
                    """
    + license_html +
    """
                </tbody>
            </table>
        </div>

        <script>
            console.log('Lab validation page loaded');
            const cacheExists = """
    + cache_bool +
    """;
            console.log('Cache exists:', cacheExists);
            const progressFill = document.getElementById('progress-fill');
            const progressPercent = document.getElementById('progress-percent');
            const statusMessage = document.getElementById('status-message');
            const recalcButton = document.getElementById('recalc-button');
            let refreshStarted = false;

            console.log('Elements found:', {
                progressFill: !!progressFill,
                progressPercent: !!progressPercent,
                statusMessage: !!statusMessage,
                recalcButton: !!recalcButton
            });

            function updateProgress(state) {
                console.log('Updating progress:', state);
                const value = state.progress || 0;
                progressFill.style.width = value + '%';
                progressPercent.textContent = value + '%';
                statusMessage.textContent = state.message || 'Idle';
                if (state.running) {
                    recalcButton.disabled = true;
                    recalcButton.textContent = 'Running...';
                } else {
                    recalcButton.disabled = false;
                    recalcButton.textContent = 'Recalculate Status';
                }
            }

            function reloadTableData() {
                console.log('Fetching updated table data...');
                fetch('/api/labstatus')
                    .then(response => response.json())
                    .then(data => {
                        console.log('Updated data:', data);
                        // Update sum_state
                        document.querySelector('[data-key="sum-state"]')?.innerText || (document.querySelectorAll('p')[2].innerText = 'sum_state: ' + data.sum_state);
                        // Reload page to show new tables
                        window.location.reload();
                    })
                    .catch(error => console.error('Failed to reload table data:', error));
            }

            function pollStatus() {
                console.log('Polling status...');
                fetch('/labstatus/status')
                    .then(response => {
                        console.log('Status response:', response.status);
                        return response.json();
                    })
                    .then(state => {
                        console.log('Status data:', state);
                        updateProgress(state);
                        if (state.running) {
                            // While running, fetch updated table data every 3 seconds
                            fetch('/api/labstatus')
                                .then(r => r.json())
                                .then(data => {
                                    if (data.power_results && data.power_results.length > 0) {
                                        console.log('Partial results available:', data.power_results.length, 'power,', data.license_results.length, 'license');
                                        // Reload to show partial data
                                        window.location.reload();
                                    }
                                })
                                .catch(e => console.error('Error fetching partial data:', e));
                            setTimeout(pollStatus, 2000);
                        } else if (refreshStarted && state.progress >= 100) {
                            console.log('Refresh complete, reloading page');
                            window.location.reload();
                        }
                    })
                    .catch(error => {
                        console.error('Poll status error:', error);
                        statusMessage.textContent = 'Unable to poll status';
                        recalcButton.disabled = false;
                    });
            }

            function startRefresh() {
                console.log('Starting refresh...');
                refreshStarted = true;
                recalcButton.disabled = true;
                recalcButton.textContent = 'Starting...';
                fetch('/labstatus/recalculate', { method: 'POST' })
                    .then(response => {
                        console.log('Recalculate response:', response.status);
                        return response.json();
                    })
                    .then(data => {
                        console.log('Recalculate data:', data);
                        pollStatus();
                    })
                    .catch(error => {
                        console.error('Start refresh error:', error);
                        statusMessage.textContent = 'Failed to start refresh';
                        recalcButton.disabled = false;
                        recalcButton.textContent = 'Recalculate Status';
                    });
            }

            console.log('Setting up event listeners...');
            recalcButton.addEventListener('click', startRefresh);
            console.log('Starting initial poll...');
            pollStatus();
            if (!cacheExists) {
                console.log('No cache, auto-starting refresh...');
                startRefresh();
            }
        </script>
    </body>
    </html>
    """)


@router.get("/labstatus", response_class=HTMLResponse)
def labstatus_page():
    try:
        data = load_or_refresh_lab_status()
        return HTMLResponse(render_lab_status_page(data))
    except Exception as exc:
        import traceback
        error_html = f"""
        <html>
        <head><title>Error</title></head>
        <body>
            <h1>Internal Server Error</h1>
            <p>An error occurred while rendering the lab status page:</p>
            <pre>{html.escape(str(exc))}</pre>
            <h2>Traceback:</h2>
            <pre>{html.escape(traceback.format_exc())}</pre>
            <p><a href="/labstatus/debug">View Debug Info</a></p>
        </body>
        </html>
        """
        return HTMLResponse(error_html, status_code=500)



@router.get("/labstatus/status")
def labstatus_status():
    with job_lock:
        return JSONResponse(content={
            "running": job_state["running"],
            "progress": job_state["progress"],
            "phase": job_state["phase"],
            "message": job_state["message"],
            "error": job_state["error"],
        })


@router.get("/api/labstatus")
def labstatus_api():
    data = load_or_refresh_lab_status()
    result = {**data, "cached": bool(data.get("last_run"))}
    return JSONResponse(content=result)


@router.post("/labstatus/recalculate")
def labstatus_recalculate():
    started = start_refresh_in_background()
    if not started:
        return JSONResponse({"started": False, "message": "Refresh already running."}, status_code=202)
    return JSONResponse({"started": True, "message": "Refresh started."}, status_code=202)


@router.post("/labstatus/{device_id}/power/{action}")
def labstatus_power_action(device_id: str, action: str):
    if action not in ["power-on", "power-off"]:
        return HTMLResponse("Invalid power action", status_code=400)

    payload = None
    if action == "power-on":
        payload = {
            "configuration": True,
            "license": True,
            "post_boot": True,
            "timeout": 0,
        }

    try:
        api_post(f"/api/v1/runtime/vm/{device_id}:{action}", payload)
    except Exception:
        pass

    refresh_lab_status()
    return RedirectResponse(url="/labstatus", status_code=303)


@router.post("/labstatus/{device_id}/reinstall")
def labstatus_reinstall(device_id: str):
    """Reinstall a device: delete, wait 30s, then reinstall."""
    try:
        # Delete the device
        api_delete(f"/api/v1/runtime/device/{device_id}")
        # Wait 30 seconds
        time.sleep(30)
        # Reinstall the device
        api_post(f"/api/v1/runtime/device/{device_id}", REINSTALL_PAYLOAD)
    except Exception as exc:
        print(f"Reinstall error for device {device_id}: {exc}")
        pass

    # Refresh lab status after reinstall
    refresh_lab_status()
    return RedirectResponse(url="/labstatus", status_code=303)



@router.get("/labstatus/debug")
def labstatus_debug():
    with job_lock:
        return JSONResponse(content={
            "job_state": job_state,
            "cache_exists": os.path.exists(CACHE_PATH),
            "cache_size": os.path.getsize(CACHE_PATH) if os.path.exists(CACHE_PATH) else 0,
        })


@router.get("/labstatus/test", response_class=HTMLResponse)
def labstatus_test():
    return HTMLResponse("""
    <html>
    <head><title>Test</title></head>
    <body>
        <h1>Lab Validation Router Test</h1>
        <p>If you can see this, the router is working!</p>
        <p>Try these endpoints:</p>
        <ul>
            <li><a href="/labstatus">/labstatus</a> - Main dashboard</li>
            <li><a href="/api/labstatus">/api/labstatus</a> - JSON API</li>
            <li><a href="/labstatus/status">/labstatus/status</a> - Status endpoint</li>
        </ul>
    </body>
    </html>
    """)


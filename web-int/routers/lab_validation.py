import html
import json
import os
import paramiko
import re
import time

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from utils import api_delete, api_get, api_post, load_inventory

router = APIRouter()
CACHE_PATH = "labstatus_cache.json"
REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
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


def get_runtime_device_map():
    try:
        response = api_get("/api/v1/runtime/device")
        objects = response.get("object", [])
        return {obj.get("name"): obj.get("id") for obj in objects if obj.get("name") and obj.get("id")}
    except Exception:
        return {}


def get_license_status(host, username, password):
    def try_login(passwd):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=username, password=passwd, timeout=5)
        return ssh

    ssh = None
    try:
        try:
            ssh = try_login(password)
        except Exception:
            ssh = try_login("")
            shell = ssh.invoke_shell()
            time.sleep(1)
            output = shell.recv(5000).decode(errors="ignore")
            if "change your password" in output.lower():
                shell.send(password + "\n")
                time.sleep(1)
                shell.send(password + "\n")
                time.sleep(2)
            ssh.close()
            ssh = try_login(password)

        stdin, stdout, stderr = ssh.exec_command("get system status")
        output = stdout.read().decode(errors="ignore") + stderr.read().decode(errors="ignore")
        ssh.close()
    except Exception as exc:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        return f"ssh error: {str(exc)}"

    license_match = re.search(r"(valid|warning|expired|invalid|unknown|error)", output, re.IGNORECASE)
    return license_match.group(1).lower() if license_match else "unknown"


def normalize_power_status(status):
    if not status:
        return "unknown"
    status = status.lower()
    if status == "running":
        return "power-on"
    if status in ["power-off", "stopped", "shutdown"]:
        return "power-off"
    return status


def refresh_lab_status():
    inventory = load_inventory()
    device_map = get_runtime_device_map()
    power_results = []
    license_results = []
    sum_state = 0

    powercheck = inventory.get("powercheck", {})
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
            continue

        row["device_id"] = device_id
        try:
            response = api_get(f"/api/v1/runtime/vm/{device_id}/status")
            status = response.get("object", {}).get("status", "unknown")
            row["status"] = status
        except Exception as exc:
            row["status"] = f"error: {str(exc)[:120]}"
            power_results.append(row)
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

    licensecheck = inventory.get("licensecheck", {})
    username = inventory.get("fgt_user")
    password = inventory.get("fgt_password")
    for name, config in (licensecheck or {}).items():
        ip = config.get("ip") if isinstance(config, dict) else None
        row = {
            "name": name,
            "ip": ip,
            "status": "unknown",
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
            continue

        if not username or not password:
            row["status"] = "no credentials"
            row["message"] = "Inventory missing fgt_user or fgt_password"
            license_results.append(row)
            continue

        status = get_license_status(ip, username, password)
        row["status"] = status
        if status == "valid":
            row["ok"] = True
            row["color"] = "green"
            sum_state += 1
        elif status == "warning":
            row["ok"] = True
            row["color"] = "yellow"
            sum_state += 1
        else:
            row["ok"] = False
            row["color"] = "red"

        license_results.append(row)

    result = {
        "last_run": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "sum_state": sum_state,
        "power_results": power_results,
        "license_results": license_results,
    }

    save_cache(result)
    return result


def load_or_refresh_lab_status():
    cached = load_cache()
    if cached:
        return cached
    return refresh_lab_status()


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
        status_text = html.escape(row.get("status", "unknown"))
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

    return f"""
    <html>
    <head>
        <title>Lab Validation Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 30px; }}
            h1 {{ margin-bottom: 10px; }}
            .summary {{ margin-bottom: 30px; padding: 20px; background: white; border-radius: 12px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); }}
            .btn {{ padding: 10px 16px; border-radius: 6px; border: none; cursor: pointer; color: white; font-size: 14px; font-weight: 600; }}
            .primary {{ background: #1677ff; }}
            .secondary {{ background: #6c757d; }}
            .danger {{ background: #dc3545; }}
            .muted {{ color: #6c757d; font-size: 13px; }}
            .table-card {{ background: white; border-radius: 12px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); overflow: hidden; margin-bottom: 30px; }}
            table {{ border-collapse: collapse; width: 100%; min-width: 720px; }}
            th, td {{ padding: 14px 16px; text-align: left; border-bottom: 1px solid #eee; }}
            th {{ background: #f8f9fa; color: #333; }}
            td {{ vertical-align: middle; }}
            .actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
            a.button-link {{ text-decoration: none; display: inline-block; background: #495057; color: white; padding: 10px 16px; border-radius: 6px; }}
        </style>
    </head>
    <body>
        <h1>Lab Validation Dashboard</h1>
        <div class="summary">
            <div class="actions">
                <form action="/labstatus/recalculate" method="post" style="margin:0;">
                    <button class="btn primary" type="submit">Recalculate Status</button>
                </form>
                <a class="button-link" href="/">Back to Home</a>
            </div>
            <p><strong>Last run:</strong> {html.escape(data.get('last_run', 'N/A'))}</p>
            <p><strong>sum_state:</strong> {data.get('sum_state', 0)}</p>
        </div>

        <div class="table-card">
            <table>
                <thead>
                    <tr><th>Power Device</th><th>Current State</th><th>Expected State</th><th>Action</th></tr>
                </thead>
                <tbody>
                    {''.join(power_rows)}
                </tbody>
            </table>
        </div>

        <div class="table-card">
            <table>
                <thead>
                    <tr><th>License Device</th><th>IP</th><th>License Status</th><th>Action</th></tr>
                </thead>
                <tbody>
                    {''.join(license_rows)}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """


@router.get("/labstatus", response_class=HTMLResponse)
def labstatus_page():
    data = load_or_refresh_lab_status()
    return HTMLResponse(render_lab_status_page(data))


@router.get("/api/labstatus")
def labstatus_api():
    data = load_or_refresh_lab_status()
    return JSONResponse(content=data)


@router.post("/labstatus/recalculate")
def labstatus_recalculate():
    refresh_lab_status()
    return RedirectResponse(url="/labstatus", status_code=303)


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
    try:
        api_delete(f"/api/v1/runtime/device/{device_id}", params={"delete": "true"})
        time.sleep(30)
        api_post(f"/api/v1/runtime/device/{device_id}", REINSTALL_PAYLOAD)
    except Exception:
        pass

    refresh_lab_status()
    return RedirectResponse(url="/labstatus", status_code=303)

import yaml
import paramiko
import re
import csv
import io
import os
# Traffic Control
import requests
import urllib3
import re
from http.cookiejar import MozillaCookieJar
from fastapi import Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
# End of Traffic Control
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

# Traffic Control
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
templates = Jinja2Templates(directory="templates")
# End of Traffic Control

#################################################
# Home page
#################################################

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
    <head>
        <title>LATAM Automation Portal</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f4f6f9;
                padding: 40px;
            }

            h1 {
                margin-bottom: 30px;
            }

            .grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 25px;
            }

            .section {
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
            }

            .section h2 {
                margin-top: 0;
                font-size: 18px;
                color: #444;
            }

            .btn {
                display: inline-block;
                padding: 12px 18px;
                margin-top: 15px;
                border-radius: 8px;
                border: none;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
            }

            .primary {
                background: #1677ff;
                color: white;
            }

            .secondary {
                background: #6c757d;
                color: white;
            }

            .danger {
                background: #dc3545;
                color: white;
            }

            .btn:hover {
                opacity: 0.9;
            }

            .danger-title {
                color: #dc3545;
            }

            /* Responsive fallback */
            @media (max-width: 900px) {
                .grid {
                    grid-template-columns: 1fr;
                }
            }
        </style>
    </head>
    <body>

    <h1>LATAM Automation Portal</h1>

    <div class="grid">

        <!-- Inventory CSV -->
        <div class="section">
            <h2>Inventory</h2>
            <form action="/download" method="get">
                <button class="btn primary" type="submit">
                    Download Inventory CSV
                </button>
            </form>
        </div>

        <!-- Factory Reset -->
        <div class="section">
            <h2 class="danger-title">Factory Reset</h2>
            <form action="/factoryreset" method="get">
                <button class="btn danger" type="submit">
                    Factory Reset All Sites
                </button>
            </form>
        </div>

        <!-- Traffic Control -->
        <div class="section">
            <h2>Traffic Control</h2>
            <form action="/traffic" method="get">
                <button class="btn primary" type="submit">
                    Traffic Control Dashboard
                </button>
            </form>
        </div>

        <!-- FMG Replacement -->
        <div class="section">
            <h2>FMG Serial Replacement</h2>
            <form action="/replace" method="post">
                <button class="btn secondary" type="submit">
                    Replace Serials on FortiManager
                </button>
            </form>
        </div>

    </div>

    </body>
    </html>
    """

#################################################
# Get FortiGate Inventory and export to CSV
#################################################

def load_inventory():
    with open("inventory.yaml", "r") as f:
        return yaml.safe_load(f)

import time

def get_serial(host, username, password):

    def try_login(passwd):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=username, password=passwd, timeout=5)
        return ssh

    try:
        # First try normal password
        try:
            ssh = try_login(password)
        except Exception:
            # Try blank password
            ssh = try_login("")

            # Handle forced password change
            shell = ssh.invoke_shell()
            time.sleep(1)
            output = shell.recv(5000).decode()

            if "change your password" in output.lower():
                shell.send(password + "\n")
                time.sleep(1)
                shell.send(password + "\n")
                time.sleep(2)

            ssh.close()

            # Reconnect with new password
            ssh = try_login(password)

        # Now run command
        stdin, stdout, stderr = ssh.exec_command("get system status")
        output = stdout.read().decode()
        ssh.close()

        match = re.search(r"Serial-Number:\s+(.*)", output)
        return match.group(1).strip() if match else "Not Found"

    except Exception as e:
        return f"Error: {str(e)}"


@app.get("/download")
def download_csv():

    inventory = load_inventory()

    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "Serial Number",
            "Device Blueprint",
            "Name",
            "vm_interface_number",
            "hostname",
            "loopback",
            "isp1_intf",
            "isp2_intf",
            "mpls_intf",
            "lan_intf",
            "lan_ip",
            "mgmt_ip",
        ],
    )

    writer.writeheader()

    for site_name, site_data in sites.items():

        serial = get_serial(
            site_data.get("ip"),
            fgt_user,
            fgt_password
        )

        writer.writerow({
            "Serial Number": serial,
            "Device Blueprint": site_data.get("blueprint", ""),
            "Name": site_name,
            "vm_interface_number": "10",  # not defined in YAML
            "hostname": site_name,
            "loopback": site_data.get("loopback", ""),
            "isp1_intf": site_data.get("isp1_intf", ""),
            "isp2_intf": site_data.get("isp2_intf", ""),
            "mpls_intf": site_data.get("mpls_intf", ""),
            "lan_intf": site_data.get("lan_intf", ""),
            "lan_ip": site_data.get("lan_ip", ""),
            "mgmt_ip": site_data.get("mgmt_ip", ""),
        })

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=fortigate_inventory.csv"
        },
    )

#################################################
# Fix FMG Serial Numbers after a Backup Restore
#################################################

def replace_serials_on_fmg():

    inventory = load_inventory()

    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")

    fmg_ip = inventory.get("fmg_ip")
    fmg_user = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")

    sites = inventory.get("sites", {})

    results = []

    try:
        # Connect to FortiManager
        fmg_ssh = paramiko.SSHClient()
        fmg_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        fmg_ssh.connect(
            hostname=fmg_ip,
            username=fmg_user,
            password=fmg_password,
            timeout=5
        )

        shell = fmg_ssh.invoke_shell()
        time.sleep(1)

        # Enter global config
        shell.send("config global\n")
        time.sleep(1)

        for site_name, site_data in sites.items():

            serial = get_serial(
                site_data.get("ip"),
                fgt_user,
                fgt_password
            )

            if "Error" in serial:
                results.append(f"{site_name}: Failed to get serial")
                continue

            cmd = f"exec device replace sn {site_name} {serial}\n"
            shell.send(cmd)
            time.sleep(2)

            results.append(f"{site_name}: replaced with {serial}")

        fmg_ssh.close()

    except Exception as e:
        results.append(f"FMG Connection Error: {str(e)}")

    return results

@app.post("/replace", response_class=HTMLResponse)
def replace_devices():

    results = replace_serials_on_fmg()

    result_html = "<br>".join(results)

    return f"""
    <html>
        <body>
            <h3>Replace Results</h3>
            <p>{result_html}</p>
            <br>
            <a href="/">Back</a>
        </body>
    </html>
    """

#################################################
# Factory Reset FGTs
#################################################

def factory_reset_sites():

    inventory = load_inventory()

    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})

    results = []

    for site_name, site_data in sites.items():

        ip = site_data.get("ip")

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=ip,
                username=fgt_user,
                password=fgt_password,
                timeout=5
            )

            shell = ssh.invoke_shell()
            time.sleep(1)

            # Send reset command
            shell.send("execute factoryreset keepvmlicense\n")
            time.sleep(1)

            # Confirm with 'y'
            shell.send("y\n")
            time.sleep(2)

            ssh.close()

            results.append(f"{site_name}: factory reset triggered")

        except Exception as e:
            results.append(f"{site_name}: ERROR - {str(e)}")

    return results

# Page with Confirmation
from fastapi import Form
from fastapi.responses import HTMLResponse

@app.get("/factoryreset", response_class=HTMLResponse)
def factoryreset_confirm():
    return """
    <html>
        <body>
            <h2 style="color:red;">⚠ WARNING ⚠</h2>
            <p>This will factory reset ALL FortiGates and reboot them.</p>
            <p>Type <b>RESET</b> below to confirm:</p>

            <form method="post" action="/factoryreset">
                <input type="text" name="confirmation" />
                <br><br>
                <button type="submit" style="background-color:red; color:white;">
                    Confirm Factory Reset
                </button>
            </form>

            <br>
            <a href="/">Cancel</a>
        </body>
    </html>
    """

@app.post("/factoryreset", response_class=HTMLResponse)
def factoryreset_execute(confirmation: str = Form(...)):

    if confirmation != "RESET":
        return """
        <html>
            <body>
                <h3 style="color:red;">Confirmation failed.</h3>
                <p>You must type RESET exactly to proceed.</p>
                <a href="/factoryreset">Try Again</a>
            </body>
        </html>
        """

    results = factory_reset_sites()

    result_html = "<br>".join(results)

    return f"""
    <html>
        <body>
            <h3>Factory Reset Triggered</h3>
            <p>{result_html}</p>
            <br>
            <a href="/">Back to Home</a>
        </body>
    </html>
    """

#################################################
# Traffic Control
#################################################

## Authentication
FABRIC_HOST = os.getenv("FABRIC_HOST")
CREDENTIAL  = os.getenv("CREDENTIAL")
API_BASE    = f"https://{FABRIC_HOST}"

if not FABRIC_HOST or not CREDENTIAL:
    raise RuntimeError("Missing credentials — ensure /fabric/credentials.env is present and loaded by the service.")


_bearer_token: str | None = None

def get_bearer_token() -> str:
    global _bearer_token
    # Re-use cached token; for production add expiry tracking (expires_in: 36000s = 10h)
    if _bearer_token:
        return _bearer_token

    r = requests.post(
        f"{API_BASE}/oauth2/token/",
        headers={
            "Authorization": f"Basic {CREDENTIAL}",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        verify=False,
    )
    r.raise_for_status()
    _bearer_token = r.json()["access_token"]
    return _bearer_token


def api_get(path: str):
    r = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {get_bearer_token()}"},
        verify=False,
    )
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict):
    r = requests.post(
        f"{API_BASE}{path}",
        json=payload,
        headers={
            "Authorization": f"Bearer {get_bearer_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        verify=False,
    )
    r.raise_for_status()
    return r.json()

## Authentication END

def get_site_names():
    inventory = load_inventory()
    return list(inventory.get("sites", {}).keys())

def get_traffic_devices():

    inventory = load_inventory()
    sites = inventory.get("sites", {})

    resp = api_get("/api/v1/runtime/device")
    objects = resp.get("object", [])

    devices = {}

    for site_name, site_data in sites.items():

        row = next((o for o in objects if o.get("name") == site_name), None)
        if not row:
            continue

        ports = row.get("ports", [])

        target_interfaces = [
            site_data.get("isp1_intf"),
            site_data.get("isp2_intf"),
            site_data.get("mpls_intf"),
        ]

        port_map = {}

        for idx, port_id in enumerate(ports):
            port_label = f"port{idx+1}"
            if port_label in target_interfaces:
                port_map[port_label] = port_id

        devices[site_name] = {
            "id": row["id"],
            "ports": port_map,
            "status": {},
            "cable": {}       # ← new
        }

        for port_label, pid in port_map.items():
            # TC status
            try:
                s = api_get(f"/api/v1/runtime/device/{row['id']}/port/{pid}/tc")
                obj = s.get("object", {})
                devices[site_name]["status"][port_label] = {
                    "delay": obj.get("delay", 0),
                    "loss": obj.get("loss", 0),
                    "corrupt": obj.get("corrupt", 0)
                }
            except Exception:
                devices[site_name]["status"][port_label] = {
                    "delay": 0, "loss": 0, "corrupt": 0
                }

            # Cable state  ← new block
            try:
                cable_resp = api_get(f"/api/v1/runtime/device/cable/{row['id']}/{pid}")
                # API returns a string: "broken" or "repaired" (or similar)
                cable_state = cable_resp if isinstance(cable_resp, str) else cable_resp.get("object", "repaired")
                devices[site_name]["cable"][port_label] = cable_state
            except Exception:
                devices[site_name]["cable"][port_label] = "repaired"

    return devices

@app.get("/traffic", response_class=HTMLResponse)
def traffic_dashboard(request: Request):

    devices = get_traffic_devices()
    metrics = ["delay", "loss", "corrupt"]

    return templates.TemplateResponse(
        "traffic.html",
        {
            "request": request,
            "devices": devices,
            "metrics": metrics
        }
    )

@app.post("/traffic/update/{device}/{port}/{metric}")
def update_traffic(device: str, port: str, metric: str, value: float = Form(...)):

    devices = get_traffic_devices()

    if device not in devices or port not in devices[device]["ports"]:
        return {"error": "Device or port not found"}

    dev = devices[device]
    port_id = dev["ports"][port]
    current = dev["status"][port]

    if metric == "delay" and not (0 <= value <= 500):
        return {"error": "Delay must be 0-500"}

    if metric in ["loss", "corrupt"] and not (0 <= value <= 100):
        return {"error": "Value must be 0-100"}

    if metric in ["loss", "corrupt"]:
        value = value / 100.0

    payload = {
        "object": {
            "id": 0,
            "delay": value if metric == "delay" else current["delay"],
            "loss": value if metric == "loss" else current["loss"],
            "corrupt": value if metric == "corrupt" else current["corrupt"],
            "duplicate": 0,
            "reorder": 0,
            "bandwidth": 0,
            "bucket_size": 15000
        },
        "update_fields": "string",
        "related_fields": ["string"]
    }

    api_post(f"/api/v1/runtime/device/{dev['id']}/port/{port_id}/tc", payload)

    return {"status": "updated"}

@app.post("/traffic/cable/{device}/{port}:break")
def break_cable(device: str, port: str):
    devices = get_traffic_devices()
    if device not in devices or port not in devices[device]["ports"]:
        return {"error": "Device or port not found"}
    dev = devices[device]
    port_id = dev["ports"][port]
    api_post(f"/api/v1/runtime/device/cable/{dev['id']}/{port_id}:break", {})
    return {"status": "broken"}


@app.post("/traffic/cable/{device}/{port}:repair")
def repair_cable(device: str, port: str):
    devices = get_traffic_devices()
    if device not in devices or port not in devices[device]["ports"]:
        return {"error": "Device or port not found"}
    dev = devices[device]
    port_id = dev["ports"][port]
    api_post(f"/api/v1/runtime/device/cable/{dev['id']}/{port_id}:repair", {})
    return {"status": "repaired"}
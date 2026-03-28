import time
import paramiko
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from utils import load_inventory

router = APIRouter()

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

@router.get("/factoryreset", response_class=HTMLResponse)
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

@router.post("/factoryreset", response_class=HTMLResponse)
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

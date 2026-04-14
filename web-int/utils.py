import os
import yaml
import requests
import urllib3
import paramiko
import re
import time
from fastapi.templating import Jinja2Templates

# Traffic Control initializations
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
templates = Jinja2Templates(directory="templates")

# Load Inventory
def load_inventory():
    with open("inventory.yaml", "r") as f:
        return yaml.safe_load(f)

# Common utility to get serial number via SSH
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

# Traffic Control Authentication
FABRIC_HOST = os.getenv("FABRIC_HOST")
CREDENTIAL  = os.getenv("CREDENTIAL")
API_BASE    = f"https://{FABRIC_HOST}"

_bearer_token: str | None = None
_token_expires_at: float | None = None

def get_bearer_token() -> str:
    global _bearer_token, _token_expires_at
    now = time.time()
    
    # Check if token exists and is still valid (with 5-minute buffer)
    if _bearer_token and _token_expires_at and now < (_token_expires_at - 300):
        return _bearer_token
    
    # Fetch new token
    if not FABRIC_HOST or not CREDENTIAL:
        raise RuntimeError("Missing credentials — ensure /fabric/credentials.env is present and loaded by the service.")
    
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
    response = r.json()
    _bearer_token = response["access_token"]
    _token_expires_at = now + response.get("expires_in", 36000)  # Default to 10h if not provided
    return _bearer_token

def api_get(path: str, retry: bool = True):
    global _bearer_token
    # Need to make sure we don't call this if FABRIC_HOST isn't set, it will raise exception in get_bearer_token anyway.
    r = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {get_bearer_token()}"},
        verify=False,
    )
    if r.status_code == 401 and retry:
        _bearer_token = None
        return api_get(path, retry=False)
        
    r.raise_for_status()
    return r.json()

def api_post(path: str, payload: dict, retry: bool = True):
    global _bearer_token
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
    if r.status_code == 401 and retry:
        _bearer_token = None
        return api_post(path, payload, retry=False)
        
    r.raise_for_status()
    return r.json()

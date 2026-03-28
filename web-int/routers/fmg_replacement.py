import requests
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from utils import load_inventory, get_serial

router = APIRouter()

def fmg_login(fmg_ip, fmg_user, fmg_password):
    url = f"https://{fmg_ip}/jsonrpc"
    payload = {
        "id": 1,
        "method": "exec",
        "params": [{
            "data": {"passwd": fmg_password, "user": fmg_user},
            "url": "/sys/login/user",
        }]
    }
    response = requests.post(url, json=payload, verify=False).json()
    if "session" in response:
        return response["session"]
    raise Exception(f"FMG login failed: {response}")

def fmg_logout(fmg_ip, fmg_sessionToken):
    url = f"https://{fmg_ip}/jsonrpc"
    payload = {
        "id": 1,
        "method": "exec",
        "session": fmg_sessionToken,
        "params": [{"url": "/sys/logout"}]
    }
    requests.post(url, json=payload, verify=False)

def fmg_get_sn_by_name(fmg_ip, fmg_sessionToken, fgt_name):
    url = f"https://{fmg_ip}/jsonrpc"
    payload = {
        "id": 1,
        "method": "get",
        "session": fmg_sessionToken,
        "params": [{
            "fields": ["sn"],
            "filter": [["name", "==", fgt_name]],
            "loadsub": 0,
            "url": "/dvmdb/device",
        }]
    }
    response = requests.post(url, json=payload, verify=False).json()
    data = response.get("result", [{}])[0].get("data", [])
    return data[0]["sn"] if data else ""

def fmg_delete_sn(fmg_ip, fmg_sessionToken, fmg_adom, fgt_sn):
    url = f"https://{fmg_ip}/jsonrpc"
    payload = {
        "id": 1,
        "method": "exec",
        "session": fmg_sessionToken,
        "params": [{
            "data": {"adom": [fmg_adom], "device": [fgt_sn]},
            "url": "/dvm/cmd/del/device",
        }]
    }
    requests.post(url, json=payload, verify=False)

def fmg_replace_sn(fmg_ip, fmg_sessionToken, fgt_name, fgt_sn):
    url = f"https://{fmg_ip}/jsonrpc"
    payload = {
        "id": 1,
        "method": "exec",
        "session": fmg_sessionToken,
        "params": [{
            "data": {"sn": fgt_sn},
            "url": f"/dvmdb/device/replace/sn/{fgt_name}",
        }]
    }
    response = requests.post(url, json=payload, verify=False).json()
    return response

def replace_serials_on_fmg():
    inventory = load_inventory()

    fgt_user     = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    fmg_ip       = inventory.get("fmg_ip")
    fmg_user     = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")
    fmg_adom     = inventory.get("fmg_adom")
    sites        = inventory.get("sites", {})

    results = []

    try:
        session = fmg_login(fmg_ip, fmg_user, fmg_password)
    except Exception as e:
        return [f"FMG Login Error: {str(e)}"]

    try:
        for site_name, site_data in sites.items():
            # Get real serial from the FortiGate
            real_sn = get_serial(site_data.get("ip"), fgt_user, fgt_password)

            if "Error" in real_sn or "Not Found" in real_sn:
                results.append(f"{site_name}: Failed to get serial from device")
                continue

            # Get what FMG currently has for this device
            fmg_sn = fmg_get_sn_by_name(fmg_ip, session, site_name)

            if fmg_sn == real_sn:
                results.append(f"{site_name}: Already correct ({real_sn}), skipped")
                continue

            # Delete stale SN entry if it exists as its own device on FMG
            if fmg_sn:
                fmg_delete_sn(fmg_ip, session, fmg_adom, fmg_sn)

            # Replace the SN on the named device entry
            fmg_replace_sn(fmg_ip, session, site_name, real_sn)
            results.append(f"{site_name}: replaced {fmg_sn or 'N/A'} → {real_sn}")

    except Exception as e:
        results.append(f"Error during replacement: {str(e)}")

    finally:
        fmg_logout(fmg_ip, session)

    return results

@router.post("/replace", response_class=HTMLResponse)
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

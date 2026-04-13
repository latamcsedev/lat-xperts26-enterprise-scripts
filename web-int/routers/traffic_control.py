from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from utils import load_inventory, api_get, api_post, templates

router = APIRouter()

def get_traffic_devices():
    inventory = load_inventory()
    trafficcontrol = inventory.get("trafficcontrol", {})

    resp = api_get("/api/v1/runtime/device")
    objects = resp.get("object", [])

    devices = {}

    for device_name, interfaces in trafficcontrol.items():
        row = next((o for o in objects if o.get("name") == device_name), None)
        if not row:
            continue

        ports = row.get("ports", [])

        port_map = {}

        for idx, port_id in enumerate(ports):
            port_label = f"port{idx+1}"
            if port_label in interfaces:
                port_map[port_label] = port_id

        devices[device_name] = {
            "id": row["id"],
            "ports": port_map,
            "status": {},
            "cable": {}
        }

        for port_label, pid in port_map.items():
            # TC status
            try:
                s = api_get(f"/api/v1/runtime/device/{row['id']}/port/{pid}/tc")
                obj = s.get("object", {})
                devices[device_name]["status"][port_label] = {
                    "delay": obj.get("delay", 0),
                    "loss": obj.get("loss", 0),
                    "corrupt": obj.get("corrupt", 0)
                }
            except Exception:
                devices[device_name]["status"][port_label] = {
                    "delay": 0, "loss": 0, "corrupt": 0
                }

            # Cable state
            try:
                cable_resp = api_get(f"/api/v1/runtime/device/cable/{row['id']}/{pid}")
                # API returns a string: "broken" or "repaired" (or similar)
                cable_state = cable_resp if isinstance(cable_resp, str) else cable_resp.get("object", "repaired")
                devices[device_name]["cable"][port_label] = cable_state
            except Exception:
                devices[device_name]["cable"][port_label] = "repaired"

    return devices

@router.get("/traffic", response_class=HTMLResponse)
def traffic_dashboard(request: Request):
    try:
        devices = get_traffic_devices()
        metrics = ["delay", "loss", "corrupt"]

        return templates.TemplateResponse(
            request,
            "traffic.html",
            {
                "request": request,
                "devices": devices,
                "metrics": metrics
            }
        )
    except Exception as e:
        import traceback
        return f"<html><body><h2>Traffic Dashboard Error</h2><pre>{str(e)}\n\n{traceback.format_exc()}</pre></body></html>"

@router.post("/traffic/update/{device}/{port}/{metric}")
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

@router.post("/traffic/cable/{device}/{port}:break")
def break_cable(device: str, port: str):
    devices = get_traffic_devices()
    if device not in devices or port not in devices[device]["ports"]:
        return {"error": "Device or port not found"}
    dev = devices[device]
    port_id = dev["ports"][port]
    api_post(f"/api/v1/runtime/device/cable/{dev['id']}/{port_id}:break", {})
    return {"status": "broken"}

@router.post("/traffic/cable/{device}/{port}:repair")
def repair_cable(device: str, port: str):
    devices = get_traffic_devices()
    if device not in devices or port not in devices[device]["ports"]:
        return {"error": "Device or port not found"}
    dev = devices[device]
    port_id = dev["ports"][port]
    api_post(f"/api/v1/runtime/device/cable/{dev['id']}/{port_id}:repair", {})
    return {"status": "repaired"}

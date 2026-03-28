from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from utils import load_inventory, api_get, api_post

router = APIRouter()

@router.get("/powercontrol", response_class=HTMLResponse)
def powercontrol_dashboard():
    inventory = load_inventory()
    devices = inventory.get("powercontrol", [])
    
    # Pre-fetch the device objects to resolve name -> id
    try:
        dev_resp = api_get("/api/v1/runtime/device")
        all_objects = dev_resp.get("object", [])
    except Exception:
        all_objects = []

    rows = ""
    for dev in devices:
        # Find device ID for this dev name
        row = next((o for o in all_objects if o.get("name") == dev), None)
        button = ""
        status = "unknown"
        dev_id = None
        
        if not row:
            status = "not found in fabric backend"
        else:
            dev_id = row["id"]
            try:
                resp = api_get(f"/api/v1/runtime/vm/{dev_id}/status")
                status = resp.get("object", {}).get("status", "unknown")
            except Exception as e:
                status = f"error: {str(e)[:30]}"
                
            if status.lower() == "running":
                button = f'''
                <form action="/powercontrol/{dev_id}/power-off?name={dev}" method="post" style="display:inline;">
                    <button class="btn danger" type="submit">Power Off</button>
                </form>
                '''
            elif status.lower() != "error":
                button = f'''
                <form action="/powercontrol/{dev_id}/power-on?name={dev}" method="post" style="display:inline;">
                    <button class="btn primary" type="submit">Power On</button>
                </form>
                '''
                
        rows += f'''
        <tr>
            <td style="padding:10px; border-bottom:1px solid #ddd;">{dev}</td>
            <td style="padding:10px; border-bottom:1px solid #ddd;"><b>{status}</b></td>
            <td style="padding:10px; border-bottom:1px solid #ddd;">{button}</td>
        </tr>
        '''
        
    return f"""
    <html>
    <head>
        <title>Power Control Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 40px; }}
            .btn {{ padding: 8px 12px; border-radius: 4px; border: none; cursor: pointer; color: white; font-size: 14px; font-weight: 500; }}
            .primary {{ background: #1677ff; }}
            .danger {{ background: #dc3545; }}
            .btn:hover {{ opacity: 0.9; }}
            table {{ border-collapse: collapse; min-width: 600px; background: white; margin-top: 20px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); border-radius: 8px; overflow: hidden; }}
            th {{ background: #f8f9fa; text-align: left; padding: 15px; border-bottom: 2px solid #dee2e6; color: #444; }}
            td {{ padding: 15px; border-bottom: 1px solid #eee; }}
            a {{ color: #1677ff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h2>Power Control Dashboard</h2>
        <table>
            <tr><th>Device</th><th>Status</th><th>Action</th></tr>
            {rows}
        </table>
        <div style="margin-bottom: 20px;">
            <a href="/" style="
                text-decoration: none;
                background: #007bff;
                color: white;
                padding: 8px 14px;
                border-radius: 6px;
                font-weight: 500;
            ">
              ← Back to Home
            </a>
        </div>
    </body>
    </html>
    """

@router.post("/powercontrol/{device}/{action}", response_class=HTMLResponse)
def powercontrol_action(device: str, action: str, request: Request, name: str = None):
    try:
        if action not in ["power-on", "power-off"]:
            return "Invalid action"
            
        payload = None
        if action == "power-on":
            payload = {
                "configuration": True,
                "license": True,
                "post_boot": True,
                "timeout": 0
            }
            
        try:
            api_post(f"/api/v1/runtime/vm/{device}:{action}", payload)
            message = f"Successfully sent {action} to {device} ({name})."
        except Exception as e:
            message = f"Error sending {action}: {str(e)}"
            
        return f"""
        <html>
        <head>
            <meta http-equiv="refresh" content="3;url=/powercontrol" />
            <title>Action Triggered</title>
            <style>
                body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 40px; text-align: center; }}
                .message-box {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); display: inline-block; }}
                a {{ color: #1677ff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="message-box">
                <h2>{message}</h2>
                <p>Redirecting back to dashboard in 3 seconds...</p>
                <br>
                <a href="/powercontrol">Click here if not redirected automatically</a>
            </div>
        </body>
        </html>
        """
    except Exception as e:
        import traceback
        return f"<html><body><h2>Action Error</h2><pre>{str(e)}\n\n{traceback.format_exc()}</pre></body></html>"

import asyncio
import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils import api_get, api_post

router = APIRouter()

# Typed by the operator before A3_ToolHost is reinstalled.
DEVICE_NAME = "A3_ToolHost"
CONFIRM_PHRASE = DEVICE_NAME

# Same body as Reset SD-WAN and Lab Validation. POST install already
# uninstalls the current instance, so this action does not DELETE first.
# A3_ToolHost is the VM this portal runs on; a delete-then-wait would stop
# the process before the install was requested.
REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
}


class UpdatePortalRequest(BaseModel):
    confirmation: str


def get_runtime_device_map():
    try:
        response = api_get("/api/v1/runtime/device")
        objects = response.get("object", [])
        return {obj.get("name"): obj.get("id") for obj in objects if obj.get("name") and obj.get("id")}
    except Exception:
        return {}


async def reinstall_toolhost():
    name = DEVICE_NAME
    yield f"Update Automation Portal: reinstalling {name}\n"
    await asyncio.sleep(0.2)

    device_map = get_runtime_device_map()
    if not device_map:
        yield "Could not read runtime devices from Fabric Studio — nothing was reinstalled.\n"
        return

    device_id = device_map.get(name)
    if not device_id:
        yield f"{name} not found in Fabric Studio runtime devices, skipping\n"
        return

    try:
        api_post(f"/api/v1/runtime/device/{device_id}", REINSTALL_PAYLOAD)
        yield f"{name} ({device_id}) reinstall started\n"
        yield (
            "The tool host is reinstalling. This portal will drop offline "
            "if it runs on that VM, until the VM finishes booting.\n"
        )
    except Exception as exc:
        yield f"{name} reinstall failed: {exc}\n"


@router.get("/updateportal", response_class=HTMLResponse)
def update_portal_page():
    name = html.escape(DEVICE_NAME)

    return f"""
    <html>
    <head>
        <title>Update Automation Portal</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f4f6f9;
                padding: 40px;
            }}

            h1 {{
                margin-bottom: 30px;
            }}

            .box {{
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
                max-width: 900px;
            }}

            .box h2 {{
                margin-top: 0;
                font-size: 18px;
                color: #dc3545;
            }}

            .btn {{
                display: inline-block;
                padding: 12px 18px;
                border-radius: 8px;
                border: none;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
            }}

            .danger {{
                background: #dc3545;
                color: white;
            }}

            .btn:disabled {{
                background: #c9ced6;
                cursor: not-allowed;
            }}

            .btn:hover:enabled {{
                opacity: 0.9;
            }}

            #confirm {{
                padding: 8px;
                font-size: 14px;
                border: 1px solid #c9ced6;
                border-radius: 6px;
            }}

            #responseArea {{
                margin-top: 20px;
                font-family: Menlo, Consolas, monospace;
                font-size: 13px;
                white-space: pre-wrap;
                color: #222;
            }}
        </style>
        <script>
            const PHRASE = "{CONFIRM_PHRASE}";

            function checkPhrase() {{
                const typed = document.getElementById("confirm").value.trim();
                document.getElementById("go").disabled = typed !== PHRASE;
            }}

            async function updatePortal() {{
                const typed = document.getElementById("confirm").value.trim();
                const outputDiv = document.getElementById("responseArea");
                outputDiv.textContent = "";

                const response = await fetch('/updateportal_submit', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ confirmation: typed }})
                }});

                if (!response.ok) {{
                    outputDiv.textContent = await response.text();
                    return;
                }}

                const reader = response.body.getReader();
                const decoder = new TextDecoder();

                while (true) {{
                    const {{ value: chunkValue, done }} = await reader.read();
                    if (done) break;
                    const chunk = decoder.decode(chunkValue, {{ stream: true }});
                    const line = document.createElement('div');
                    line.textContent = chunk;
                    outputDiv.appendChild(line);
                }}
            }}
        </script>
    </head>
    <body>

    <button onclick="window.history.back()">Back</button> <h1>Automation Portal 1.06</h1>

    <div class="box">
        <h2>Update Automation Portal</h2>

        This reinstalls <b>{name}</b> through Fabric Studio. The runtime device is
        removed and installed again with its license, configuration, and post-boot
        run, then powered on.
        <br><br>
        A portal running on that VM goes offline until the VM finishes booting.
        <br><br>
        Type <b>{name}</b> below to confirm:
        <br><br>
        <input type="text" id="confirm" size="45" autocomplete="off" oninput="checkPhrase()">
        <br><br>
        <button class="btn danger" id="go" onclick="updatePortal(); return false;" type="button" disabled>
            Update Automation Portal
        </button>

        <div id="responseArea"></div>
    </div>

    </body>
    </html>
    """


@router.post("/updateportal_submit")
def update_portal_submit(req: UpdatePortalRequest):
    typed = (req.confirmation or "").strip()
    if typed != CONFIRM_PHRASE:
        return HTMLResponse(
            "Confirmation failed — nothing was reinstalled. "
            f"Type {CONFIRM_PHRASE} exactly to proceed.",
            status_code=400,
        )
    return StreamingResponse(reinstall_toolhost(), media_type="text/event-stream")

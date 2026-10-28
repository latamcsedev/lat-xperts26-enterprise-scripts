import asyncio
import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils import api_delete, api_get, api_post, load_inventory

router = APIRouter()

# Typed by the operator before anything is reinstalled.
CONFIRM_PHRASE = "superkalifragilistikexpialigetisch"

# VMs reinstalled by Reset SD-WAN, in reinstall order. inventory.yaml may
# override this with a `resetsdwan:` list so another lab resets its own set.
DEFAULT_RESET_VMS = [
    "A1_FortiManager",
    "fg1-H1",
    "fg1-H2",
    "fg1-site1",
    "fg1-site2",
]

REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
}


class ResetSdwanRequest(BaseModel):
    confirmation: str


def reset_targets():
    try:
        targets = load_inventory().get("resetsdwan") or []
    except Exception:
        targets = []
    return [str(t) for t in targets] or DEFAULT_RESET_VMS


def get_runtime_device_map():
    try:
        response = api_get("/api/v1/runtime/device")
        objects = response.get("object", [])
        return {obj.get("name"): obj.get("id") for obj in objects if obj.get("name") and obj.get("id")}
    except Exception:
        return {}


async def reset_sdwan_devices():
    targets = reset_targets()

    yield "Reset SD-WAN: reinstalling " + ", ".join(targets) + "\n"
    await asyncio.sleep(0.5)

    device_map = get_runtime_device_map()
    if not device_map:
        yield "Could not read runtime devices from Fabric Studio — nothing was reinstalled.\n"
        return

    for name in targets:
        device_id = device_map.get(name)
        yield f"--- {name} ---\n"
        await asyncio.sleep(0.2)

        if not device_id:
            yield f"{name} not found in Fabric Studio runtime devices, skipping\n"
            await asyncio.sleep(0.2)
            continue

        try:
            yield f"{name} ({device_id}) removing runtime device\n"
            api_delete(f"/api/v1/runtime/device/{device_id}")
            await asyncio.sleep(30)  # asyncio.sleep, not time.sleep, to keep the stream alive
            api_post(f"/api/v1/runtime/device/{device_id}", REINSTALL_PAYLOAD)
            yield f"{name} reinstall started\n"
        except Exception as exc:
            yield f"{name} reinstall failed: {exc}\n"

        await asyncio.sleep(0.5)

    yield "Reset SD-WAN finished — devices are booting. Run Lab Validation once they are up.\n"


@router.get("/resetsdwan", response_class=HTMLResponse)
def resetsdwan_page():
    target_list = "".join(f"<li>{html.escape(name)}</li>" for name in reset_targets())

    return f"""
    <html>
    <head>
        <title>Reset SD-WAN</title>
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

            async function resetSdwan() {{
                const typed = document.getElementById("confirm").value.trim();
                const outputDiv = document.getElementById("responseArea");
                outputDiv.textContent = "";

                const response = await fetch('/resetsdwan_submit', {{
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
        <h2>Reset SD-WAN</h2>

        This reinstalls the following VMs through Fabric Studio. Each runtime device is removed
        and reinstalled with its license, configuration and post-boot run:
        <ul>
            {target_list}
        </ul>

        This wipes the SD-WAN lab. Type <b>{CONFIRM_PHRASE}</b> below to confirm:
        <br><br>
        <input type="text" id="confirm" size="45" autocomplete="off" oninput="checkPhrase()">
        <br><br>
        <button class="btn danger" id="go" onclick="resetSdwan(); return false;" type="button" disabled>
            Reset SD-WAN
        </button>

        <div id="responseArea"></div>
    </div>

    </body>
    </html>
    """


@router.post("/resetsdwan_submit")
def resetsdwan_submit(req: ResetSdwanRequest):
    typed = (req.confirmation or "").strip()
    if typed != CONFIRM_PHRASE:
        return HTMLResponse(
            "Confirmation failed — nothing was reinstalled. "
            f"Type {CONFIRM_PHRASE} exactly to proceed.",
            status_code=400,
        )
    return StreamingResponse(reset_sdwan_devices(), media_type="text/event-stream")

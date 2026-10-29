import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter()

SCRIPT = Path(__file__).resolve().parent.parent / "fgt_ext_default_routes.py"


def run_public_ip_fix():
    """Run fgt_ext_default_routes.py and stream its output."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            yield line
    finally:
        code = process.wait()
    if code != 0:
        yield f"\nPublic IP Fix failed (exit {code})\n"
    else:
        yield "\nPublic IP Fix finished\n"


@router.get("/publicipfix", response_class=HTMLResponse)
def public_ip_fix_page():
    return """
    <html>
    <head>
        <title>Public IP Fix</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f4f6f9;
                padding: 40px;
            }

            h1 {
                margin-bottom: 30px;
            }

            .box {
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
                max-width: 900px;
            }

            .box h2 {
                margin-top: 0;
                font-size: 18px;
                color: #444;
            }

            .btn {
                display: inline-block;
                padding: 12px 18px;
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

            .btn:disabled {
                background: #c9ced6;
                cursor: not-allowed;
            }

            .btn:hover:enabled {
                opacity: 0.9;
            }

            #responseArea {
                margin-top: 20px;
                font-family: Menlo, Consolas, monospace;
                font-size: 13px;
                white-space: pre-wrap;
                color: #222;
            }
        </style>
        <script>
            async function runFix() {
                const button = document.getElementById("go");
                const outputDiv = document.getElementById("responseArea");
                button.disabled = true;
                outputDiv.textContent = "";

                const response = await fetch("/publicipfix_submit", { method: "POST" });
                const reader = response.body.getReader();
                const decoder = new TextDecoder();

                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    outputDiv.textContent += decoder.decode(value, { stream: true });
                }
                button.disabled = false;
            }
        </script>
    </head>
    <body>

    <button onclick="window.history.back()">Back</button> <h1>Automation Portal 1.06</h1>

    <div class="box">
        <h2>Public IP Fix</h2>

        <p>
            GCP does not give FGT_EXT port1–port5 a default gateway. This runs
            <code>fgt_ext_default_routes.py</code> against the external FortiGate
            and installs a default route on each of those ports, with the same
            distance and priority as port10, so they ECMP.
        </p>

        <button class="btn primary" id="go" onclick="runFix()" type="button">
            Run Public IP Fix
        </button>

        <div id="responseArea"></div>
    </div>

    </body>
    </html>
    """


@router.post("/publicipfix_submit")
def public_ip_fix_submit():
    return StreamingResponse(run_public_ip_fix(), media_type="text/plain")

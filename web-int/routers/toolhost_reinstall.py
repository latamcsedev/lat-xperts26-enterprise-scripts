import asyncio
import paramiko
import re
from urllib.parse import unquote
from paramiko_expect import SSHClientInteraction
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
from fastapi import Request, FastAPI

from utils import load_inventory,get_serial

from pydantic import BaseModel

class ReinstallRequest(BaseModel):
    fs_instance: str
    password: str

router = APIRouter()

async def toolhost_reinstall_all(fs_instance_list,fs_pass):
    prompt = ".*[ ~]#.*"

    fs_instances = fs_instance_list.splitlines()
    for fs_instance in fs_instances:
        fs_instance = fs_instance.strip()
        yield f"Processing {fs_instance}\n"
        await asyncio.sleep(0.5)

        try:
            await asyncio.sleep(0.5)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=fs_instance, username="admin", password=fs_pass, timeout=10)
            await asyncio.sleep(2)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)
            interact.send("runtime device install tool-h")
            interact.expect(prompt)
            yield f"Finished {fs_instance}\n"
            await asyncio.sleep(0.5)
        except Exception as e:
            yield f"{e}"
            await asyncio.sleep(0.5)
            yield f"Failed to process {fs_instance}\n"
            await asyncio.sleep(0.5)


@router.get("/toolhost_reinstall", response_class=HTMLResponse)
def toolhost_reinstall():
       
    return """
    <html>
    <head>
        <title>A3_Toolhost reinstall</title>
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
    <script>
    async function reinstall() {
        const textArea = document.getElementById("fs_list").value;
        const password = document.getElementById("pwd").value;

        const response = await fetch('/toolhost_reinstall_submit', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ 
                fs_instance: textArea,
                password: password
            })
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const outputDiv = document.getElementById('responseArea');

        outputDiv.innerHTML = "";

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value, { stream: true });

            const line = document.createElement('div');
            line.textContent = chunk;
            outputDiv.appendChild(line);
        }
    }
    </script>
    <body>
        <h2>A3_Toolhost reinstall</h2>
        FabricStudio Password:<br>
        <input type="password" id="pwd"><br>
        FabricStudio URL list:<br>
        <textarea class="input" rows="30" cols="80" id="fs_list"></textarea>
        <br>
        <button class="btn danger" onclick="reinstall(); return false;" type="button"> Reinstall All </button>
        <div id="responseArea">
        </div>
    </body>
    </html>
    """

@router.get("/toolhost_reinstall_submit/{fs_instance}")
def toolhost_reinstall_submit(fs_instance: str):
    return StreamingResponse(toolhost_reinstall_all(fs_instance), media_type="text/event-stream")

@router.post("/toolhost_reinstall_submit")
def toolhost_reinstall_submit(req: ReinstallRequest):
    return StreamingResponse(
        toolhost_reinstall_all(req.fs_instance, req.password),
        media_type="text/event-stream"
    )
import asyncio
import paramiko
import re
from paramiko_expect import SSHClientInteraction
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse

from utils import load_inventory,get_serial

router = APIRouter()

async def prepare_fos8_devices():
    prompt = ".* #.*"
    inventory = load_inventory()
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites_v8", {})
    fmg_ip       = "10.254.1.28"
    fmg_user     = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")

    yield f"Starting to execute actions on FOS 8.0 devices, please do not refresh or close this page\n"
    await asyncio.sleep(0.5)

    for site_name, site_data in sites.items():
        
        ip = site_data.get("ip")
        real_sn = "Not Found"
        
        # Device Factory Reset
        try:
            yield f"Starting {site_name}\n"
            await asyncio.sleep(0.5)
            
            try:
                yield f"{site_name} Trying to login\n"
                await asyncio.sleep(0.5)
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password=fgt_password, timeout=10)
                await asyncio.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect(prompt)
            except Exception as e:
                yield f"{e}"
                await asyncio.sleep(0.5)
                yield f"{site_name} Trying to reset password\n"
                await asyncio.sleep(0.5)
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password="", timeout=10)
                await asyncio.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect('.*Password.*')
                interact.send(fgt_password)
                interact.expect('.*Password.*')
                interact.send(fgt_password)
                interact.expect(prompt)
                ssh.close()
                # Reconnect with new password
                yield f"{site_name} Trying to login again\n"
                await asyncio.sleep(0.5)
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password=fgt_password, timeout=10)
                await asyncio.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect(prompt)
            
            yield f"{site_name} Finding the serial number\n"
            await asyncio.sleep(0.5)
            interact.send("config system console")
            interact.expect(prompt)
            interact.send("set output standard")
            interact.expect(prompt)
            interact.send("end")
            interact.expect(prompt)
            interact.send("get system status")
            interact.expect(prompt)
            
            log = interact.current_output
            match = re.search(r"Serial-Number:\s+(.*)", log)
            real_sn = match.group(1).strip() if match else "Not Found"
            
            yield f"{site_name} Starting factory reset\n"
            await asyncio.sleep(0.5)
            # Send reset command
            interact.send("execute factoryreset2 keepvmlicense")
            interact.expect('.*y/n.*')
            interact.send("y")
            ssh.close()

            yield f"Factory Reset executed on {site_name} {real_sn}\n"

        except Exception as e:
            yield f"Failed to reset {site_name}"
            await asyncio.sleep(0.5)
            yield f"{e}"
            await asyncio.sleep(0.5)
        
        # Serial Number update on FMG
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=fmg_ip, username=fmg_user, password=fmg_password, timeout=10)
            await asyncio.sleep(2)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)

            try:
                if "Error" in real_sn or "Not Found" in real_sn:
                    yield f"{site_name}: Failed to get serial from device\n"
                    await asyncio.sleep(0.5)

                # Get what FMG currently has for this device
                interact.send(f"diag dvm device list {real_sn}\n")
                interact.expect(prompt)
                if "Hub80" in interact.current_output_clean or "Branch80" in interact.current_output_clean:
                    yield f"{site_name}: sn {real_sn} already correct on FMG, skipped\n"
                    await asyncio.sleep(0.5)
                else:
                    interact.send(f"diag dvm device delete root {real_sn}")
                    interact.expect(prompt)
                    interact.send(f"execute device replace sn {site_name} {real_sn}")
                    interact.expect(prompt)
                    yield f"{site_name}: replaced → {real_sn}"
                    await asyncio.sleep(0.5)

            except Exception as e:
                yield f"Error during replacement: {str(e)}\n"
                await asyncio.sleep(0.5)

            finally:
                ssh.close()

        except Exception as e:
            yield f"Failed to update sn on FMG for {site_name}\n"
            await asyncio.sleep(0.5)
    
    # Waiting up to 300 seconds and configure FMG
    yield f"Waiting for the devices to boot\n"
    await asyncio.sleep(0.5)
    results = {}
    for site_name, site_data in sites.items():
        results[site_name] = False
    
    for timer_count in range (0,20):
        finished = True
        for site_name, site_data in sites.items():
            if not results[site_name]: finished = False
        if finished: break

        await asyncio.sleep(15)
        for site_name, site_data in sites.items():
            ip = site_data.get("ip")
            if results[site_name] == False:
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(hostname=ip, username=fgt_user, password="", timeout=10)
                    await asyncio.sleep(2)
                    interact = SSHClientInteraction(ssh, timeout=10, display=True)
                    interact.expect('.*Password.*')
                    interact.send(fgt_password)
                    interact.expect('.*Password.*')
                    interact.send(fgt_password)
                    interact.expect(prompt)
                    ssh.close()
                    # Reconnect with new password
                    yield f"{site_name} password reset\n"
                    await asyncio.sleep(0.5)
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(hostname=ip, username=fgt_user, password=fgt_password, timeout=10)
                    await asyncio.sleep(2)
                    interact = SSHClientInteraction(ssh, timeout=10, display=True)
                    interact.expect(prompt)

                    # Send FMG config
                    interact.send("config system central-management")
                    interact.expect(prompt)
                    interact.send("set type fortimanager")
                    interact.expect(prompt)
                    interact.send("set fmg 10.254.1.28")
                    interact.expect(prompt)
                    interact.send("end")
                    interact.expect('.*y/n.*')
                    interact.send("y")
                    interact.expect('.*y/n.*')
                    interact.send("y")
                    interact.expect(prompt)
                    
                    
                    ssh.close()
                    results[site_name] = True
                    yield f"{site_name} FMG configured"
                    await asyncio.sleep(0.5)

                except Exception as e:
                    yield f"{site_name} not ready yet (retry: {timer_count + 1})"
                    await asyncio.sleep(0.5)
                    yield f"{e}"
                    await asyncio.sleep(0.5)
        
        
    yield f"Script finished, you can close this page now\n"
    await asyncio.sleep(0.5)


@router.get("/fos80labtools", response_class=HTMLResponse)
def fos80labtools_home():
    return """
    <html>
    <head>
        <title>Automation Portal</title>
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

    <button onclick="window.history.back()">Back</button> <h1>Automation Portal 1.04</h1>

    <div class="grid">

        <!-- Prepare FOSv8 for FMG -->
        <div class="section">
            <h2>Prepare FOSv8 for FMG</h2>
            <form action="/fos80labtools_prepare" method="get">
                <button class="btn danger" type="submit">
                    Prepare FOSv8 for FMG
                </button>
            </form>
        </div>

    </div>

    </body>
    </html>
    """





@router.get("/fos80labtools_prepare", response_class=HTMLResponse)
def fos80labtools_prepare():
    return """
    <html>
    <head>
        <title>Automation Portal</title>
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
        <script>
            async function prepare() {
                const response = await fetch('/fos80labtools_prepare_submit');
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                const outputDiv = document.getElementById('responseArea');

                while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                const chunk = decoder.decode(value, { stream: true });
                let br = document.createElement('br');
                outputDiv.appendChild(br);
                outputDiv.innerHTML += chunk;
                }
            }
        </script>
    </head>
    <body>

    <button onclick="window.history.back()">Back</button> <h1>Automation Portal 1.04</h1>

    <div class="grid">

        <!-- Prepare FOSv8 for FMG -->
        <div class="section">
            <h2>Prepare FOSv8 for FMG</h2>
            
            This will factory reset fgt1-v8 (Hub80) and fgt2-v8 (Branch80), click Proceed to confirm:<br>
            <br>
            <form action="/fos80labtools_prepare" method="get">
                <button onclick="prepare(); return false;" type="button"> Proceed </button>
            </form>

            <div id="responseArea">
            </div>
        </div>

    </div>

    </body>
    </html>
    """


@router.get("/fos80labtools_prepare_submit")
def fos80labtools_prepare_submit():
    return StreamingResponse(prepare_fos8_devices(), media_type="text/event-stream")
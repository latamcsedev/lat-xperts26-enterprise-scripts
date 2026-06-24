import asyncio
import paramiko
import re
import os
import requests
import socket
import time

from paramiko_expect import SSHClientInteraction
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse

from utils import load_inventory,get_serial

router = APIRouter()

def device_online(host_ip,username,password,site_name):
    prompt = ".*[ ~]#.*"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
        time.sleep(2)
        interact = SSHClientInteraction(ssh, timeout=10, display=True)
        interact.expect(prompt)
        print(f"Device {site_name} online")
        return True
    except socket.timeout:
        # Device offline
        print(f"Device {site_name} offline")
        return False
    except paramiko.ssh_exception.AuthenticationException:
        print("Authentication failed, retrying")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host_ip, username=username, password="", timeout=10)
            time.sleep(2)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(".*Password.*")
            return True
        except:
            print(f"Login error on {host_ip}")
            return False
    except:
        print(f"Unknown error on {host_ip}")
        return False
    
def disable_offline_mode(host_ip,username,password):
    prompt = ".*[ ~]#.*"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
        time.sleep(2)
        interact = SSHClientInteraction(ssh, timeout=10, display=True)
        interact.expect(prompt)
        interact.send("config system admin setting")
        interact.expect(prompt)
        interact.send("set offline_mode disable")
        interact.expect(prompt)
        interact.send("end")
        interact.expect(prompt)
        return True
    except:
        print(f"Unknown error on {host_ip}")
        return False

def set_fmg(host_ip,username,password):
    prompt = ".*[ ~]#.*"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
        time.sleep(2)
        interact = SSHClientInteraction(ssh, timeout=10, display=True)
        interact.expect(prompt)
        interact.send("config system central-management")
        interact.expect(prompt)
        interact.send("set type fortimanager")
        interact.expect(prompt)
        interact.send("set fmg 10.254.1.2")
        interact.expect(prompt)
        interact.send("end")
        interact.expect(".*y/n.*")
        interact.send("y")
        interact.expect(".*y/n.*")
        interact.send("y")
        interact.expect(prompt)
        time.sleep(2)
        interact.send("show sys central-management")
        interact.expect(prompt)
        output = interact.current_output_clean
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"FMG registration result: {host_ip}\n")
            f.write(f"{output}\n")
        if not "serial-number" in output:
            #failed to register on FMG, trying again
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"failed to register on FMG, trying again: {host_ip}\n")
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
                time.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect(prompt)
                interact.send("config system central-management")
                interact.expect(prompt)
                interact.send("set type fortimanager")
                interact.expect(prompt)
                interact.send("set fmg 10.254.1.2")
                interact.expect(prompt)
                interact.send("end")
                interact.expect(".*y/n.*")
                interact.send("y")
                interact.expect(".*y/n.*")
                interact.send("y")
                interact.expect(prompt)
                time.sleep(2)
                interact.send("show sys central-management")
                interact.expect(prompt)
                output = interact.current_output_clean
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(f"FMG registration result: {host_ip}\n")
                    f.write(f"{output}\n")
                if not "serial-number" in output:
                    return False
                return True

            except:
                return False
        return True
    except:
        print(f"Unknown error on {host_ip}")
        #failed to register on FMG, trying again
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"failed to register on FMG, trying again: {host_ip}\n")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
            time.sleep(2)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)
            interact.send("config system central-management")
            interact.expect(prompt)
            interact.send("set type fortimanager")
            interact.expect(prompt)
            interact.send("set fmg 10.254.1.2")
            interact.expect(prompt)
            interact.send("end")
            interact.expect(".*y/n.*")
            interact.send("y")
            interact.expect(".*y/n.*")
            interact.send("y")
            interact.expect(prompt)
            time.sleep(2)
            interact.send("show sys central-management")
            interact.expect(prompt)
            output = interact.current_output_clean
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"FMG registration result: {host_ip}\n")
                f.write(f"{output}\n")
            if not "serial-number" in output:
                return False
            return True
        except:
            return False

def replace_sn_fmg():
    prompt = ".*[ ~]#.*"
    inventory = load_inventory()
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})
    fmg_ip       = inventory.get("fmg_ip")
    fmg_user     = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")
    with open ("/root/tshoot.log", "w+") as f:
        f.write(f"Starting\n")
    for site_name, site_data in sites.items():
        
        ip = site_data.get("ip")
        real_sn = "Not Found"
        
        # Device Factory Reset
        try:
            time.sleep(0.5)
            
            try:
                time.sleep(0.5)
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password=fgt_password, timeout=10)
                time.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect(prompt)
            except Exception as e:
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(f"Authentication failed on: {site_name}\n")
                    f.write(str(e))
                    f.write(f"\n")
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password="", timeout=10)
                time.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect('.*Password.*')
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(f"{interact.current_output_clean}\n")
                interact.send(fgt_password)
                interact.expect('.*Password.*')
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(f"{interact.current_output_clean}\n")
                interact.send(fgt_password)
                interact.expect(prompt)
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(f"{interact.current_output_clean}\n")
                ssh.close()
                # Reconnect with new password
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=ip, username=fgt_user, password=fgt_password, timeout=10)
                time.sleep(2)
                interact = SSHClientInteraction(ssh, timeout=10, display=True)
                interact.expect(prompt)
            
            interact.send("get system status | grep Serial")
            interact.expect(prompt)
            
            log = interact.current_output
            match = re.search(r"Serial-Number:\s+(.*)", log)
            real_sn = match.group(1).strip() if match else "Not Found"
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"{real_sn}\n")
            # Purge static route
            interact.send("config router static")
            interact.expect(prompt)
            interact.send("purge")
            interact.expect('.*y/n.*')
            interact.send("y")
            interact.expect(prompt)
            interact.send("end")
            interact.expect(prompt)
            # Send reset command
            interact.send("execute factoryreset2 keepvmlicense")
            interact.expect('.*y/n.*')
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"{interact.current_output_clean}\n")
            interact.send("y")
            ssh.close()

        except Exception as e:
            print(f"{e}")
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"Authentication failed on: {site_name}\n")
                f.write(str(e))
                f.write(f"\n")
            return False
        
        # Serial Number update on FMG
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=fmg_ip, username=fmg_user, password=fmg_password, timeout=10)
            time.sleep(2)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)

            try:
                if "Error" in real_sn or "Not Found" in real_sn:
                    print(f"{site_name}: Failed to get serial from device")
                # Get what FMG currently has for this device
                interact.send(f"diag dvm device list {real_sn}\n")
                interact.expect(prompt)
                if "fg1" in interact.current_output_clean or "fg2" in interact.current_output_clean:
                    print(f"{site_name}: sn {real_sn} already correct on FMG, skipped")
                else:
                    interact.send(f"diag dvm device delete root {real_sn}")
                    interact.expect(prompt)
                    interact.send(f"execute device replace sn {site_name} {real_sn}")
                    interact.expect(prompt)

            except Exception as e:
                print(f"Error during replacement: {str(e)}\n")
                with open ("/root/tshoot.log", "a+") as f:
                    f.write(str(e))
                    f.write(f"\n")
                return False

            finally:
                ssh.close()

        except Exception as e:
            print(f"Error during replacement: {str(e)}\n")
            with open ("/root/tshoot.log", "a+") as f:
                f.write(str(e))
                f.write(f"\n")
            return False
        
    return True

def restore_fmg_backup(fmg_ip, fmg_user, fmg_password, backup_name):
    prompt = ".*[ ~]#.*"
    print(f"Starting to restore FMG\n")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=fmg_ip, username=fmg_user, password=fmg_password, timeout=10)
        time.sleep(2)
        interact = SSHClientInteraction(ssh, timeout=10, display=True)
        interact.expect(prompt)
        # TBD - FTP server not working
        interact.send(f"execute restore all-settings sftp 10.254.1.16 /srv/ftp/{backup_name} root {fmg_password} fortinet")
        interact.expect('.*y/n.*')
        interact.send("y")
        interact.expect('.*estarting')
        # wait some time for FMG to start rebooting
        time.sleep(2)
    except:
        print(f"Failed to restore FMG config\n")

    print(f"Configuration restored\n")

def fgt_online(host_ip,username,password):
    prompt = ".*[ ~]#.*"
    try:
        time.sleep(0.5)
        
        try:
            time.sleep(0.5)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
            time.sleep(1)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)
        except Exception as e:
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"Authentication failed on: {host_ip}\n")
                f.write(str(e))
                f.write(f"\n")
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host_ip, username=username, password="", timeout=10)
            time.sleep(1)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect('.*Password.*')
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"{interact.current_output_clean}\n")
            interact.send(password)
            interact.expect('.*Password.*')
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"{interact.current_output_clean}\n")
            interact.send(password)
            interact.expect(prompt)
            with open ("/root/tshoot.log", "a+") as f:
                f.write(f"{interact.current_output_clean}\n")
            ssh.close()
            # Reconnect with new password
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
            time.sleep(1)
            interact = SSHClientInteraction(ssh, timeout=10, display=True)
            interact.expect(prompt)
            
        interact.send("get system status | grep Serial")
        interact.expect(prompt)
        return True
        
    except:
        return False

def fmg_create_api_user(fmg_ip, fmg_user, fmg_password):
    prompt = ".*[ ~]#.*"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=fmg_ip, username=fmg_user, password=fmg_password, timeout=10)
        time.sleep(2)
        interact = SSHClientInteraction(ssh, timeout=10, display=True)
        interact.expect(prompt)
        interact.send("config system admin user")
        interact.expect(prompt)
        interact.send("edit admin-api")
        interact.expect(prompt)
        interact.send("set rpc-permit read-write")
        interact.expect(prompt)
        interact.send("set user_type api")
        interact.expect(prompt)
        interact.send("set password rRUp8!ndXerj5V")
        interact.expect(prompt)
        interact.send("set profileid Super_User")
        interact.expect(prompt)
        interact.send("end")
        interact.expect(prompt)
        ssh.close()
    except:
        print(f"Failed to create API user\n")
    print(f"API user created\n")

def fmg_install_policy(fmg_ip):
    url = f"https://{fmg_ip}/jsonrpc"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer rRUp8!ndXerj5V"}
    payload = {
        "id": 1,
        "method": "exec",
        "params": [
            {
            "url": "/securityconsole/install/package",
            "data": {
                "adom": "root",
                "pkg": "Policy-Package_Hubs",
                "flags": [
                "none"
                ],
                "scope": [
                {
                    "name": "HUBs"
                }
                ]
            }
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload, verify=False)
    with open ("/root/tshoot.log", "a+") as f:
        f.write(f"{response.content}\n")

    payload = {
        "id": 1,
        "method": "exec",
        "params": [
            {
            "url": "/securityconsole/install/package",
            "data": {
                "adom": "root",
                "pkg": "Policy-Package_Spokes",
                "flags": [
                "none"
                ],
                "scope": [
                {
                    "name": "SPKs"
                }
                ]
            }
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload, verify=False)
    with open ("/root/tshoot.log", "a+") as f:
        f.write(f"{response.content}\n")

async def prepare_sdwan_devices():
    inventory = load_inventory()
    fmg_ip       = inventory.get("fmg_ip")
    fmg_user     = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})

    yield f"Preparing SD-WAN environment, please do not refresh or close this page\n"
    await asyncio.sleep(0.5)

    try:
        yield f"Starting backup download\n"
        await asyncio.sleep(0.5)
        folder_path = '/srv/ftp/'
        file_path = os.path.join(folder_path, 'fmg_sdwan.dat')
        yield f"{file_path}\n"
        await asyncio.sleep(0.5)
        
        if not os.path.isfile(file_path):
            url = 'https://xperts26.s3.sa-east-1.amazonaws.com/2026.04.27_FMG_v7.6.6_pwd_fortinet_sdwan_final.dat'
            folder_path = '/srv/ftp/'
            file_path = os.path.join(folder_path, 'fmg_sdwan.dat')
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
            yield f"Download Completed\n"
            await asyncio.sleep(0.5)
        else:
            yield f"File already exists, skipping download\n"
            await asyncio.sleep(0.5)
    except Exception as e:
        yield f"Failed to download FMG backup\n"
        await asyncio.sleep(0.5)
        return


    try:
        yield f"Starting to restore FMG configuration\n"
        await asyncio.sleep(0.5)
        
        restore_fmg_backup(fmg_ip, fmg_user, fmg_password, "fmg_sdwan.dat")
        
        yield f"Configuration restored, restarting\n"
        await asyncio.sleep(10)
        #wait until fmg is back online for 5 minutes
        for retry in range(0,30):
            yield f"FMG not online yet (retry: {retry + 1})\n"
            await asyncio.sleep(10)
            if (device_online(fmg_ip, fmg_user, fmg_password, "A1_FortiManager")):
                break
        
        #check if FMG is back online
        if not device_online(fmg_ip, fmg_user, fmg_password, "A1_FortiManager"):
            yield f"Failed to check that FMG is back online, aborting\n"
            await asyncio.sleep(0.5)
            return
        
        #replace SN's
        yield f"Replacing FGT SNs on FMG\n"
        await asyncio.sleep(0.5)
        if not replace_sn_fmg():
            yield f"Failed to replace SNs on FMG, aborting\n"
            await asyncio.sleep(0.5)
            return
        
        #disable offline mode
        yield f"Disabling offline mode on FMG\n"
        await asyncio.sleep(0.5)
        if not disable_offline_mode(fmg_ip, fmg_user, fmg_password):
            yield f"Failed to disable offline mode, aborting\n"
            await asyncio.sleep(0.5)
            return
        
        #force FMG on FGTs
        yield f"Starting to prepare FGTs\n"
        await asyncio.sleep(10)
        for site_name, site_data in sites.items():
            ip = site_data.get("ip")
            for retry in range(0,30):
                await asyncio.sleep(10)
                if fgt_online(ip,fgt_user,fgt_password):
                    set_fmg(ip,fgt_user,fgt_password)
                    break
                yield f"{site_name} not online yet (retry: {retry + 1})\n"

        #Install policy package
        for retry in range(0,30):
            all_online = True
            for site_name, site_data in sites.items():
                ip = site_data.get("ip")
                if not fgt_online(ip,fgt_user,fgt_password):
                    all_online = False
                    yield f"{site_name} not online yet (retry: {retry + 1})\n"
                    await asyncio.sleep(0.5)
            if all_online:
                break
            else:
                await asyncio.sleep(10)
        yield f"Starting to install the Policy Packages\n"
        await asyncio.sleep(0.5)
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"Creating API user\n")
        fmg_create_api_user(fmg_ip, fmg_user, fmg_password)
        
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"Install Policy Package\n")
        fmg_install_policy(fmg_ip)
        yield f"Installation initiated, please check the status on FMG Task Monitor\n"
        await asyncio.sleep(0.5)
        # Finished
        yield f"Script finished\n"
        await asyncio.sleep(0.5)

    except Exception as e:
        yield f"Failed to restore FMG configuration\n"
        await asyncio.sleep(0.5)
        yield f"{e}"
        await asyncio.sleep(0.5)



@router.get("/saselabtools", response_class=HTMLResponse)
def saselabtools_home():
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

        <!-- Restore SD-WAN deployment for SASE Lab -->
        <div class="section">
            <h2>Restore SD-WAN deployment for SASE Lab</h2>
            <form action="/saselabtools_prepare" method="get">
                <button class="btn danger" type="submit">
                    Reset SD-WAN
                </button>
            </form>
        </div>

        
        <!-- Restore SD-WAN after SPA
        <div class="section">
            <h2>Restore SD-WAN after SPA integration</h2>
            <form action="/saselabtools_spa" method="get">
                <button class="btn danger" type="submit">
                    Restore SPA config
                </button>
            </form>
        </div>  -->

    </div>

    </body>
    </html>
    """



@router.get("/saselabtools_prepare", response_class=HTMLResponse)
def saselabtools_prepare():
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
                const response = await fetch('/saselabtools_prepare_submit');
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

        <!-- Restore SD-WAN deployment for SASE Lab -->
        <div class="section">
            <h2>Restore SD-WAN deployment for SASE Lab</h2>
            
            This will restore the SD-WAN devices to the configuration after SD-WAN Lab is completed, click Proceed to confirm:<br>
            <br>
            <form action="/saselabtools_prepare" method="get">
                <button onclick="prepare(); return false;" type="button"> Proceed </button>
            </form>

            <div id="responseArea">
            </div>
        </div>

    </div>

    </body>
    </html>
    """


@router.get("/saselabtools_spa", response_class=HTMLResponse)
def saselabtools_spa():
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
            async function spa_apply() {
                const response = await fetch('/saselabtools_spa_submit');
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

        <!-- Restore SD-WAN after SPA integration -->
        <div class="section">
            <h2>Restore SD-WAN after SPA integration</h2>
            
            This will restore the SD-WAN devices to the configuration after SPA integration is completed.<br>
            FortiSASE configuration won't be changed, please follow the Lab guide to complete the configuration.<br>
            Click Proceed to confirm:<br>
            <br>
            <form action="/saselabtools_spa" method="get">
                <button onclick="spa_apply(); return false;" type="button"> Proceed </button>
            </form>

            <div id="responseArea">
            </div>
        </div>

    </div>

    </body>
    </html>
    """

@router.get("/saselabtools_prepare_submit")
def saselabtools_prepare_submit():
    return StreamingResponse(prepare_sdwan_devices(), media_type="text/event-stream")


@router.get("/saselabtools_spa_submit")
def saselabtools_spa_submit():
    return StreamingResponse(prepare_sdwan_devices(), media_type="text/event-stream")
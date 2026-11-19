import asyncio
import paramiko
import re
import os
import requests
import socket
import time

from paramiko_expect import SSHClientInteraction
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse

from utils import load_inventory,get_serial

router = APIRouter()

# Default backup for the Proceed buttons; Custom Backup supplies its own URL
BACKUP_URL_766 = 'https://xperts26.s3.sa-east-1.amazonaws.com/2026.04.27_FMG_v7.6.6_pwd_fortinet_sdwan_final.dat'

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

def set_auto_update(host_ip,username,password,mode):
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
        interact.send(f"set auto-update {mode}")
        interact.expect(prompt)
        interact.send("end")
        interact.expect(prompt)
        interact.send("get system admin setting")
        interact.expect(prompt)
        output = interact.current_output_clean
        ssh.close()
        value = re.search(r"auto-update\s*:\s*(\S+)", output)
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"Configuration changes received from managed device -> {mode}: {value.group(1) if value else 'not found'}\n")
        # mode "enable" = Automatically Accept, "disable" = Prompt Administrator to accept
        return bool(value) and value.group(1) == mode
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

async def prepare_sdwan_devices(backup_url=BACKUP_URL_766):
    inventory = load_inventory()
    fmg_ip       = inventory.get("fmg_ip")
    fmg_user     = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})

    steps_total = 8
    current_step = 0
    def at(step, text):
        nonlocal current_step
        current_step = step
        return f"[step {step}/{steps_total}] {text}\n"
    def done(step, text):
        return f"[step {step}/{steps_total}] {text} - DONE ({step}/{steps_total} done, {steps_total - step} missing)\n\n"
    def failed(step, text):
        completed = step - 1
        return (f"[step {step}/{steps_total}] {text} - FAILED at step {step} of {steps_total}\n"
                f"[step {step}/{steps_total}] script stopped: {completed}/{steps_total} done, {steps_total - completed} missing\n")
    yield f"Preparing SD-WAN environment: {steps_total} steps, please do not refresh or close this page\n"
    await asyncio.sleep(0.5)

    try:
        yield at(1, "Download FMG backup - starting")
        await asyncio.sleep(0.5)
        folder_path = '/srv/ftp/'
        file_path = os.path.join(folder_path, 'fmg_sdwan.dat')
        yield at(1, f"backup file: {file_path}")
        yield at(1, f"selected backup release: {backup_url}")
        source_path = file_path + '.source'
        cached_url = ''
        if os.path.isfile(source_path):
            try:
                with open(source_path) as fh:
                    cached_url = fh.read().strip()
            except Exception:
                cached_url = ''
        await asyncio.sleep(0.5)
        
        if (not os.path.isfile(file_path)) or (cached_url != backup_url):
            url = backup_url
            yield at(1, f"staged backup missing or from another release - downloading {url}")
            folder_path = '/srv/ftp/'
            file_path = os.path.join(folder_path, 'fmg_sdwan.dat')
            if os.path.isfile(file_path + '.part'):
                os.remove(file_path + '.part')
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                with open(file_path + '.part', 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
            if os.path.isfile(file_path + '.part'):
                os.replace(file_path + '.part', file_path)
                with open(source_path, 'w') as fh:
                    fh.write(url)
                yield done(1, "Backup downloaded")
            else:
                raise IOError(f"download did not complete - HTTP {response.status_code}")
            await asyncio.sleep(0.5)
        else:
            yield done(1, "Staged backup matches the selected release, download skipped")
            await asyncio.sleep(0.5)
    except Exception as e:
        yield failed(1, f"Could not download FMG backup ({e})")
        await asyncio.sleep(0.5)
        return


    try:
        yield at(2, "Restore FMG configuration - starting")
        await asyncio.sleep(0.5)
        
        restore_fmg_backup(fmg_ip, fmg_user, fmg_password, "fmg_sdwan.dat")
        
        yield at(2, "Restore command sent, FMG is restarting - waiting for it to come back online")
        await asyncio.sleep(10)
        #wait until fmg is back online for 5 minutes
        for retry in range(0,30):
            yield at(2, f"FMG not online yet (waiting, retry {retry + 1}/30, ~{(retry + 1) * 10}s)")
            await asyncio.sleep(10)
            if (device_online(fmg_ip, fmg_user, fmg_password, "A1_FortiManager")):
                break
        
        #check if FMG is back online
        if not device_online(fmg_ip, fmg_user, fmg_password, "A1_FortiManager"):
            yield failed(2, "FMG did not come back online within 5 minutes")
            await asyncio.sleep(0.5)
            return
        
        yield done(2, "FMG back online")

        #right after the backup is restored: hold incoming device changes for review
        yield at(3, "Set Configuration Changes Received from Managed Device = Prompt Administrator to accept - starting")
        await asyncio.sleep(0.5)
        if not set_auto_update(fmg_ip, fmg_user, fmg_password, "disable"):
            yield failed(3, "Could not set Prompt Administrator to accept on FMG")
            await asyncio.sleep(0.5)
            return
        yield done(3, "Prompt Administrator to accept set")

        #replace SN's
        yield at(4, "Replace FGT serial numbers on FMG - starting")
        await asyncio.sleep(0.5)
        if not replace_sn_fmg():
            yield failed(4, "Could not replace FGT serial numbers on FMG")
            await asyncio.sleep(0.5)
            return
        
        #disable offline mode
        yield done(4, "FGT serial numbers replaced")
        yield at(5, "Disable offline mode on FMG - starting")
        await asyncio.sleep(0.5)
        if not disable_offline_mode(fmg_ip, fmg_user, fmg_password):
            yield failed(5, "Could not disable offline mode on FMG")
            await asyncio.sleep(0.5)
            return
        
        #force FMG on FGTs
        yield done(5, "Offline mode disabled")
        yield at(6, "Prepare FGTs (register each FGT with FMG) - starting")
        await asyncio.sleep(10)
        for site_name, site_data in sites.items():
            ip = site_data.get("ip")
            for retry in range(0,30):
                await asyncio.sleep(10)
                if fgt_online(ip,fgt_user,fgt_password):
                    set_fmg(ip,fgt_user,fgt_password)
                    break
                yield at(6, f"{site_name} not online yet (waiting, retry {retry + 1}/30)")

        #Install policy package
        for retry in range(0,30):
            all_online = True
            for site_name, site_data in sites.items():
                ip = site_data.get("ip")
                if not fgt_online(ip,fgt_user,fgt_password):
                    all_online = False
                    yield at(6, f"{site_name} not online yet (waiting, retry {retry + 1}/30)")
                    await asyncio.sleep(0.5)
            if all_online:
                break
            else:
                await asyncio.sleep(10)
        yield done(6, "FGTs prepared and reachable")
        yield at(7, "Install policy packages - starting")
        await asyncio.sleep(0.5)
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"Creating API user\n")
        fmg_create_api_user(fmg_ip, fmg_user, fmg_password)
        
        with open ("/root/tshoot.log", "a+") as f:
            f.write(f"Install Policy Package\n")
        fmg_install_policy(fmg_ip)
        yield done(7, "Policy package install initiated - check FMG Task Monitor for the result")
        await asyncio.sleep(0.5)
        #accept configuration changes received from managed devices (last step)
        yield at(8, "Set Configuration Changes Received from Managed Device = Automatically Accept - starting")
        await asyncio.sleep(0.5)
        if not set_auto_update(fmg_ip, fmg_user, fmg_password, "enable"):
            yield failed(8, "Could not set Automatically Accept on FMG")
            await asyncio.sleep(0.5)
            return

        # Finished
        yield done(8, "Automatically Accept set - last step")
        yield f"ALL {steps_total}/{steps_total} STEPS DONE - script finished, no further output will follow\n"
        await asyncio.sleep(0.5)

    except Exception as e:
        yield failed(current_step, "Unexpected error")
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

    <h1>Automation Portal 1.06</h1>
    <div style="margin:12px 0 20px;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>

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
            async function prepareCustom() {
                const url = prompt('Enter the URL of the backup file (.dat) to restore:');
                if (!url || !url.trim()) { return; }
                await prepare('/saselabtools_prepare_custom?backup_url=' + encodeURIComponent(url.trim()));
            }
            async function prepare(path = '/saselabtools_prepare_submit') {
                const response = await fetch(path, { method: 'POST' });
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                const outputDiv = document.getElementById('responseArea');

                while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                const chunk = decoder.decode(value, { stream: true });
                outputDiv.innerHTML += chunk.replace(/\\r?\\n/g, '<br>');
                }
            }
        </script>
    </head>
    <body>

    <h1>Automation Portal 1.06</h1>
    <div style="margin:12px 0 20px;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>

    <div class="grid">

        <!-- Restore SD-WAN deployment for SASE Lab -->
        <div class="section">
            <h2>Restore SD-WAN deployment for SASE Lab</h2>
            
            This will restore the SD-WAN devices to the configuration after SD-WAN Lab is completed, click Proceed to confirm:<br>
            <b>Proceed</b> restores the 7.6.6 backup (S3); <b>Custom Backup</b> prompts for the URL of the backup to restore.<br>
            <br>
            <form action="/saselabtools_prepare" method="get">
                <button onclick="prepare(); return false;" type="button"> Proceed </button>
                <button onclick="prepareCustom(); return false;" type="button" style="margin-left:10px; background:#17a2b8; color:white; border:none; padding:12px 18px; border-radius:8px; font-size:14px; font-weight:500; cursor:pointer;"> Custom Backup </button>
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
                const response = await fetch('/saselabtools_spa_submit', { method: 'POST' });
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

    <h1>Automation Portal 1.06</h1>
    <div style="margin:12px 0 20px;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>

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

@router.post("/saselabtools_prepare_submit")
def saselabtools_prepare_submit():
    return StreamingResponse(prepare_sdwan_devices(), media_type="text/event-stream")


@router.post("/saselabtools_prepare_custom")
def saselabtools_prepare_custom(backup_url: str = ""):
    backup_url = backup_url.strip()
    if not backup_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="backup_url must be an http(s) URL")
    return StreamingResponse(prepare_sdwan_devices(backup_url), media_type="text/event-stream")


@router.post("/saselabtools_spa_submit")
def saselabtools_spa_submit():
    return StreamingResponse(prepare_sdwan_devices(), media_type="text/event-stream")
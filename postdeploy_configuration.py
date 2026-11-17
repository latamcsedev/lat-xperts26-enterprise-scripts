import os
import subprocess
import sys
import time
from pathlib import Path

import paramiko
import yaml
from paramiko_expect import SSHClientInteraction
from scp import SCPClient
from check_all_devices_online import device_online

# web-int sits beside this script (repo checkout or /opt/lat-scripts).
WEB_INT_DIR = Path(__file__).resolve().parent / "web-int"
if str(WEB_INT_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_INT_DIR))

# Same parser the portal's Public IP Fix button uses, so both read the table alike.
from fgt_ext_default_routes import DEFAULT_DST, parse_routing_table  # noqa: E402

def load_inventory():
    with open("/opt/lat-scripts/web-int/inventory.yaml", "r") as f:
        return yaml.safe_load(f)

def copy_file(host_ip, username, password, source_file, destination_file):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
    except Exception as e:
        print(f"Login error on {host_ip}")
        return
    
    time.sleep(2)

    try:
        scp = SCPClient(ssh.get_transport())
        scp.put(source_file, destination_file)
        scp.close()
    except:
        print(f"Error to copy file {source_file}")
        return



def apply_configuration(host_ip, username, password, commands_text):
    commands = commands_text.splitlines()
    prompt = ".*[ ~]#.*"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
    except Exception as e:
        print(f"Login error on {host_ip}")
        return
    
    time.sleep(2)
    interact = SSHClientInteraction(ssh, timeout=10, display=True)
    interact.expect(prompt)

    try:
        for command in commands:
            interact.send(command)
            interact.expect(prompt)
    except:
        print(f"Error to apply {command}")
        return


def z1_wan_ports(inventory):
    """WAN ports z1_external must carry a default on: routecheck.z1_external's
    default_routes minus its reference port (routecheck counts port10 too)."""
    routecheck = (inventory.get("routecheck") or {}).get("z1_external") or {}
    ports = routecheck.get("default_routes") or []
    reference = routecheck.get("reference_interface")
    return [str(p) for p in ports if p != reference]


def read_routing_table(host_ip, username, password):
    """The live routing table of a FortiGate, or None when it cannot be read."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host_ip, username=username, password=password, timeout=10)
        try:
            _stdin, stdout, stderr = ssh.exec_command("get router info routing-table all", timeout=30)
            output = stdout.read().decode(errors="ignore")
            error = stderr.read().decode(errors="ignore")
        finally:
            ssh.close()
    except Exception as exc:
        print(f"Could not read the routing table from {host_ip}: {exc}")
        return None

    if not output.strip():
        print(f"No answer for 'get router info routing-table all' on {host_ip}: {error.strip()}")
        return None
    return output


def public_ip_fix_check(inventory, zz_ext_ip, fgt_user, fgt_password):
    """Run the Public IP Fix only when z1_external has no default on NONE of
    the checked WAN ports (a full miss). A partially routed table is reported
    and left alone."""
    wan_ports = z1_wan_ports(inventory)
    if not wan_ports:
        print("routecheck.z1_external has no default_routes — Public IP Fix check skipped")
        return

    table = read_routing_table(zz_ext_ip, fgt_user, fgt_password)
    if table is None:
        print("z1_external routing table unreadable — Public IP Fix check skipped")
        return

    routes = parse_routing_table(table)
    if not routes:
        print(f"Nothing parsed out of {zz_ext_ip}'s routing table — Public IP Fix check skipped")
        return

    with_default = {route["iface"] for route in routes if route["dst"] == DEFAULT_DST}
    missing = [port for port in wan_ports if port not in with_default]

    if not missing:
        print(f"z1_external has a {DEFAULT_DST} route on {', '.join(wan_ports)} — Public IP Fix not needed")
        return

    if len(missing) != len(wan_ports):
        print(
            "z1_external has a default on some checked ports but not on "
            + ", ".join(missing)
            + " — only a full miss runs the Public IP Fix, so leaving it alone"
        )
        return

    print(f"z1_external has no {DEFAULT_DST} route on {', '.join(wan_ports)} — running the Public IP Fix")
    run_public_ip_fix()


def run_public_ip_fix():
    """Run the same script the portal's Public IP Fix button runs, streaming its
    output into the deploy log. Non-fatal either way."""
    script = WEB_INT_DIR / "fgt_ext_default_routes.py"
    print(f"Running {script}")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        result = subprocess.run([sys.executable, str(script)], check=False, env=env)
    except Exception as exc:
        print(f"Public IP Fix could not be started: {exc}")
        return
    if result.returncode:
        print(f"Public IP Fix exited {result.returncode} — see its output above")
    else:
        print("Public IP Fix finished")


def main():
    inventory = load_inventory()
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    zz_ext_ip = inventory.get("zz_ext_ip")

    #----- Public IP Fix check
    # GCP hands z1_external no default route on the secondary NICs it cables to
    # port1-port5. When NONE of the routecheck WAN ports carries one, run the
    # Public IP Fix script (fgt_ext_default_routes.py). Everything here is
    # non-fatal: deploy.sh runs this with set -e.
    if not zz_ext_ip:
        print("inventory has no zz_ext_ip — Public IP Fix check skipped")
    elif device_online(zz_ext_ip, fgt_user, fgt_password, "z1_external"):
        public_ip_fix_check(inventory, zz_ext_ip, fgt_user, fgt_password)
    else:
        # z1_external offline, retry for 5 minutes
        online = False
        for retry in range(0, 5):
            time.sleep(60)
            if device_online(zz_ext_ip, fgt_user, fgt_password, "z1_external"):
                public_ip_fix_check(inventory, zz_ext_ip, fgt_user, fgt_password)
                online = True
                break
        if not online:
            print("z1_external stayed offline for 5 minutes — Public IP Fix check skipped")

    #----- First fix
    # JMC - Added to Golden Image on May27
    # create DNS zone and entries on z_ext and disable policy route for FOSv8 due to issues reaching FSASE
    # commands = """
    # config system dns-database
    #     edit "xperts26.com"
    #         set domain "xperts26.com"
    #         config dns-entry
    #             edit 1
    #                 set hostname "fmg80"
    #                 set ip 205.0.115.9
    #             next
    #             edit 2
    #                 set hostname "branch80"
    #                 set ip 205.0.115.9
    #             next
    #             edit 3
    #                 set hostname "finance"
    #                 set ip 100.64.1.9
    #             next
    #             edit 4
    #                 set hostname "crm"
    #                 set ip 100.64.1.9
    #             next
    #         end
    #     next
    # end
    # config system interface
    #     edit port1
    #         unset mtu-override
    #     next
    #     edit port2
    #         unset mtu-override
    #     next
    #     edit port3
    #         unset mtu-override
    #     next
    #     edit port4
    #         unset mtu-override
    #     next
    #     edit port5
    #         unset mtu-override
    #     next
    #     edit port6
    #         unset mtu-override
    #     next
    #     edit port7
    #         unset mtu-override
    #     next
    # end
    # """
    # if (device_online(zz_ext_ip,fgt_user,fgt_password,"zz_ext")):
    #     apply_configuration(zz_ext_ip, fgt_user, fgt_password, commands)
    # else:
    #     #zz_ext offline, retry for 5 minutes
    #     for retry in range(0,5):
    #         time.sleep(60)
    #         if (device_online(zz_ext_ip,fgt_user,fgt_password,"zz_ext")):
    #             apply_configuration(zz_ext_ip, fgt_user, fgt_password, commands)
    #             break


    #----- Second fix
    # create Finance and CRM server on cli1H1
    cli1H1_ip = "10.254.1.12"
    cli1H1_user = "root"
    with open ("/opt/lat-scripts/sase-utils/commands_cli1H1.txt", "r") as f:
        commands = f.read()
    if (device_online(cli1H1_ip,cli1H1_user,fgt_password,"cli1H1")):
        #Copy files
        copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/finance_server.py','/root/finance_server.py')
        copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/finance_server.service','/etc/systemd/system/finance_server.service')
        copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/crm_server.py','/root/crm_server.py')
        copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/crm_server.service','/etc/systemd/system/crm_server.service')
        apply_configuration(cli1H1_ip, cli1H1_user, fgt_password, commands)
    else:
        #cli1H1 offline, retry for 5 minutes
        for retry in range(0,5):
            time.sleep(60)
            if (device_online(cli1H1_ip,cli1H1_user,fgt_password,"cli1H1")):
                copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/finance_server.py','/root/finance_server.py')
                copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/finance_server.service','/etc/systemd/system/finance_server.service')
                copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/crm_server.py','/root/crm_server.py')
                copy_file(cli1H1_ip,cli1H1_user,fgt_password,'/opt/lat-scripts/sase-utils/crm_server.service','/etc/systemd/system/crm_server.service')
                apply_configuration(cli1H1_ip, cli1H1_user, fgt_password, commands)
                break

    # create HR server on cli1H2
    cli1H2_ip = "10.254.1.13"
    cli1H2_user = "root"
    with open ("/opt/lat-scripts/sase-utils/commands_cli1H2.txt", "r") as f:
        commands = f.read()
    if (device_online(cli1H2_ip,cli1H1_user,fgt_password,"cli1H1")):
        #Copy files
        copy_file(cli1H2_ip,cli1H2_user,fgt_password,'/opt/lat-scripts/sase-utils/hr_server.py','/root/hr_server.py')
        copy_file(cli1H2_ip,cli1H2_user,fgt_password,'/opt/lat-scripts/sase-utils/hr_server.service','/etc/systemd/system/hr_server.service')
        apply_configuration(cli1H2_ip, cli1H2_user, fgt_password, commands)
    else:
        #cli1H1 offline, retry for 5 minutes
        for retry in range(0,5):
            time.sleep(60)
            if (device_online(cli1H2_ip,cli1H2_user,fgt_password,"cli1H1")):
                copy_file(cli1H2_ip,cli1H2_user,fgt_password,'/opt/lat-scripts/sase-utils/hr_server.py','/root/hr_server.py')
                copy_file(cli1H2_ip,cli1H2_user,fgt_password,'/opt/lat-scripts/sase-utils/hr_server.service','/etc/systemd/system/hr_server.service')
                apply_configuration(cli1H2_ip, cli1H2_user, fgt_password, commands)
                break


    #----- Third fix
    # JMC - Added to Golden Image on May27
    # Enable port6 on branch80
    # fgt2v8 = "10.254.1.25"
    # commands = """
    # config system interface
    #     edit port6
    #     set allowaccess https 
    # end
    # """
    # if (device_online(fgt2v8,fgt_user,fgt_password,"fgt2-v8")):
    #     apply_configuration(fgt2v8, fgt_user, fgt_password, commands)
    # else:
    #     #fgt2v8 offline, retry for 5 minutes
    #     for retry in range(0,5):
    #         time.sleep(60)
    #         if (device_online(fgt2v8,fgt_user,fgt_password,"fgt2-v8")):
    #             apply_configuration(fgt2v8, fgt_user, fgt_password, commands)
    #             break


if __name__ == "__main__":
    main()
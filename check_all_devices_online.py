import paramiko
import yaml
import time
import socket
from paramiko_expect import SSHClientInteraction

def load_inventory():
    with open("./web-int/inventory.yaml", "r") as f:
        return yaml.safe_load(f)

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
    

def main():
    inventory = load_inventory()
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    
    sites = inventory.get("licensecheck", {})
    for site_name, site_data in sites.items():
        ip = site_data.get("ip")
        device_online(ip,fgt_user,fgt_password,site_name)


if __name__ == "__main__":
    main()
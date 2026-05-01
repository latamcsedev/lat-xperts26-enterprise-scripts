import paramiko
import yaml
import time
from paramiko_expect import SSHClientInteraction
from check_all_devices_online import device_online

def load_inventory():
    with open("/opt/lat-scripts/web-int/inventory.yaml", "r") as f:
        return yaml.safe_load(f)

def apply_configuration(host_ip, username, password, commands_text):
    commands = commands_text.splitlines()
    prompt = ".* #.*"
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


def main():
    inventory = load_inventory()
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    zz_ext_ip = inventory.get("zz_ext_ip")
    
    #----- First fix
    # create DNS zone and entries on z_ext
    commands = """
    config system dns-database
        edit "xperts26.com"
            set domain "xperts26.com"
            config dns-entry
                edit 1
                    set hostname "fmg80"
                    set ip 205.0.115.9
                next
                edit 2
                    set hostname "branch80"
                    set ip 205.0.115.9
                next
            end
        next
    end
    """
    if (device_online(zz_ext_ip,fgt_user,fgt_password,"zz_ext")):
        apply_configuration(zz_ext_ip, fgt_user, fgt_password, commands)
    else:
        #zz_ext offline, retry for 5 minutes
        for retry in range(0,5):
            time.sleep(60)
            if (device_online(zz_ext_ip,fgt_user,fgt_password,"zz_ext")):
                apply_configuration(zz_ext_ip, fgt_user, fgt_password, commands)
                break



if __name__ == "__main__":
    main()
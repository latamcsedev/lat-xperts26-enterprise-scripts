import csv
import io
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from utils import load_inventory, get_serial

router = APIRouter()

@router.get("/download")
def download_csv():

    inventory = load_inventory()

    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites", {})

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "Serial Number",
            "Device Blueprint",
            "Name",
            "vm_interface_number",
            "hostname",
            "loopback",
            "isp1_intf",
            "isp2_intf",
            "mpls_intf",
            "lan_intf",
            "lan_ip",
            "mgmt_ip",
        ],
    )

    writer.writeheader()

    for site_name, site_data in sites.items():

        serial = get_serial(
            site_data.get("ip"),
            fgt_user,
            fgt_password
        )

        writer.writerow({
            "Serial Number": serial,
            "Device Blueprint": site_data.get("blueprint", ""),
            "Name": site_name,
            "vm_interface_number": "10",  # not defined in YAML
            "hostname": site_name,
            "loopback": site_data.get("loopback", ""),
            "isp1_intf": site_data.get("isp1_intf", ""),
            "isp2_intf": site_data.get("isp2_intf", ""),
            "mpls_intf": site_data.get("mpls_intf", ""),
            "lan_intf": site_data.get("lan_intf", ""),
            "lan_ip": site_data.get("lan_ip", ""),
            "mgmt_ip": site_data.get("mgmt_ip", ""),
        })

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=fortigate_inventory.csv"
        },
    )

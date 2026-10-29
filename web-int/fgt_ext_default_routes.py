#!/usr/bin/env python3
"""Install ECMP default routes on FGT_EXT port1-port5.

Fabric Studio on this GCP pod cables host NICs to z1_external (FGT_EXT):

    nic1 ens5 -> port1
    nic2 ens6 -> port2
    nic3 ens7 -> port3
    nic4 ens8 -> port4
    nic5 ens9 -> port5

GCP DHCP on those secondary NICs hands out a /32 and a subnet route via the
VPC gateway. It does not hand out a default route. This script adds a static
0.0.0.0/0 on each of port1-port5 via that learned gateway, with the same
distance and priority as the existing port10 default, so FortiOS ECMP-installs
all of them.

Run on the ToolHost (inventory zz_ext_ip, SSH port 22):

    python3 fgt_ext_default_routes.py

Override the target when reaching the FortiGate through a published port:

    python3 fgt_ext_default_routes.py --host <pod-fqdn> --port 11000
"""

import argparse
import re
import socket
import sys
import time
from pathlib import Path

import paramiko
import yaml

WAN_PORTS = ("port1", "port2", "port3", "port4", "port5")
REF_PORT = "port10"
DEFAULT_DST = "0.0.0.0/0"

_ROUTE_RE = re.compile(
    r"^(?P<code>\S+)\s+"
    r"(?P<dst>\d+\.\d+\.\d+\.\d+/\d+)\s+"
    r"(?:\[(?P<dist>\d+)/(?P<metric>\d+)\]\s+)?"
    r"(?:via\s+(?P<gw>\d+\.\d+\.\d+\.\d+),\s+)?"
    r"(?:is directly connected,\s+)?"
    r"(?P<iface>[A-Za-z0-9_.-]+)"
    r"(?:,\s+\[(?P<prio>\d+)/(?P<weight>\d+)\])?"
    r"\s*$"
)
# ECMP nexthops are printed under the first path, without repeating the prefix:
#                   [5/0] via 10.104.2.1, port2, [1/0]
_ECMP_RE = re.compile(
    r"^\[(?P<dist>\d+)/(?P<metric>\d+)\]\s+"
    r"(?:via\s+(?P<gw>\d+\.\d+\.\d+\.\d+),\s+)?"
    r"(?:is directly connected,\s+)?"
    r"(?P<iface>[A-Za-z0-9_.-]+)"
    r"(?:,\s+\[(?P<prio>\d+)/(?P<weight>\d+)\])?"
    r"\s*$"
)


def load_inventory(path):
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    host = data.get("zz_ext_ip")
    user = data.get("fgt_user")
    password = data.get("fgt_password")
    if not host or not user or not password:
        raise SystemExit(f"{path} must set zz_ext_ip, fgt_user, and fgt_password")
    return host, user, password


def _route_from_match(match, dst, code, selected):
    return {
        "code": code,
        "dst": dst,
        "distance": int(match.group("dist")) if match.group("dist") else None,
        "gateway": match.group("gw"),
        "iface": match.group("iface"),
        "priority": int(match.group("prio")) if match.group("prio") else 1,
        "selected": selected,
    }


def parse_routing_table(text):
    routes = []
    current_dst = None
    current_code = ""
    current_selected = False
    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        match = _ROUTE_RE.match(line)
        if match:
            current_dst = match.group("dst")
            current_code = match.group("code")
            current_selected = "*" in current_code
            routes.append(_route_from_match(match, current_dst, current_code, current_selected))
            continue
        # A gateway-less "directly connected" line under a default is the
        # recursive resolution of an ECMP nexthop, not another default route.
        ecmp = _ECMP_RE.match(line)
        if ecmp and current_dst and ecmp.group("gw"):
            routes.append(_route_from_match(ecmp, current_dst, current_code, current_selected))
    return routes


def parse_static_config(text):
    """Return configured static routes keyed by edit id."""
    routes = []
    current = None
    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        edit = re.match(r"edit\s+(\d+)", line)
        if edit:
            current = {
                "id": int(edit.group(1)),
                "dst": "0.0.0.0 0.0.0.0",
                "gateway": None,
                "device": None,
                "distance": 10,
                "priority": 1,
            }
            continue
        if current is None:
            continue
        if line == "next":
            routes.append(current)
            current = None
            continue
        gateway = re.match(r"set gateway\s+(\S+)", line)
        if gateway:
            current["gateway"] = gateway.group(1)
            continue
        device = re.match(r'set device\s+"?([^"\s]+)"?', line)
        if device:
            current["device"] = device.group(1)
            continue
        distance = re.match(r"set distance\s+(\d+)", line)
        if distance:
            current["distance"] = int(distance.group(1))
            continue
        priority = re.match(r"set priority\s+(\d+)", line)
        if priority:
            current["priority"] = int(priority.group(1))
            continue
        dst = re.match(r"set dst\s+(\S+)\s+(\S+)", line)
        if dst:
            current["dst"] = f"{dst.group(1)} {dst.group(2)}"
    return routes


def _is_default(route):
    return route["dst"] == DEFAULT_DST


def reference_default(routes):
    """Distance and priority of the port10 default route."""
    candidates = [route for route in routes if _is_default(route) and route["iface"] == REF_PORT]
    if not candidates:
        raise SystemExit(f"No {DEFAULT_DST} route on {REF_PORT} to copy distance and priority from")
    selected = [route for route in candidates if route["selected"]]
    chosen = selected[0] if selected else candidates[0]
    if chosen["distance"] is None:
        raise SystemExit(f"{REF_PORT} default route has no distance")
    return chosen["distance"], chosen["priority"], chosen["gateway"]


def wan_gateways(routes):
    """Next hop of the DHCP subnet route on each WAN port.

    GCP installs 10.104.x.0/24 via 10.104.x.1 and an on-link /32 for that
    gateway. The /24's via address is the default gateway to install.
    """
    found = {}
    for port in WAN_PORTS:
        via_routes = [
            route
            for route in routes
            if route["iface"] == port and route["gateway"] and not _is_default(route)
        ]
        if not via_routes:
            raise SystemExit(
                f"{port} has no DHCP gateway route. It needs a lease before a default can be added."
            )
        subnet = [route for route in via_routes if not route["dst"].endswith("/32")]
        chosen = subnet[0] if subnet else via_routes[0]
        found[port] = chosen["gateway"]
    return found


def plan_changes(static_routes, gateways, distance, priority):
    """Return (edits, creates). edits is a list of (id, port, gateway)."""
    by_port = {}
    for route in static_routes:
        dst = route["dst"].replace(" ", "/")
        if dst not in (DEFAULT_DST, "0.0.0.0/0.0.0.0"):
            continue
        if route["device"] in WAN_PORTS:
            by_port.setdefault(route["device"], route)

    edits = []
    creates = []
    unchanged = []
    for port in WAN_PORTS:
        gateway = gateways[port]
        existing = by_port.get(port)
        if existing is None:
            creates.append((port, gateway))
            continue
        same = (
            existing["gateway"] == gateway
            and existing["distance"] == distance
            and existing["priority"] == priority
        )
        if same:
            unchanged.append((existing["id"], port, gateway))
        else:
            edits.append((existing["id"], port, gateway))
    return edits, creates, unchanged


class FortiGate:
    def __init__(self, host, port, user, password):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.ssh = None
        self.shell = None

    def connect(self):
        try:
            self.ssh = self._login(self.password)
        except Exception as first_error:
            try:
                self.ssh = self._login("")
            except Exception:
                raise first_error
            self._accept_new_password()
            self.ssh.close()
            self.ssh = self._login(self.password)
        self.shell = self.ssh.invoke_shell(width=200, height=2000)
        time.sleep(0.8)
        self._read(timeout=8)

    def close(self):
        if self.ssh is not None:
            self.ssh.close()

    def command(self, command, timeout=25):
        self.shell.send(command + "\n")
        time.sleep(0.2)
        return self._read(timeout=timeout)

    def _login(self, password):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=password,
            timeout=20,
            banner_timeout=30,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        return ssh

    def _accept_new_password(self):
        shell = self.ssh.invoke_shell(width=200, height=80)
        time.sleep(0.8)
        banner = self._read_shell(shell, timeout=8)
        if "password" not in banner.lower():
            return
        shell.send(self.password + "\n")
        time.sleep(0.4)
        follow = self._read_shell(shell, timeout=8)
        if "password" in follow.lower():
            shell.send(self.password + "\n")
            time.sleep(0.4)
            self._read_shell(shell, timeout=8)

    def _read(self, timeout):
        return self._read_shell(self.shell, timeout)

    @staticmethod
    def _read_shell(shell, timeout):
        accumulated = ""
        shell.settimeout(0.8)
        start = time.time()
        last = time.time()
        while time.time() - start < timeout:
            try:
                chunk = shell.recv(16384).decode(errors="ignore")
            except socket.timeout:
                tail = accumulated.rstrip()
                if tail and re.search(r"(?m)(?:^|\n)[^\n]*[#\$]\s*$", tail) and time.time() - last > 0.5:
                    break
                if accumulated and time.time() - last > 2.5:
                    break
                continue
            if not chunk:
                break
            accumulated += chunk
            last = time.time()
            if re.search(r"--More--\s*$", accumulated.rstrip()):
                shell.send(" ")
        return accumulated


def _fail_if_cli_error(output, what):
    if re.search(r"Command fail|command parse error|Unknown action|Return code -", output, re.I):
        raise SystemExit(f"{what} failed:\n{output}")


def apply_routes(fgt, edits, creates, distance, priority):
    output = fgt.command("config router static")
    _fail_if_cli_error(output, "config router static")
    try:
        for edit_id, port, gateway in edits:
            _push_route(fgt, str(edit_id), port, gateway, distance, priority)
        for port, gateway in creates:
            _push_route(fgt, "0", port, gateway, distance, priority)
    except Exception:
        fgt.command("abort")
        raise
    end_out = fgt.command("end")
    _fail_if_cli_error(end_out, "end")
    return end_out


def _push_route(fgt, edit_id, port, gateway, distance, priority):
    steps = [
        f"edit {edit_id}",
        "set dst 0.0.0.0 0.0.0.0",
        f"set device \"{port}\"",
        f"set gateway {gateway}",
        f"set distance {distance}",
        f"set priority {priority}",
        "set comment \"ECMP default via GCP NIC\"",
        "next",
    ]
    for step in steps:
        output = fgt.command(step, timeout=15)
        _fail_if_cli_error(output, step)


def verify(routes, gateways, distance, priority):
    missing = []
    for port, gateway in gateways.items():
        match = [
            route
            for route in routes
            if _is_default(route)
            and route["iface"] == port
            and route["gateway"] == gateway
            and route["distance"] == distance
            and route["priority"] == priority
        ]
        if not match:
            missing.append(f"{port} via {gateway} distance {distance} priority {priority}")
    ref = [
        route
        for route in routes
        if _is_default(route) and route["iface"] == REF_PORT and route["distance"] == distance
    ]
    if not ref:
        missing.append(f"{REF_PORT} default route was removed")
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        default=str(Path(__file__).with_name("inventory.yaml")),
        help="Inventory with zz_ext_ip, fgt_user, and fgt_password",
    )
    parser.add_argument("--host", help="Override zz_ext_ip")
    parser.add_argument("--port", type=int, default=22, help="SSH port (22 on the lab network)")
    parser.add_argument("--dry-run", action="store_true", help="Print the routes and do not change the FortiGate")
    args = parser.parse_args(argv)

    host, user, password = load_inventory(args.inventory)
    if args.host:
        host = args.host

    print(f"Connecting to {host}:{args.port} as {user}")
    fgt = FortiGate(host, args.port, user, password)
    fgt.connect()
    try:
        table = fgt.command("get router info routing-table all")
        static_text = fgt.command("show router static")
        routes = parse_routing_table(table)
        static_routes = parse_static_config(static_text)
        distance, priority, ref_gw = reference_default(routes)
        gateways = wan_gateways(routes)
        edits, creates, unchanged = plan_changes(static_routes, gateways, distance, priority)

        print(f"Reference default: {DEFAULT_DST} via {ref_gw} {REF_PORT} distance {distance} priority {priority}")
        for port in WAN_PORTS:
            print(f"  {port} gateway {gateways[port]}")
        if unchanged:
            print("Already installed: " + ", ".join(f"{port} (edit {edit_id})" for edit_id, port, _gw in unchanged))
        if not edits and not creates:
            print("No config changes needed")
        else:
            for edit_id, port, gateway in edits:
                print(f"Update edit {edit_id}: {DEFAULT_DST} via {gateway} {port} distance {distance} priority {priority}")
            for port, gateway in creates:
                print(f"Create: {DEFAULT_DST} via {gateway} {port} distance {distance} priority {priority}")

        if args.dry_run:
            print("Dry run; configuration was not changed")
            return 0
        if edits or creates:
            apply_routes(fgt, edits, creates, distance, priority)

        check = fgt.command("get router info routing-table all")
        installed = parse_routing_table(check)
        missing = verify(installed, gateways, distance, priority)
        print("Default routes now in the routing table:")
        for route in installed:
            if _is_default(route) and route["iface"] in WAN_PORTS + (REF_PORT,):
                mark = "*" if route["selected"] else " "
                print(
                    f"  {mark} {route['dst']} via {route['gateway']} {route['iface']} "
                    f"distance {route['distance']} priority {route['priority']}"
                )
        if missing:
            print("Missing from the routing table:", file=sys.stderr)
            for item in missing:
                print(f"  {item}", file=sys.stderr)
            return 1
        print("port1-port5 defaults are installed and match port10 distance and priority")
        return 0
    finally:
        fgt.close()


if __name__ == "__main__":
    sys.exit(main())

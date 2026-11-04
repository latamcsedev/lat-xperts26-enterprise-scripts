import html
import json
import os
import paramiko
import re
import socket
import threading
import time

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from utils import api_delete, api_get, api_post, load_inventory

router = APIRouter()
CACHE_PATH = "labstatus_cache.json"
PARTIAL_CACHE_PATH = "labstatus_partial.json"
REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
}

PASSING_STATUSES = {"valid", "warning", "duplicated"}

# A run that has not advanced for this long is treated as abandoned and a new
# refresh may take over. The old worker thread is not killed, but every state
# and cache write it makes is dropped, because its run_id no longer matches.
JOB_STALE_SECONDS = 600


def get_runtime_device_map(retries=2):
    """name -> runtime id. Retries once, then raises: a silently empty map used to
    turn every power row into a bogus "not found" result."""
    last = None
    for _ in range(max(1, retries)):
        try:
            response = api_get("/api/v1/runtime/device")
            objects = response.get("object", [])
            return {obj.get("name"): obj.get("id") for obj in objects if obj.get("name") and obj.get("id")}
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise last


def clean_license_output(output):
    if output is None:
        return None
    output = output.strip()
    if not output:
        return None
    output = re.sub(r"\s*--More--\s*", " ", output)
    prompt_start = re.compile(r"^[A-Za-z0-9_.-]+(?:VMSTM|VM|EXT|80)?\s*#\s*", re.IGNORECASE)
    prompt_end = re.compile(r"\s+[A-Za-z0-9_.-]+(?:VMSTM|VM|EXT|80)?\s*#\s*$", re.IGNORECASE)
    lines = output.splitlines()
    license_lines = []
    for line in lines:
        if re.search(r"license", line, re.IGNORECASE):
            cleaned = line.strip()
            cleaned = prompt_start.sub("", cleaned)
            cleaned = prompt_end.sub("", cleaned).strip()
            if cleaned:
                license_lines.append(cleaned)
    for line in license_lines:
        if re.search(r"(Expiration|Valid|Warning|Expired|Invalid|Status|Date)", line, re.IGNORECASE):
            return line
    if license_lines:
        return license_lines[0]
    return None

job_lock = threading.Lock()
job_state = {
    "running": False,
    "progress": 0,
    "phase": "idle",
    "message": "Idle",
    "error": None,
    "run_id": 0,
    "updated_at": time.time(),
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data, run_id=None):
    if run_id is not None:
        with job_lock:
            if job_state.get("run_id") != run_id:
                return  # a newer run took over
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _save_partial(data, run_id=None):
    if run_id is not None:
        with job_lock:
            if job_state.get("run_id") != run_id:
                return  # a newer run took over
    with open(PARTIAL_CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _load_partial():
    if not os.path.exists(PARTIAL_CACHE_PATH):
        return None
    try:
        with open(PARTIAL_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _clear_partial(run_id=None):
    if run_id is not None:
        with job_lock:
            if job_state.get("run_id") != run_id:
                return  # a newer run took over
    try:
        if os.path.exists(PARTIAL_CACHE_PATH):
            os.remove(PARTIAL_CACHE_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

def _set_state(running, progress, phase, message, error=None, run_id=None):
    with job_lock:
        if run_id is not None and job_state.get("run_id") != run_id:
            return  # a newer run took over
        job_state.update({
            "running": running,
            "progress": progress,
            "phase": phase,
            "message": message,
            "error": error,
            "updated_at": time.time(),
        })


def _load_data():
    with job_lock:
        if job_state["running"]:
            partial = _load_partial()
            if partial:
                return partial
    return _load_cache() or {
        "last_run": None,
        "failed_count": 0,
        "power_results": [],
        "license_results": [],
        "route_results": [],
    }


# ---------------------------------------------------------------------------
# License check: invoke_shell-based so --More-- pagination works on FortiGate 8.0.
# Tries 'get system status | grep -i license' first (fast, no pagination needed).
# Falls back to full 'get system status' for devices that don't support grep
# (e.g. FortiAnalyzer), handling every --More-- prompt via the interactive shell.
# ---------------------------------------------------------------------------

def _read_shell_until_prompt(shell, timeout=25):
    """Read from an interactive shell until the device prompt reappears or timeout."""
    accumulated = ""
    shell.settimeout(1.0)
    start = time.time()
    last_data = time.time()

    while time.time() - start < timeout:
        try:
            chunk = shell.recv(4096).decode(errors="ignore")
            if chunk:
                accumulated += chunk
                last_data = time.time()
                # Send space whenever --More-- appears at the end of current buffer
                if re.search(r"--More--\s*$", accumulated.rstrip()):
                    shell.send(" ")
                    time.sleep(0.15)
        except socket.timeout:
            # 1 s of silence — check for a prompt (e.g. "Hub80 # ")
            if re.search(r"\w[\w.-]*\s*#\s*$", accumulated.rstrip()):
                break
            # After 3 s of silence with no prompt, give up
            if time.time() - last_data > 3.0:
                break

    return accumulated


def _get_license_status(host, username, password, timeout=30):
    """
    PTY-based license status check.
    1. Tries 'get system status | grep -i license' (FortiGate, no pagination).
    2. Falls back to full 'get system status' with --More-- handling (FortiAnalyzer etc.).
    Adds 'duplicated' to the recognised statuses and always returns 'display_output'
    with the full SSH output for the troubleshooting collapsible.
    """
    def try_login(passwd, ssh_timeout=10):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=username, password=passwd, timeout=ssh_timeout,
                    banner_timeout=15, auth_timeout=20)
        return ssh

    ssh = None
    try:
        try:
            ssh = try_login(password)
        except Exception as first_err:
            try:
                ssh = try_login("", ssh_timeout=8)
                sh = ssh.invoke_shell(timeout=5)
                time.sleep(0.5)
                out = sh.recv(5000).decode(errors="ignore")
                if "change your password" in out.lower():
                    sh.send(password + "\n")
                    time.sleep(0.5)
                    sh.send(password + "\n")
                    time.sleep(1)
                ssh.close()
                ssh = try_login(password)
            except Exception:
                raise first_err

        shell = ssh.invoke_shell()
        time.sleep(1)
        # Drain the banner / initial prompt
        try:
            while shell.recv_ready():
                shell.recv(8192)
                time.sleep(0.1)
        except Exception:
            pass

        # ---- Attempt 1: grep (FortiGate) ----
        shell.send("get system status | grep -i license\n")
        time.sleep(1.5)
        grep_out = _read_shell_until_prompt(shell, timeout=8)

        # The echoed command line itself contains the word "license", so it has to
        # be stripped before deciding whether grep produced anything. Otherwise
        # devices that accept the pipe but return nothing (FortiManager) look
        # like a successful grep and the full-output fallback never runs — the
        # row then reports "parse error" instead of the real license status.
        grep_body = "\n".join(
            line for line in grep_out.splitlines()
            if "get system status" not in line.lower()
        )

        grep_ok = (
            re.search(r"license", grep_body, re.IGNORECASE)
            and not re.search(r"(invalid input|command fail|unknown action|parse error)", grep_out, re.IGNORECASE)
        )

        if grep_ok:
            raw_output = grep_out
            # Parse directly: grep output only has license lines, but the command echo
            # ("get system status | grep -i license") also contains "status", which
            # confuses clean_license_output. Skip the echo and prompt lines ourselves.
            output_line = None
            for line in grep_out.splitlines():
                s = line.strip()
                if not s:
                    continue
                if re.search(r"get system status", s, re.IGNORECASE):
                    continue
                if re.match(r"[A-Za-z0-9_][\w.-]*\s*#", s):
                    continue
                if re.search(r"license", s, re.IGNORECASE):
                    output_line = s
                    break
        else:
            # ---- Attempt 2: full output (FortiAnalyzer / devices without grep) ----
            shell.send("get system status\n")
            time.sleep(1)
            raw_output = _read_shell_until_prompt(shell, timeout=25)
            output_line = clean_license_output(raw_output)

        ssh.close()

        if output_line is None:
            return {"status": "parse error", "output_line": raw_output, "display_output": raw_output}

        # Match status including 'duplicated'
        match = re.search(
            r"(Valid|Warning|Expired|Invalid|Unknown|Error|Duplicated)",
            output_line, re.IGNORECASE,
        )
        if not match:
            # Try to infer from expiration date
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", output_line)
            if date_match:
                from datetime import datetime
                try:
                    exp = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                    now = datetime.now()
                    if exp < now:
                        status = "expired"
                    elif (exp - now).days <= 30:
                        status = "warning"
                    else:
                        status = "valid"
                    return {"status": status, "output_line": output_line, "display_output": raw_output}
                except Exception:
                    pass

        status = match.group(1).lower() if match else "unknown"
        return {"status": status, "output_line": output_line, "display_output": raw_output}

    except Exception as exc:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        msg = str(exc)
        kind = "ssh timeout" if "timed out" in msg.lower() or "timeout" in msg.lower() else f"ssh error: {msg[:60]}"
        return {"status": kind, "output_line": msg, "display_output": msg}


def _check_license(ip, username, password):
    result = _get_license_status(ip, username, password)
    status = result.get("status", "unknown")
    return {
        "status": status,
        "output_line": result.get("output_line") or "",
        "display_output": result.get("display_output") or "",
        "ok": status in PASSING_STATUSES,
    }


# ---------------------------------------------------------------------------
# Default-route check (inventory `routecheck`)
#
# Reads `get router info routing-table all` and requires a 0.0.0.0/0 nexthop on
# every interface an inventory entry lists. Both static routes and the defaults
# a DHCP lease installs count: on z1_external the port1-port5 defaults come from
# DHCP (ad 5, priority 10) and port10's is a static route (distance 5,
# priority 1). Cost is therefore compared against a reference interface's
# default, and a priority difference is reported rather than failed unless the
# inventory asks for it — FortiOS fixes the priority of a DHCP-installed route.
# ---------------------------------------------------------------------------

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
#                   [5/0] via 192.168.101.1, port1, [10/0]
_ECMP_RE = re.compile(
    r"^\[(?P<dist>\d+)/(?P<metric>\d+)\]\s+"
    r"(?:via\s+(?P<gw>\d+\.\d+\.\d+\.\d+),\s+)?"
    r"(?:is directly connected,\s+)?"
    r"(?P<iface>[A-Za-z0-9_.-]+)"
    r"(?:,\s+\[(?P<prio>\d+)/(?P<weight>\d+)\])?"
    r"\s*$"
)


def parse_routing_table(text):
    """Nexthops of every prefix in the output, ECMP members included."""
    routes = []
    current_dst = None
    current_code = ""
    current_selected = False

    def build(match):
        return {
            "code": current_code,
            "dst": current_dst,
            "distance": int(match.group("dist")) if match.group("dist") else None,
            "gateway": match.group("gw"),
            "iface": match.group("iface"),
            "priority": int(match.group("prio")) if match.group("prio") else None,
            "selected": current_selected,
        }

    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        match = _ROUTE_RE.match(line)
        if match:
            current_dst = match.group("dst")
            current_code = match.group("code")
            current_selected = "*" in current_code
            routes.append(build(match))
            continue
        # A gateway-less "directly connected" line under a prefix is the
        # recursive resolution of a nexthop, not another route to that prefix.
        ecmp = _ECMP_RE.match(line)
        if ecmp and current_dst and ecmp.group("gw"):
            routes.append(build(ecmp))
    return routes


def _connect_shell(host, username, password):
    """Interactive shell on a FortiGate, accepting a first-boot blank password
    and the forced password change that follows it."""
    def login(passwd, ssh_timeout=10):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=username, password=passwd,
                    timeout=ssh_timeout, banner_timeout=15, auth_timeout=20,
                    look_for_keys=False, allow_agent=False)
        return ssh

    try:
        ssh = login(password)
    except Exception as first_err:
        try:
            ssh = login("", ssh_timeout=8)
            sh = ssh.invoke_shell(timeout=5)
            time.sleep(0.5)
            out = sh.recv(5000).decode(errors="ignore")
            if "change your password" in out.lower():
                sh.send(password + "\n")
                time.sleep(0.5)
                sh.send(password + "\n")
                time.sleep(1)
            ssh.close()
            ssh = login(password)
        except Exception:
            raise first_err

    shell = ssh.invoke_shell()
    time.sleep(1)
    try:
        while shell.recv_ready():
            shell.recv(8192)
            time.sleep(0.1)
    except Exception:
        pass
    return ssh, shell


def _clean_routing_output(output):
    """Drop the --More-- pagination markers and their CR padding, so the text
    parses and reads cleanly. FortiOS sends them as '--More-- \\r<spaces>\\r' in
    the middle of a line, which would otherwise swallow the following route."""
    lines = []
    for raw in output.replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip().lower().startswith("--more--"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _read_default_routes(host, username, password):
    """(routes, raw output, error) for the target's full routing table."""
    ssh = None
    try:
        ssh, shell = _connect_shell(host, username, password)
        shell.send("get router info routing-table all\n")
        time.sleep(1)
        output = _clean_routing_output(_read_shell_until_prompt(shell, timeout=30))
        ssh.close()
        if re.search(r"(invalid input|command fail|unknown action|parse error)", output, re.IGNORECASE):
            return [], output, "routing table command rejected"
        routes = parse_routing_table(output)
        if not routes:
            return [], output, "no routes parsed from output"
        return routes, output, None
    except Exception as exc:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        msg = str(exc)
        kind = "ssh timeout" if "timeout" in msg.lower() else f"ssh error: {msg[:60]}"
        return [], msg, kind


def _check_default_routes(ip, username, password, interfaces,
                          reference_interface=None, require_priority_match=False):
    """One row per device: every listed interface must carry a default route
    whose distance matches the reference interface's default."""
    result = {
        "status": "unknown",
        "ok": False,
        "checks": [],
        "missing": [],
        "reference_interface": reference_interface,
        "output_line": "",
        "display_output": "",
    }

    routes, output, error = _read_default_routes(ip, username, password)
    result["display_output"] = output
    if error:
        result["status"] = error
        result["output_line"] = error
        return result

    defaults = [route for route in routes if route["dst"] == DEFAULT_DST and route["gateway"]]

    # Reference: the named interface, else port10 when it is in the list, else
    # the first listed interface that has a default route of its own.
    candidates = [reference_interface] if reference_interface else []
    candidates.append("port10")
    candidates.extend(interfaces)
    reference = None
    for candidate in candidates:
        for route in defaults:
            if route["iface"] == candidate:
                reference = route
                break
        if reference:
            result["reference_interface"] = candidate
            break

    result["reference"] = (
        {"interface": reference["iface"], "gateway": reference["gateway"],
         "distance": reference["distance"], "priority": reference["priority"]}
        if reference else None
    )

    problems = []
    checks = []
    for interface in interfaces:
        found = next((route for route in defaults if route["iface"] == interface), None)
        check = {
            "interface": interface,
            "ok": False,
            "gateway": None,
            "distance": None,
            "priority": None,
            "note": "",
        }
        if found is None:
            check["note"] = f"no {DEFAULT_DST} route"
            problems.append(f"{interface}: no default route")
        else:
            check.update({
                "gateway": found["gateway"],
                "distance": found["distance"],
                "priority": found["priority"],
            })
            note = f"via {found['gateway']}, distance {found['distance']}, priority {found['priority']}"
            if reference is None:
                check["note"] = note
                check["ok"] = True
            elif found["distance"] != reference["distance"]:
                check["note"] = f"{note} (expected distance {reference['distance']} from {reference['iface']})"
                problems.append(
                    f"{interface}: distance {found['distance']}, expected {reference['distance']}"
                )
            elif require_priority_match and found["priority"] != reference["priority"]:
                check["note"] = f"{note} (expected priority {reference['priority']} from {reference['iface']})"
                problems.append(
                    f"{interface}: priority {found['priority']}, expected {reference['priority']}"
                )
            else:
                if found["priority"] != reference["priority"]:
                    note += f" (priority differs from {reference['iface']})"
                check["note"] = note
                check["ok"] = True
        checks.append(check)

    result["checks"] = checks
    result["missing"] = [check["interface"] for check in checks if not check["ok"]]
    result["ok"] = not problems
    result["status"] = "valid" if result["ok"] else "missing default"
    if problems:
        result["output_line"] = "; ".join(problems)
    elif reference is None:
        result["output_line"] = (
            f"{len(interfaces)} default route{'s' if len(interfaces) != 1 else ''} present, "
            f"no reference interface for cost comparison"
        )
    else:
        result["output_line"] = (
            f"{len(interfaces)}/{len(interfaces)} defaults present, "
            f"distance {reference['distance']} as {reference['iface']}"
        )
    return result


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------

def _refresh(inventory=None, run_id=None):
    _set_state(True, 0, "starting", "Starting refresh", run_id=run_id)
    # Optional explicit inventory is used for this run only (workshop_check
    # --inventory). UI power/reinstall always fall back to local inventory.yaml.
    if inventory is None:
        inventory = load_inventory()
    power_results = []
    license_results = []
    route_results = []
    failed_count = 0

    powercheck = inventory.get("powercheck", {}) or {}
    licensecheck = inventory.get("licensecheck", {}) or {}
    routecheck = inventory.get("routecheck", {}) or {}
    total = max(1, len(powercheck) + len(licensecheck) + len(routecheck))
    step = 0

    try:
        # Inside the try: a Fabric API failure must surface as a job error, not as
        # a device map of {} that marks every power row "not found".
        device_map = get_runtime_device_map()

        # ---- Power checks ----
        for name, config in powercheck.items():
            expected = str(config.get("expected_state", "")).lower().strip()
            device_id = device_map.get(name)
            row = {
                "name": name,
                "expected_state": expected,
                "status": "unknown",
                "device_id": device_id,
                "ok": False,
                "action": None,
                "action_label": None,
            }

            if not device_id:
                row["status"] = "not found"
                failed_count += 1
            else:
                try:
                    resp = api_get(f"/api/v1/runtime/vm/{device_id}/status")
                    row["status"] = resp.get("object", {}).get("status", "unknown")
                    row["ok"] = row["status"].lower().strip() == expected
                    if not row["ok"]:
                        failed_count += 1
                except Exception as exc:
                    row["status"] = f"error: {str(exc)[:80]}"
                    failed_count += 1

            row["action"] = "power-off" if row["status"].lower() == "running" else "power-on"
            row["action_label"] = "Power Off" if row["action"] == "power-off" else "Power On"

            power_results.append(row)
            step += 1
            _set_state(True, int(step / total * 100), "power", f"Power: {name}", run_id=run_id)
            _save_partial({"last_run": None, "failed_count": failed_count,
                           "power_results": power_results, "license_results": license_results,
                           "route_results": route_results},
                          run_id=run_id)

        # ---- License checks ----
        username = inventory.get("fgt_user")
        password = inventory.get("fgt_password")

        for name, config in licensecheck.items():
            ip = config.get("ip") if isinstance(config, dict) else None
            row = {
                "name": name,
                "ip": ip,
                "status": "unknown",
                "output_line": None,
                "display_output": None,
                "device_id": device_map.get(name),
                "ok": False,
            }

            if not ip:
                row["status"] = "no ip configured"
                failed_count += 1
            elif not username or not password:
                row["status"] = "no credentials"
                failed_count += 1
            else:
                checked = _check_license(ip, username, password)
                row.update(checked)
                if not row["ok"]:
                    failed_count += 1

            license_results.append(row)
            step += 1
            _set_state(True, int(step / total * 100), "license", f"License: {name}", run_id=run_id)
            _save_partial({"last_run": None, "failed_count": failed_count,
                           "power_results": power_results, "license_results": license_results,
                           "route_results": route_results},
                          run_id=run_id)

        # ---- Default-route checks ----
        for name, config in routecheck.items():
            config = config if isinstance(config, dict) else {}
            ip = config.get("ip")
            interfaces = [str(iface) for iface in (config.get("default_routes") or [])]
            row = {
                "name": name,
                "ip": ip,
                "expected": interfaces,
                "status": "unknown",
                "output_line": None,
                "display_output": None,
                "checks": [],
                "missing": [],
                "reference_interface": config.get("reference_interface"),
                "device_id": device_map.get(name),
                "ok": False,
            }

            if not ip:
                row["status"] = "no ip configured"
            elif not interfaces:
                row["status"] = "no interfaces configured"
            elif not username or not password:
                row["status"] = "no credentials"
            else:
                checked = _check_default_routes(
                    ip, username, password, interfaces,
                    reference_interface=config.get("reference_interface"),
                    require_priority_match=bool(config.get("require_priority_match", False)),
                )
                row.update(checked)
            if not row["ok"]:
                failed_count += 1

            route_results.append(row)
            step += 1
            _set_state(True, int(step / total * 100), "routes", f"Default routes: {name}", run_id=run_id)
            _save_partial({"last_run": None, "failed_count": failed_count,
                           "power_results": power_results, "license_results": license_results,
                           "route_results": route_results},
                          run_id=run_id)

        result = {
            "last_run": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "failed_count": failed_count,
            "power_results": power_results,
            "license_results": license_results,
            "route_results": route_results,
        }
        _save_cache(result, run_id=run_id)
        _clear_partial(run_id=run_id)
        _set_state(False, 100, "complete", "Refresh complete", run_id=run_id)
        return result

    except Exception as exc:
        with job_lock:
            current_progress = job_state["progress"]
        _set_state(False, current_progress, "error", f"Refresh failed: {exc}", str(exc), run_id=run_id)
        raise
    finally:
        # Never leave the job wedged in "running": whatever ends this worker
        # (exception, killed thread, abandoned run) must release the flag so the
        # next Recalculate can start. Without this a single stalled call made
        # every later Recalculate a no-op until the service was restarted.
        with job_lock:
            if job_state["running"] and (run_id is None or job_state.get("run_id") == run_id):
                job_state.update({
                    "running": False,
                    "phase": "error",
                    "error": job_state.get("error") or "aborted",
                    "message": "Refresh aborted before completing",
                    "updated_at": time.time(),
                })


def _start_refresh(inventory=None):
    with job_lock:
        if job_state["running"]:
            updated = job_state.get("updated_at") or 0
            if time.time() - updated < JOB_STALE_SECONDS:
                return False
            print(f"[labstatus] taking over stalled run "
                  f"(phase={job_state.get('phase')!r}, message={job_state.get('message')!r}, "
                  f"idle={int(time.time() - updated)}s)")
        run_id = (job_state.get("run_id") or 0) + 1
        job_state.update({"running": True, "progress": 0, "phase": "queued",
                          "message": "Queued", "error": None,
                          "run_id": run_id, "updated_at": time.time()})
    _save_partial({"last_run": None, "failed_count": 0,
                   "power_results": [], "license_results": [],
                   "route_results": []}, run_id=run_id)
    threading.Thread(target=_refresh, args=(inventory, run_id), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def _render_page(data):
    failed_count = data.get("failed_count", 0)
    last_run = html.escape(data.get("last_run") or "Never")
    cache_bool = "true" if data.get("last_run") else "false"

    power_results = data.get("power_results", [])
    license_results = data.get("license_results", [])
    route_results = data.get("route_results", [])
    power_total = len(power_results)
    power_failed = sum(1 for r in power_results if not r.get("ok", False))
    license_total = len(license_results)
    license_failed = sum(1 for r in license_results if not r.get("ok", False))
    route_total = len(route_results)
    route_failed = sum(1 for r in route_results if not r.get("ok", False))

    def _count_badge(failed, total, eid=""):
        if not total:
            return ""
        color = "#c62828" if failed else "#2e7d32"
        id_attr = f' id="{eid}"' if eid else ""
        return (f' <span{id_attr} style="font-size:13px;font-weight:400;color:{color};">'
                f'{failed}/{total} failed</span>')

    power_heading = f"Power Status{_count_badge(power_failed, power_total, 'power-badge')}"
    license_heading = f"License Status{_count_badge(license_failed, license_total, 'license-badge')}"
    route_heading = f"Default Routes{_count_badge(route_failed, route_total, 'route-badge')}"

    # ---- Power table rows ----
    power_rows = []
    for row in power_results:
        ok = row.get("ok", False)
        bg = "#e8f5e9" if ok else "#fdecea"
        result_badge = (
            '<span style="color:#2e7d32;font-weight:600;">Passed</span>' if ok
            else '<span style="color:#c62828;font-weight:600;">Failed</span>'
        )
        power_btn = ""
        if row.get("device_id") and row.get("action"):
            btn_cls = "btn-danger" if row["action"] == "power-off" else "btn-primary"
            power_btn = (
                f'<form action="/labstatus/{html.escape(str(row["device_id"]))}'
                f'/power/{html.escape(row["action"])}" method="post" style="display:inline;margin:0;">'
                f'<button class="btn {btn_cls}" type="submit">{html.escape(row["action_label"])}</button>'
                f'</form>'
            )
        reinstall_btn = ""
        if row.get("device_id"):
            reinstall_btn = (
                f'<form action="/labstatus/{html.escape(str(row["device_id"]))}/reinstall"'
                f' method="post" style="display:inline;margin:0;">'
                f'<button class="btn btn-secondary" type="submit">Reinstall</button>'
                f'</form>'
            )
        power_rows.append(f"""
            <tr style="background:{bg};">
                <td>{html.escape(row.get("name",""))}</td>
                <td><code>{html.escape(row.get("status","unknown"))}</code></td>
                <td>{result_badge}</td>
                <td class="actions">{power_btn} {reinstall_btn}</td>
            </tr>""")

    # ---- License table rows ----
    license_rows = []
    for row in license_results:
        ok = row.get("ok", False)
        status = row.get("status", "unknown")
        output_line = row.get("output_line") or ""
        display_output = row.get("display_output") or ""

        if status == "valid":
            bg = "#e8f5e9"
        elif status in ("warning", "duplicated"):
            bg = "#fff8e1"
        else:
            bg = "#fdecea"

        full_output_html = ""
        if display_output:
            escaped_output = html.escape(display_output)
            full_output_html = (
                f'<details style="margin-top:4px;">'
                f'<summary style="cursor:pointer;font-size:12px;color:#555;">Full SSH output</summary>'
                f'<pre style="font-size:11px;white-space:pre-wrap;word-break:break-all;'
                f'background:#f5f5f5;padding:6px;border-radius:4px;margin-top:4px;">'
                f'{escaped_output}</pre></details>'
            )
        status_cell = f'<code>{html.escape(status)}</code>{full_output_html}'

        if ok:
            result_cell = '<span style="color:#2e7d32;font-weight:600;">Passed</span>'
        else:
            raw_text = html.escape(output_line or status)
            result_cell = (
                f'<span style="color:#c62828;font-weight:600;">Failed</span>'
                f'<br><small style="color:#555;">{raw_text}</small>'
            )

        reinstall_btn = ""
        if row.get("device_id"):
            reinstall_btn = (
                f'<form action="/labstatus/{html.escape(str(row["device_id"]))}/reinstall"'
                f' method="post" style="display:inline;margin:0;">'
                f'<button class="btn btn-secondary" type="submit">Reinstall</button>'
                f'</form>'
            )
        else:
            reinstall_btn = '<span class="muted">No runtime ID</span>'

        license_rows.append(f"""
            <tr style="background:{bg};">
                <td>{html.escape(row.get("name",""))}</td>
                <td>{status_cell}</td>
                <td>{result_cell}</td>
                <td>{reinstall_btn}</td>
            </tr>""")

    # ---- Default route table rows ----
    route_rows = []
    for row in route_results:
        ok = row.get("ok", False)
        bg = "#e8f5e9" if ok else "#fdecea"

        checks_html = []
        for check in row.get("checks") or []:
            good = check.get("ok")
            color = "#2e7d32" if good else "#c62828"
            mark = "&#10003;" if good else "&#10007;"
            note = check.get("note") or ""
            checks_html.append(
                f'<div style="margin:2px 0;font-size:12px;color:{color};">'
                f'<strong>{mark} {html.escape(str(check.get("interface", "")))}</strong>'
                f'<span style="color:#555;"> &mdash; {html.escape(note)}</span></div>'
            )
        if row.get("status") in ("no ip configured", "no interfaces configured", "no credentials"):
            checks_html.append(f'<div class="muted">{html.escape(row["status"])}</div>')
        routes_cell = "".join(checks_html) or '<span class="muted">No results</span>'

        reference_html = ""
        reference = row.get("reference") or {}
        if reference:
            reference_html = (
                f'<div class="muted" style="margin-top:4px;">reference: '
                f'{html.escape(str(reference.get("interface", "")))} default '
                f'distance {html.escape(str(reference.get("distance", "?")))}, '
                f'priority {html.escape(str(reference.get("priority", "?")))}</div>'
            )

        output_line = row.get("output_line") or ""
        if ok:
            result_cell = '<span style="color:#2e7d32;font-weight:600;">Passed</span>'
            if output_line:
                result_cell += f'<br><small style="color:#555;">{html.escape(output_line)}</small>'
        else:
            result_cell = (
                f'<span style="color:#c62828;font-weight:600;">Failed</span>'
                f'<br><small style="color:#555;">{html.escape(output_line or row.get("status", "unknown"))}</small>'
            )

        display_output = row.get("display_output") or ""
        if display_output:
            result_cell += (
                f'<details style="margin-top:4px;">'
                f'<summary style="cursor:pointer;font-size:12px;color:#555;">Routing table output</summary>'
                f'<pre style="font-size:11px;white-space:pre-wrap;word-break:break-all;'
                f'background:#f5f5f5;padding:6px;border-radius:4px;margin-top:4px;">'
                f'{html.escape(display_output)}</pre></details>'
            )

        route_reinstall_btn = ""
        if row.get("device_id"):
            route_reinstall_btn = (
                f'<form action="/labstatus/{html.escape(str(row["device_id"]))}/reinstall"'
                f' method="post" style="display:inline;margin:0;">'
                f'<button class="btn btn-secondary" type="submit">Reinstall</button>'
                f'</form>'
            )
        # Public IP Fix installs the port1-port5 defaults this row validates, so
        # it sits next to Reinstall; the page holds its own Run button and
        # streams the script output, so the row only links to it.
        route_actions = (
            '<a class="btn btn-primary" href="/publicipfix" '
            'style="display:inline-block;text-decoration:none;">Public IP Fix</a> '
            f'{route_reinstall_btn}'
        )

        row_ip = row.get("ip")
        name_cell = html.escape(row.get("name", ""))
        if row_ip:
            name_cell += f'<br><small class="muted">{html.escape(str(row_ip))}</small>'

        route_rows.append(f"""
            <tr style="background:{bg};">
                <td>{name_cell}</td>
                <td>{routes_cell}{reference_html}</td>
                <td>{result_cell}</td>
                <td class="actions">{route_actions}</td>
            </tr>""")

    power_html = "".join(power_rows) or "<tr><td colspan='4' class='muted'>No results yet</td></tr>"
    license_html = "".join(license_rows) or "<tr><td colspan='4' class='muted'>No results yet</td></tr>"
    route_html = "".join(route_rows) or "<tr><td colspan='4' class='muted'>No results yet</td></tr>"

    failed_color = "#c62828" if failed_count > 0 else "#2e7d32"
    failed_label = f"{failed_count} test{'s' if failed_count != 1 else ''} failed" if failed_count > 0 else "All tests passed"

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Lab Validation v2</title>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; background:#f4f6f9; padding:30px; margin:0; }}
    h1 {{ margin:0 0 20px; }}
    h2 {{ margin:0 0 12px; font-size:16px; color:#333; }}
    .card {{ background:white; border-radius:12px; box-shadow:0 3px 8px rgba(0,0,0,.08);
             padding:20px; margin-bottom:24px; }}
    .failed-counter {{ font-size:28px; font-weight:700; color:{failed_color}; margin-bottom:4px; }}
    .failed-label {{ font-size:14px; color:#666; }}
    .btn {{ padding:8px 14px; border-radius:6px; border:none; cursor:pointer;
            color:white; font-size:13px; font-weight:600; }}
    .btn-primary {{ background:#1677ff; }}
    .btn-secondary {{ background:#6c757d; }}
    .btn-danger {{ background:#dc3545; }}
    .btn:disabled {{ opacity:.6; cursor:not-allowed; }}
    .muted {{ color:#6c757d; font-size:13px; }}
    .table-wrap {{ overflow-x:auto; }}
    table {{ border-collapse:collapse; width:100%; min-width:600px; }}
    th, td {{ padding:12px 14px; text-align:left; border-bottom:1px solid #eee; vertical-align:middle; }}
    th {{ background:#f8f9fa; color:#333; font-size:13px; }}
    .col-filter {{ display:block; margin-top:5px; width:100%; box-sizing:border-box;
                   padding:4px 6px; font-size:12px; border:1px solid #ccc;
                   border-radius:4px; font-weight:normal; color:#333; }}
    .col-filter:focus {{ outline:none; border-color:#1677ff; }}
    .filter-wrap {{ display:flex; align-items:center; margin-top:5px; gap:2px; }}
    .filter-wrap .col-filter {{ margin-top:0; flex:1; }}
    .filter-clear {{ background:none; border:none; cursor:pointer; color:#aaa;
                     font-size:16px; padding:2px 4px; line-height:1; border-radius:3px; font-weight:normal; }}
    .filter-clear:hover {{ color:#333; background:#f0f0f0; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    code {{ background:#f0f0f0; padding:2px 5px; border-radius:3px; font-size:12px; }}
    .progress-bar {{ background:#e9ecef; border-radius:999px; overflow:hidden; height:14px; margin-top:8px; }}
    .progress-fill {{ background:#1677ff; height:100%; width:0%; transition:width .3s; }}
    .card-header {{ display:flex; justify-content:space-between; align-items:center; cursor:pointer;
                    user-select:none; margin-bottom:0; }}
    .card-header h2 {{ margin:0; }}
    .collapse-toggle {{ background:none; border:none; cursor:pointer; font-size:18px; color:#888;
                        padding:0 4px; line-height:1; }}
    .card-body {{ margin-top:12px; }}
    .card-body.collapsed {{ display:none; }}
  </style>
</head>
<body>
  <h1>Lab Validation v2</h1>
  <div style="margin:12px 0 20px;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>

  <!-- Summary + Controls row -->
  <div class="card" style="padding:0;">
    <div style="display:grid;grid-template-columns:1fr 1fr;min-height:120px;">
      <!-- Left: controls -->
      <div style="padding:20px;border-right:1px solid #eee;">
        <h2 style="margin:0 0 12px;">Controls</h2>
        <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
          <button id="recalc-btn" class="btn btn-primary" type="button">Recalculate Status</button>
        </div>
        <div class="progress-bar" style="margin-top:12px;">
          <div id="progress-fill" class="progress-fill"></div>
        </div>
        <div style="margin-top:6px;font-size:13px;display:flex;justify-content:space-between;">
          <span id="status-msg">Idle</span>
          <span id="progress-pct">0%</span>
        </div>
      </div>
      <!-- Right: status summary -->
      <div style="padding:20px;">
        <h2 style="margin:0 0 12px;">Status</h2>
        <div class="failed-counter" id="failed-counter">{failed_count}</div>
        <div class="failed-label" id="failed-label">{failed_label}</div>
        <div style="margin-top:8px;font-size:13px;color:#666;">Last run: <strong id="last-run">{last_run}</strong></div>
      </div>
    </div>
  </div>

  <!-- Power table -->
  <div class="card">
    <div class="card-header" onclick="toggleCard('power-body', 'power-toggle')">
      <h2>{power_heading}</h2>
      <button class="collapse-toggle" id="power-toggle" tabindex="-1">&#9650;</button>
    </div>
    <div class="card-body" id="power-body">
      <div class="table-wrap">
        <table id="power-table">
          <thead>
            <tr>
              <th>Device<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>Current State<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>Result<select class="col-filter"><option value="">All</option><option value="passed">Passed</option><option value="failed">Failed</option></select></th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {power_html}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- License table -->
  <div class="card">
    <div class="card-header" onclick="toggleCard('license-body', 'license-toggle')">
      <h2>{license_heading}</h2>
      <button class="collapse-toggle" id="license-toggle" tabindex="-1">&#9650;</button>
    </div>
    <div class="card-body" id="license-body">
      <div class="table-wrap">
        <table id="license-table">
          <thead>
            <tr>
              <th>Device<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>License Status<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>Result<select class="col-filter"><option value="">All</option><option value="passed">Passed</option><option value="failed">Failed</option></select></th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {license_html}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Default routes table -->
  <div class="card">
    <div class="card-header" onclick="toggleCard('route-body', 'route-toggle')">
      <h2>{route_heading}</h2>
      <button class="collapse-toggle" id="route-toggle" tabindex="-1">&#9650;</button>
    </div>
    <div class="card-body" id="route-body">
      <div class="table-wrap">
        <table id="route-table">
          <thead>
            <tr>
              <th>Device<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>Default Routes<div class="filter-wrap"><input class="col-filter" type="text" placeholder="Filter..."><button class="filter-clear" tabindex="-1">&times;</button></div></th>
              <th>Result<select class="col-filter"><option value="">All</option><option value="passed">Passed</option><option value="failed">Failed</option></select></th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {route_html}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const cacheExists = {cache_bool};
    const fill = document.getElementById('progress-fill');
    const pct = document.getElementById('progress-pct');
    const msg = document.getElementById('status-msg');
    const recalcBtn = document.getElementById('recalc-btn');
    let refreshStarted = false;

    function escHtml(s) {{
      return String(s == null ? '' : s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    function renderPowerRow(r) {{
      var ok = !!r.ok;
      var bg = ok ? '#e8f5e9' : '#fdecea';
      var badge = ok
        ? '<span style="color:#2e7d32;font-weight:600;">Passed</span>'
        : '<span style="color:#c62828;font-weight:600;">Failed</span>';
      var powerBtn = '';
      if (r.device_id && r.action) {{
        var cls = r.action === 'power-off' ? 'btn-danger' : 'btn-primary';
        powerBtn = '<form action="/labstatus/' + escHtml(r.device_id) + '/power/' + escHtml(r.action) +
          '" method="post" style="display:inline;margin:0;"><button class="btn ' + cls + '" type="submit">' +
          escHtml(r.action_label) + '</button></form>';
      }}
      var reinstallBtn = r.device_id
        ? '<form action="/labstatus/' + escHtml(r.device_id) + '/reinstall" method="post" style="display:inline;margin:0;">' +
          '<button class="btn btn-secondary" type="submit">Reinstall</button></form>'
        : '';
      return '<tr style="background:' + bg + ';">' +
        '<td>' + escHtml(r.name) + '</td>' +
        '<td><code>' + escHtml(r.status || 'unknown') + '</code></td>' +
        '<td>' + badge + '</td>' +
        '<td class="actions">' + powerBtn + ' ' + reinstallBtn + '</td></tr>';
    }}

    function renderLicenseRow(r) {{
      var ok = !!r.ok;
      var status = r.status || 'unknown';
      var bg = '#fdecea';
      if (status === 'valid') bg = '#e8f5e9';
      else if (status === 'warning' || status === 'duplicated') bg = '#fff8e1';
      var detailsHtml = '';
      if (r.display_output) {{
        detailsHtml = '<details style="margin-top:4px;"><summary style="cursor:pointer;font-size:12px;color:#555;">Full SSH output</summary>' +
          '<pre style="font-size:11px;white-space:pre-wrap;word-break:break-all;background:#f5f5f5;padding:6px;border-radius:4px;margin-top:4px;">' +
          escHtml(r.display_output) + '</pre></details>';
      }}
      var resultCell = ok
        ? '<span style="color:#2e7d32;font-weight:600;">Passed</span>'
        : '<span style="color:#c62828;font-weight:600;">Failed</span><br><small style="color:#555;">' + escHtml(r.output_line || status) + '</small>';
      var reinstallBtn = r.device_id
        ? '<form action="/labstatus/' + escHtml(r.device_id) + '/reinstall" method="post" style="display:inline;margin:0;">' +
          '<button class="btn btn-secondary" type="submit">Reinstall</button></form>'
        : '<span class="muted">No runtime ID</span>';
      return '<tr style="background:' + bg + ';">' +
        '<td>' + escHtml(r.name) + '</td>' +
        '<td><code>' + escHtml(status) + '</code>' + detailsHtml + '</td>' +
        '<td>' + resultCell + '</td>' +
        '<td>' + reinstallBtn + '</td></tr>';
    }}

    function renderRouteRow(r) {{
      var ok = !!r.ok;
      var bg = ok ? '#e8f5e9' : '#fdecea';
      var checks = (r.checks || []).map(function(c) {{
        var good = !!c.ok;
        return '<div style="margin:2px 0;font-size:12px;color:' + (good ? '#2e7d32' : '#c62828') + ';">' +
          '<strong>' + (good ? '&#10003;' : '&#10007;') + ' ' + escHtml(c.interface) + '</strong>' +
          '<span style="color:#555;"> &mdash; ' + escHtml(c.note || '') + '</span></div>';
      }}).join('');
      if (r.status === 'no ip configured' || r.status === 'no interfaces configured' || r.status === 'no credentials') {{
        checks += '<div class="muted">' + escHtml(r.status) + '</div>';
      }}
      if (!checks) {{
        checks = '<span class="muted">No results</span>';
      }}
      var ref = r.reference || null;
      var refHtml = ref
        ? '<div class="muted" style="margin-top:4px;">reference: ' + escHtml(ref.interface) +
          ' default distance ' + escHtml(ref.distance) + ', priority ' + escHtml(ref.priority) + '</div>'
        : '';
      var out = r.output_line || r.status || 'unknown';
      var resultCell = ok
        ? '<span style="color:#2e7d32;font-weight:600;">Passed</span>' +
          (r.output_line ? '<br><small style="color:#555;">' + escHtml(r.output_line) + '</small>' : '')
        : '<span style="color:#c62828;font-weight:600;">Failed</span>' +
          '<br><small style="color:#555;">' + escHtml(out) + '</small>';
      if (r.display_output) {{
        resultCell += '<details style="margin-top:4px;">' +
          '<summary style="cursor:pointer;font-size:12px;color:#555;">Routing table output</summary>' +
          '<pre style="font-size:11px;white-space:pre-wrap;word-break:break-all;background:#f5f5f5;padding:6px;border-radius:4px;margin-top:4px;">' +
          escHtml(r.display_output) + '</pre></details>';
      }}
      var reinstallBtn = r.device_id
        ? '<form action="/labstatus/' + escHtml(r.device_id) + '/reinstall" method="post" style="display:inline;margin:0;">' +
          '<button class="btn btn-secondary" type="submit">Reinstall</button></form>'
        : '';
      var publicIpBtn = '<a class="btn btn-primary" href="/publicipfix" ' +
        'style="display:inline-block;text-decoration:none;">Public IP Fix</a> ';
      var nameCell = escHtml(r.name) + (r.ip ? '<br><small class="muted">' + escHtml(r.ip) + '</small>' : '');
      return '<tr style="background:' + bg + ';">' +
        '<td>' + nameCell + '</td>' +
        '<td>' + checks + refHtml + '</td>' +
        '<td>' + resultCell + '</td>' +
        '<td class="actions">' + publicIpBtn + reinstallBtn + '</td></tr>';
    }}

    function updateBadge(id, failed, total) {{
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = total ? (failed + '/' + total + ' failed') : '';
      el.style.color = failed > 0 ? '#c62828' : '#2e7d32';
    }}

    function updateTables(data) {{
      var pr = data.power_results || [];
      var lr = data.license_results || [];
      var rr = data.route_results || [];
      var powerTbody = document.querySelector('#power-table tbody');
      var licenseTbody = document.querySelector('#license-table tbody');
      var routeTbody = document.querySelector('#route-table tbody');
      if (pr.length) {{
        powerTbody.innerHTML = pr.map(renderPowerRow).join('');
      }}
      if (lr.length) {{
        licenseTbody.innerHTML = lr.map(renderLicenseRow).join('');
      }}
      if (rr.length && routeTbody) {{
        routeTbody.innerHTML = rr.map(renderRouteRow).join('');
      }}
      // Update per-table heading badges
      updateBadge('power-badge', pr.filter(function(r) {{ return !r.ok; }}).length, pr.length);
      updateBadge('license-badge', lr.filter(function(r) {{ return !r.ok; }}).length, lr.length);
      updateBadge('route-badge', rr.filter(function(r) {{ return !r.ok; }}).length, rr.length);
      // Update overall failed counter
      if (data.failed_count !== undefined) {{
        var fc = document.getElementById('failed-counter');
        var fl = document.getElementById('failed-label');
        if (fc) {{
          fc.textContent = data.failed_count;
          fc.style.color = data.failed_count > 0 ? '#c62828' : '#2e7d32';
        }}
        if (fl) {{
          fl.textContent = data.failed_count > 0
            ? data.failed_count + ' test' + (data.failed_count !== 1 ? 's' : '') + ' failed'
            : 'All tests passed';
        }}
      }}
      if (data.last_run) {{
        var lrEl = document.getElementById('last-run');
        if (lrEl) lrEl.textContent = data.last_run;
      }}
      // Re-apply whichever column filters are currently active
      document.querySelectorAll('.col-filter').forEach(function(inp) {{
        if (inp.value) applyColFilter(inp);
      }});
    }}

    function updateProgress(state) {{
      const v = state.progress || 0;
      fill.style.width = v + '%';
      pct.textContent = v + '%';
      msg.textContent = state.message || 'Idle';
      recalcBtn.disabled = !!state.running;
      recalcBtn.textContent = state.running ? 'Running...' : 'Recalculate Status';
    }}

    function pollStatus() {{
      fetch('/labstatus/status')
        .then(r => r.json())
        .then(state => {{
          updateProgress(state);
          if (state.running) {{
            fetch('/api/labstatus')
              .then(r => r.json())
              .then(data => updateTables(data))
              .catch(() => {{}});
            setTimeout(pollStatus, 2000);
          }} else if (refreshStarted && state.progress >= 100) {{
            refreshStarted = false;
            fetch('/api/labstatus')
              .then(function(r) {{ return r.json(); }})
              .then(function(data) {{ updateTables(data); }})
              .catch(function() {{}});
          }}
        }})
        .catch(() => {{
          msg.textContent = 'Unable to poll status';
          recalcBtn.disabled = false;
        }});
    }}

    function clearTables() {{
      document.querySelectorAll('table').forEach(table => {{
        var cols = table.querySelector('thead tr').cells.length;
        table.querySelector('tbody').innerHTML =
          '<tr><td colspan="' + cols + '" class="muted" style="text-align:center;padding:20px;">Recalculating...</td></tr>';
      }});
    }}

    function startRefresh() {{
      refreshStarted = true;
      recalcBtn.disabled = true;
      recalcBtn.textContent = 'Starting...';
      clearTables();
      fetch('/labstatus/recalculate', {{ method: 'POST' }})
        .then(r => r.json())
        .then(() => pollStatus())
        .catch(() => {{
          msg.textContent = 'Failed to start refresh';
          recalcBtn.disabled = false;
          recalcBtn.textContent = 'Recalculate Status';
        }});
    }}

    function applyColFilter(input) {{
      const table = input.closest('table');
      const th = input.closest('th');
      const colIdx = Array.from(th.parentElement.children).indexOf(th);
      const val = input.value.toLowerCase();
      table.querySelectorAll('tbody tr').forEach(row => {{
        const cell = row.cells[colIdx];
        const text = cell ? cell.textContent.toLowerCase() : '';
        if (val && !text.includes(val)) {{
          row.dataset['filtered' + colIdx] = '1';
        }} else {{
          delete row.dataset['filtered' + colIdx];
        }}
        const hidden = Object.keys(row.dataset).some(k => k.startsWith('filtered'));
        row.style.display = hidden ? 'none' : '';
      }});
    }}

    document.querySelectorAll('.col-filter').forEach(input => {{
      ['input', 'change'].forEach(evt => input.addEventListener(evt, () => applyColFilter(input)));
      input.addEventListener('click', e => e.stopPropagation());
    }});

    document.querySelectorAll('.filter-clear').forEach(btn => {{
      btn.addEventListener('click', e => {{
        e.stopPropagation();
        const input = btn.previousElementSibling;
        input.value = '';
        applyColFilter(input);
      }});
    }});

    function toggleCard(bodyId, toggleId) {{
      const body = document.getElementById(bodyId);
      const btn = document.getElementById(toggleId);
      const collapsed = body.classList.toggle('collapsed');
      btn.innerHTML = collapsed ? '&#9660;' : '&#9650;';
    }}

    recalcBtn.addEventListener('click', startRefresh);
    pollStatus();
    if (!cacheExists) {{ startRefresh(); }}
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/labstatus", response_class=HTMLResponse)
def labstatus2_page():
    data = _load_data()
    return HTMLResponse(_render_page(data))


@router.get("/labstatus/status")
def labstatus2_status():
    with job_lock:
        return JSONResponse(content=dict(job_state))


@router.post("/labstatus/recalculate")
async def labstatus2_recalculate(request: Request):
    # An optional raw YAML body (pushed by workshop_check.py --inventory) is
    # used for this refresh only. Empty body -> local inventory.yaml.
    inventory = None
    body = await request.body()
    if body:
        try:
            inventory = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            return JSONResponse({"error": f"Invalid inventory YAML: {exc}"}, status_code=400)
        if not isinstance(inventory, dict):
            return JSONResponse({"error": "Inventory must be a YAML mapping (dict)."}, status_code=400)
    started = _start_refresh(inventory)
    if not started:
        return JSONResponse({"started": False, "message": "Refresh already running."}, status_code=202)
    return JSONResponse({"started": True, "message": "Refresh started."}, status_code=202)


@router.post("/labstatus/{device_id}/power/{action}")
def labstatus2_power(device_id: str, action: str):
    if action not in ("power-on", "power-off"):
        return HTMLResponse("Invalid power action", status_code=400)
    payload = {"configuration": True, "license": True, "post_boot": True, "timeout": 0} if action == "power-on" else None
    try:
        api_post(f"/api/v1/runtime/vm/{device_id}:{action}", payload)
    except Exception:
        pass
    _refresh()
    return RedirectResponse(url="/labstatus", status_code=303)


@router.post("/labstatus/{device_id}/reinstall")
def labstatus2_reinstall(device_id: str):
    try:
        api_delete(f"/api/v1/runtime/device/{device_id}")
        time.sleep(30)
        api_post(f"/api/v1/runtime/device/{device_id}", REINSTALL_PAYLOAD)
    except Exception as exc:
        print(f"Reinstall error for {device_id}: {exc}")
    _refresh()
    return RedirectResponse(url="/labstatus", status_code=303)


@router.get("/api/labstatus")
def labstatus_api():
    data = _load_data()
    return JSONResponse(content={**data, "cached": bool(data.get("last_run"))})

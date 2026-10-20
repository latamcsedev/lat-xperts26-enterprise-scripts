import html
import re
import socket
import time

import paramiko
import requests
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from utils import load_inventory, get_serial

router = APIRouter()

REQUEST_TIMEOUT = 20
CONN_STATUS = {0: "unknown", 1: "up", 2: "down"}


def _stamp():
    return time.strftime("%H:%M:%S")


def _tcp_check(host, port, timeout=4):
    started = time.time()
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        elapsed = int((time.time() - started) * 1000)
        return True, f"open ({elapsed} ms)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def _rpc_status(body):
    results = (body or {}).get("result") or []
    if not results:
        return None, "empty result"
    status = results[0].get("status") or {}
    return status.get("code"), status.get("message") or ""


def _fmg_call(fmg_ip, payload, timeout=REQUEST_TIMEOUT):
    # Do not retry a read timeout. A timed-out exec keeps running on
    # FortiManager and can leave the device database locked.
    url = f"https://{fmg_ip}/jsonrpc"
    response = requests.post(url, json=payload, verify=False, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fmg_login(fmg_ip, fmg_user, fmg_password):
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "exec",
            "params": [
                {
                    "data": {"passwd": fmg_password, "user": fmg_user},
                    "url": "/sys/login/user",
                }
            ],
        },
    )
    session = body.get("session")
    code, message = _rpc_status(body)
    if session and code == 0:
        return session, f"login ok ({message})"
    raise RuntimeError(f"FMG login failed: code={code} message={message} body={body}")


def fmg_logout(fmg_ip, session):
    try:
        body = _fmg_call(
            fmg_ip,
            {
                "id": 1,
                "method": "exec",
                "session": session,
                "params": [{"url": "/sys/logout"}],
            },
        )
        code, message = _rpc_status(body)
        if code == 0:
            return True, "logout ok"
        return False, f"logout returned {code} {message}"
    except Exception as exc:
        # FortiManager often closes the connection after logout. The replacement
        # result is already known, so this must not fail the request.
        return False, f"logout connection closed ({type(exc).__name__}: {exc})"


def fmg_list_devices(fmg_ip, session):
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "get",
            "session": session,
            "params": [
                {
                    "url": "/dvmdb/device",
                    "fields": ["name", "sn", "ip", "conn_status", "hostname"],
                    "loadsub": 0,
                }
            ],
        },
    )
    code, message = _rpc_status(body)
    if code != 0:
        raise RuntimeError(f"FMG device list failed: {code} {message}")
    data = (body.get("result") or [{}])[0].get("data") or []
    if isinstance(data, dict):
        data = [data]
    return data


def _conn_label(value):
    if isinstance(value, str):
        return value
    return CONN_STATUS.get(value, str(value))


def fmg_offline_mode(fmg_ip, session):
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "get",
            "session": session,
            "params": [{"url": "/cli/global/system/admin/setting"}],
        },
    )
    code, message = _rpc_status(body)
    if code != 0:
        return None, f"could not read offline mode ({code} {message})"
    data = (body.get("result") or [{}])[0].get("data") or {}
    enabled = data.get("offline_mode")
    if enabled in (1, "enable", "enabled", True):
        return True, "enabled; managed devices stay down until this is turned off"
    return False, "disabled"


def fmg_write_lock(host, user, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=user,
            password=password,
            timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        channel = client.invoke_shell(width=160, height=40)
        time.sleep(0.8)
        if channel.recv_ready():
            channel.recv(65535)
        channel.send("diagnose dvm lockinfo\n")
        time.sleep(2)
        text = ""
        while channel.recv_ready():
            text += channel.recv(65535).decode("utf-8", "replace")
            time.sleep(0.1)
        holder = "unlocked"
        for line in text.splitlines():
            if "write write:" in line:
                holder = " ".join(line.split())
                break
        # "unlocked" contains the substring "locked".
        return ": locked" in holder, holder
    except Exception as exc:
        return None, f"could not read lock ({exc})"
    finally:
        client.close()


def fmg_delete_device(fmg_ip, session, adom, device):
    # adom and device are strings. nonblocking returns a task instead of
    # holding the JSON call until the device database lock clears.
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "exec",
            "session": session,
            "params": [
                {
                    "url": "/dvm/cmd/del/device",
                    "data": {
                        "adom": adom,
                        "device": device,
                        "flags": ["create_task", "nonblocking"],
                    },
                }
            ],
        },
    )
    code, message = _rpc_status(body)
    data = (body.get("result") or [{}])[0].get("data") or {}
    task_id = data.get("taskid") or data.get("task") if isinstance(data, dict) else None
    return code, message, task_id


def _state_name(state):
    names = {1: "running", 4: "done", 5: "error"}
    if state in names:
        return names[state]
    return str(state)


def _task_running(task):
    return not task.get("end_tm")


def _task_summary(task):
    return (
        f"task {task.get('id')} {_state_name(task.get('state'))} "
        f"{task.get('percent')}% {task.get('title') or ''}"
    ).strip()


def fmg_list_tasks(fmg_ip, session):
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "get",
            "session": session,
            "params": [
                {
                    "url": "/task/task",
                    "fields": [
                        "id",
                        "title",
                        "percent",
                        "state",
                        "start_tm",
                        "end_tm",
                        "num_done",
                        "num_err",
                        "num_lines",
                        "user",
                    ],
                    "loadsub": 0,
                    "sortings": [{"id": -1}],
                }
            ],
        },
    )
    code, message = _rpc_status(body)
    if code != 0:
        raise RuntimeError(f"FMG task list failed: {code} {message}")
    data = (body.get("result") or [{}])[0].get("data") or []
    if isinstance(data, dict):
        data = [data]
    return data


def _parse_processes(text):
    processes = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        processes.append((int(parts[0]), parts[3], " ".join(parts[4:])))
    return processes


def _lock_holder(text):
    match = re.search(r"write write: locked by pid (\d+)\(([^)]+)\)", text)
    if not match:
        return None, None
    return int(match.group(1)), match.group(2).strip()


def _pids_to_clear(processes, holder_pid):
    # The lowest /bin/dvmcmd is the device-manager daemon. Extra copies are
    # leftover exec workers and are what stick the database lock.
    dvmcmd = sorted(pid for pid, stat, command in processes if command == "/bin/dvmcmd")
    keep = dvmcmd[0] if dvmcmd else None
    targets = []
    seen = set()
    for pid, stat, command in processes:
        if command == "/bin/dvmcmd" and pid != keep and pid not in seen:
            targets.append((pid, command))
            seen.add(pid)
    if holder_pid and holder_pid != keep and holder_pid not in seen:
        for pid, stat, command in processes:
            if pid == holder_pid and command in ("/bin/dvmcmd", "svc dvmdb reader"):
                targets.append((pid, command))
                break
    return targets


class _FmgCli:
    def __init__(self, host, user, password):
        self.host = host
        self.user = user
        self.password = password
        self.client = None
        self.channel = None

    def __enter__(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host,
            username=self.user,
            password=self.password,
            timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        self.channel = self.client.invoke_shell(width=200, height=80)
        time.sleep(0.6)
        self._drain(0.4)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

    def _drain(self, wait):
        end = time.time() + wait
        text = ""
        while time.time() < end:
            if self.channel.recv_ready():
                text += self.channel.recv(65535).decode("utf-8", "replace")
            else:
                time.sleep(0.1)
        return text

    def run(self, command, wait=2):
        self.channel.send(command + "\n")
        return self._drain(wait)


def _clear_stuck_workers(report, host, user, password):
    killed = []
    with _FmgCli(host, user, password) as cli:
        lock_text = cli.run("diagnose dvm lockinfo", wait=2)
        process_text = cli.run("diagnose sys process list", wait=3)
        holder_pid, holder_cmd = _lock_holder(lock_text)
        targets = _pids_to_clear(_parse_processes(process_text), holder_pid)
        if holder_pid and not targets:
            report.step(
                False,
                f"database lock is held by pid {holder_pid} ({holder_cmd}), "
                "which is not a leftover worker",
            )
        for pid, command in targets:
            result = cli.run(f"diagnose sys process kill -9 {pid}", wait=0.8)
            ok = "Send signal" in result and "can't kill" not in result
            report.step(ok, f"cleared leftover {command} pid {pid}")
            if ok:
                killed.append(pid)
    return killed


def _prepare_tasks(report, fmg_ip, session, fmg_user, fmg_password):
    try:
        tasks = fmg_list_tasks(fmg_ip, session)
    except Exception as exc:
        report.job("task", "Clear stuck tasks", False, f"could not list tasks ({exc})")
        report.step(False, f"could not list FortiManager tasks ({exc})")
        return False

    running = [task for task in tasks if _task_running(task)]
    if running:
        report.step(False, f"FortiManager has {len(running)} running task(s)")
        for task in running:
            report.step(False, _task_summary(task))
    else:
        report.step(True, "FortiManager has no running tasks")

    locked, lock_detail = fmg_write_lock(fmg_ip, fmg_user, fmg_password)
    if locked is None:
        report.conn("FMG device database", False, lock_detail)
        report.job("task", "Clear stuck tasks", False, lock_detail)
        return False
    report.conn("FMG device database", not locked, lock_detail)

    if not running and not locked:
        report.job(
            "task",
            "Clear stuck tasks",
            True,
            "no running tasks, device database unlocked",
        )
        return True

    report.step(True, "Clearing leftover FortiManager task workers before changes")
    try:
        killed = _clear_stuck_workers(report, fmg_ip, fmg_user, fmg_password)
    except Exception as exc:
        report.job("task", "Clear stuck tasks", False, f"clear failed ({exc})")
        report.step(False, f"failed to clear stuck tasks ({exc})")
        return False

    time.sleep(2)
    try:
        tasks = fmg_list_tasks(fmg_ip, session)
        still_running = [task for task in tasks if _task_running(task)]
        reread_ok = True
    except Exception as exc:
        still_running = []
        reread_ok = False
        report.step(False, f"could not re-read tasks ({exc})")
    locked, lock_detail = fmg_write_lock(fmg_ip, fmg_user, fmg_password)
    report.conn("FMG device database after clear", locked is False, lock_detail)

    for task in still_running:
        report.step(False, "still running " + _task_summary(task))
    cleared = reread_ok and not still_running and locked is False
    running_text = (
        f"{len(still_running)} task(s) still running"
        if reread_ok
        else "task list could not be re-read"
    )
    detail = (
        f"stopped {len(killed)} leftover worker(s); "
        f"{running_text}; {lock_detail}"
    )
    report.job("task", "Clear stuck tasks", cleared, detail)
    report.step(cleared, detail)
    return cleared


def fmg_task(fmg_ip, session, task_id):
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "get",
            "session": session,
            "params": [{"url": f"/task/task/{task_id}"}],
        },
    )
    code, message = _rpc_status(body)
    data = (body.get("result") or [{}])[0].get("data") or {}
    if not isinstance(data, dict):
        data = {}
    detail = ""
    lines = data.get("line") or []
    if lines and isinstance(lines, list):
        detail = lines[0].get("detail") or lines[0].get("name") or ""
    return {
        "code": code,
        "message": message,
        "percent": data.get("percent"),
        "state": data.get("state"),
        "end_tm": data.get("end_tm") or 0,
        "num_err": data.get("num_err") or 0,
        "detail": detail,
    }


def fmg_replace_sn(fmg_ip, session, device_name, serial):
    url = f"/dvmdb/device/replace/sn/{device_name}"
    body = _fmg_call(
        fmg_ip,
        {
            "id": 1,
            "method": "exec",
            "session": session,
            "params": [{"url": url, "data": {"sn": serial}}],
        },
        timeout=25,
    )
    code, message = _rpc_status(body)
    return code, message, url


def _device_named(devices, name):
    for device in devices:
        if device.get("name") == name:
            return device
    return None


def _devices_with_sn(devices, serial, skip_name):
    matches = []
    for device in devices:
        if device.get("sn") == serial and device.get("name") != skip_name:
            matches.append(device)
    return matches


class Report:
    def __init__(self):
        self.connectivity = []
        self.steps = []
        self.jobs = []

    def conn(self, target, ok, detail):
        self.connectivity.append((target, ok, detail))
        print(f"[{_stamp()}] connectivity {target}: {detail}", flush=True)

    def step(self, ok, message):
        self.steps.append((_stamp(), ok, message))
        print(f"[{_stamp()}] {message}", flush=True)

    def job(self, kind, name, ok, detail):
        self.jobs.append((kind, name, ok, detail))
        print(f"[{_stamp()}] job {name}: {detail}", flush=True)


def replace_serials_on_fmg():
    report = Report()
    inventory = load_inventory()
    fmg_ip = inventory.get("fmg_ip")
    fmg_user = inventory.get("fmg_user")
    fmg_password = inventory.get("fmg_password")
    fmg_adom = inventory.get("fmg_adom") or "root"
    fgt_user = inventory.get("fgt_user")
    fgt_password = inventory.get("fgt_password")
    sites = inventory.get("sites") or {}

    fmg_open, fmg_detail = _tcp_check(fmg_ip, 443)
    report.conn(f"FMG {fmg_ip}:443", fmg_open, fmg_detail)

    ssh_ok = {}
    for site_name, site_data in sites.items():
        ip = site_data.get("ip")
        ok, detail = _tcp_check(ip, 22)
        ssh_ok[site_name] = ok
        report.conn(f"{site_name} SSH {ip}:22", ok, detail)

    if not fmg_open:
        report.job("job", "Serial replacement", False, "FMG HTTPS is unreachable")
        report.step(False, "FMG HTTPS is unreachable. Serial replacement did not start.")
        return report

    session = None
    try:
        session, login_detail = fmg_login(fmg_ip, fmg_user, fmg_password)
        report.conn(f"FMG API login {fmg_ip}", True, login_detail)
    except Exception as exc:
        report.conn(f"FMG API login {fmg_ip}", False, str(exc))
        report.job("job", "Serial replacement", False, "FMG login failed")
        report.step(False, "FMG login failed. Serial replacement did not start.")
        return report

    try:
        offline, offline_detail = fmg_offline_mode(fmg_ip, session)
        report.conn(
            "FMG offline mode",
            offline is False,
            offline_detail,
        )
    except Exception as exc:
        report.conn("FMG offline mode", False, str(exc))

    ready = _prepare_tasks(report, fmg_ip, session, fmg_user, fmg_password)
    if not ready:
        report.job(
            "job",
            "Serial replacement",
            False,
            "blocked until FortiManager tasks are clear",
        )
        ok, detail = fmg_logout(fmg_ip, session)
        report.step(ok, f"FMG {detail}")
        return report

    try:
        try:
            _replace_sites(
                report,
                sites,
                ssh_ok,
                fmg_ip,
                session,
                fmg_adom,
                fgt_user,
                fgt_password,
            )
        except Exception as exc:
            report.job("job", "Serial replacement", False, f"stopped: {exc}")
            report.step(False, f"Stopped: {exc}")
        else:
            sites_done = [item for item in report.jobs if item[0] == "site"]
            failed = [item for item in sites_done if not item[2]]
            report.job(
                "job",
                "Serial replacement",
                not failed,
                f"{len(sites_done) - len(failed)} of {len(sites_done)} sites ok",
            )
    finally:
        if session:
            ok, detail = fmg_logout(fmg_ip, session)
            report.step(ok, f"FMG {detail}")

    return report


def _wait_task(report, fmg_ip, session, site_name, task_id):
    if not task_id:
        report.step(False, f"{site_name}: delete did not return a task id")
        return False
    deadline = time.time() + 45
    last = None
    snapshot = None
    while time.time() < deadline:
        try:
            snapshot = fmg_task(fmg_ip, session, task_id)
        except Exception as exc:
            report.step(False, f"{site_name}: task {task_id} poll failed ({exc})")
            return False
        summary = (
            f"{site_name}: task {task_id} state {snapshot['state']} "
            f"{snapshot['percent']}% {snapshot['detail']}"
        ).strip()
        if summary != last:
            report.step(True, summary)
            last = summary
        finished = snapshot["percent"] == 100 or (
            snapshot["end_tm"] not in (0, None) and snapshot["percent"] not in (None, 0)
        )
        if finished:
            ok = snapshot["num_err"] == 0 and snapshot["percent"] == 100
            report.step(
                ok,
                f"{site_name}: delete task {task_id} finished "
                f"({snapshot['percent']}%, errors {snapshot['num_err']})",
            )
            return ok
        time.sleep(2)
    detail = snapshot["detail"] if snapshot else "unknown"
    report.step(False, f"{site_name}: delete task {task_id} still {detail} after 45s")
    return False


def _replace_sites(report, sites, ssh_ok, fmg_ip, session, fmg_adom, fgt_user, fgt_password):
    for site_name, site_data in sites.items():
        ip = site_data.get("ip")
        report.step(True, f"{site_name}: starting ({ip})")

        if not ssh_ok.get(site_name):
            report.job("site", site_name, False, "SSH is unreachable")
            report.step(False, f"{site_name}: skipped, SSH is unreachable")
            continue

        real_sn = get_serial(ip, fgt_user, fgt_password)
        if "Error" in real_sn or "Not Found" in real_sn:
            report.job("site", site_name, False, f"failed to read serial ({real_sn})")
            report.step(False, f"{site_name}: failed to read serial ({real_sn})")
            continue
        report.step(True, f"{site_name}: device serial is {real_sn}")

        try:
            devices = fmg_list_devices(fmg_ip, session)
        except Exception as exc:
            report.job("site", site_name, False, f"failed to read FMG devices ({exc})")
            report.step(False, f"{site_name}: failed to read FMG devices ({exc})")
            continue

        current = _device_named(devices, site_name)
        if current is None:
            report.job("site", site_name, False, "no device with this name on FMG")
            report.step(False, f"{site_name}: no device with this name on FMG")
            continue

        fmg_sn = current.get("sn") or ""
        report.step(
            True,
            f"{site_name}: FMG has serial {fmg_sn or 'empty'}, "
            f"manager link {_conn_label(current.get('conn_status'))}",
        )

        if fmg_sn == real_sn:
            report.job("site", site_name, True, f"already correct ({real_sn})")
            report.step(True, f"{site_name}: already correct ({real_sn}), skipped")
            continue

        # The live serial is often already claimed by an unauthorized device.
        # That entry has to be removed. The managed device itself stays.
        duplicates = _devices_with_sn(devices, real_sn, site_name)
        if not duplicates:
            report.step(True, f"{site_name}: no other FMG device holds {real_sn}")
        delete_ok = True
        for duplicate in duplicates:
            dup_name = duplicate.get("name")
            report.step(
                True,
                f"{site_name}: removing duplicate {dup_name} "
                f"(serial {duplicate.get('sn')}, ip {duplicate.get('ip')})",
            )
            if not dup_name:
                delete_ok = False
                report.step(False, f"{site_name}: duplicate has no name")
                continue
            try:
                code, message, task_id = fmg_delete_device(
                    fmg_ip, session, fmg_adom, dup_name
                )
            except Exception as exc:
                delete_ok = False
                report.step(False, f"{site_name}: delete {dup_name} failed ({exc})")
                continue
            if code != 0:
                delete_ok = False
                report.step(
                    False,
                    f"{site_name}: delete {dup_name} returned {code} {message}",
                )
                continue
            report.step(True, f"{site_name}: delete task {task_id} accepted ({message})")
            if not _wait_task(report, fmg_ip, session, site_name, task_id):
                delete_ok = False
        if duplicates and not delete_ok:
            report.job("site", site_name, False, "duplicate serial is still present")
            report.step(
                False,
                f"{site_name}: skipped replace because the duplicate serial is still present",
            )
            continue

        try:
            code, message, url = fmg_replace_sn(fmg_ip, session, site_name, real_sn)
        except Exception as exc:
            report.job("site", site_name, False, f"replace call failed ({exc})")
            report.step(False, f"{site_name}: replace call failed ({exc})")
            continue

        if code not in (0, -20010):
            report.job("site", site_name, False, f"replace failed ({code} {message})")
            report.step(
                False,
                f"{site_name}: replace failed ({code} {message}) via {url}",
            )
            continue

        report.step(code == 0, f"{site_name}: replace returned {code} {message}")

        try:
            devices = fmg_list_devices(fmg_ip, session)
            updated = _device_named(devices, site_name)
        except Exception as exc:
            report.job("site", site_name, False, f"verify failed ({exc})")
            report.step(False, f"{site_name}: replaced, verify failed ({exc})")
            continue

        updated_sn = (updated or {}).get("sn") or ""
        updated_conn = _conn_label((updated or {}).get("conn_status"))
        if updated_sn == real_sn:
            report.job(
                "site",
                site_name,
                True,
                f"{fmg_sn or 'empty'} -> {updated_sn}, manager link {updated_conn}",
            )
            report.step(
                True,
                f"{site_name}: verified {fmg_sn or 'empty'} -> {updated_sn}, "
                f"manager link {updated_conn}",
            )
        else:
            report.job(
                "site",
                site_name,
                False,
                f"FMG still has {updated_sn or 'empty'} (wanted {real_sn})",
            )
            report.step(
                False,
                f"{site_name}: FMG still has {updated_sn or 'empty'} "
                f"after replace (wanted {real_sn})",
            )


def _status_rows(items):
    rows = []
    for name, ok, detail in items:
        klass = "ok" if ok else "bad"
        label = "ok" if ok else "failed"
        rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td class='{klass}'>{label}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    return rows


def _render(report):
    job_items = []
    ordered = []
    for kind, name, ok, detail in report.jobs:
        if kind == "job":
            ordered.append((name, ok, detail))
        else:
            job_items.append((name, ok, detail))
    job_items = ordered + job_items
    if not job_items:
        job_items = [("Serial replacement", False, "no status recorded")]
    job_rows = _status_rows(job_items)

    conn_rows = []
    for target, ok, detail in report.connectivity:
        klass = "ok" if ok else "bad"
        label = "up" if ok else "down"
        conn_rows.append(
            "<tr>"
            f"<td>{html.escape(target)}</td>"
            f"<td class='{klass}'>{label}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )

    step_rows = []
    for stamp, ok, message in report.steps:
        klass = "ok" if ok else "bad"
        step_rows.append(
            f"<div class='step {klass}'>"
            f"<span class='t'>{html.escape(stamp)}</span> "
            f"{html.escape(message)}</div>"
        )

    if not conn_rows:
        conn_rows.append("<tr><td colspan='3'>No checks ran.</td></tr>")
    if not step_rows:
        step_rows.append("<div class='step'>No progress recorded.</div>")

    return f"""
    <html>
    <head>
        <title>FMG Serial Replacement</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f4f6f9;
                padding: 40px;
                color: #222;
            }}
            h1 {{ margin-bottom: 8px; }}
            h2 {{ margin-top: 28px; font-size: 18px; color: #444; }}
            .card {{
                background: white;
                padding: 20px 24px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
                margin-top: 12px;
            }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }}
            th {{ color: #666; font-size: 13px; }}
            .ok {{ color: #0a7a32; font-weight: 600; }}
            .bad {{ color: #b42318; font-weight: 600; }}
            .step {{ padding: 7px 0; border-bottom: 1px solid #f0f0f0; }}
            .t {{ color: #888; font-family: monospace; margin-right: 8px; }}
            a {{ color: #1677ff; }}
        </style>
    </head>
    <body>
        <h1>FMG Serial Replacement</h1>
        <p>Running FortiManager tasks are cleared before serial changes. Job status is below.</p>
        <h2>Job status</h2>
        <div class="card">
            <table>
                <tr><th>Job</th><th>Status</th><th>Detail</th></tr>
                {''.join(job_rows)}
            </table>
        </div>
        <h2>Connectivity</h2>
        <div class="card">
            <table>
                <tr><th>Target</th><th>Status</th><th>Detail</th></tr>
                {''.join(conn_rows)}
            </table>
        </div>
        <h2>Progress</h2>
        <div class="card">
            {''.join(step_rows)}
        </div>
        <p><a href="/">Back</a></p>
    </body>
    </html>
    """


@router.post("/replace", response_class=HTMLResponse)
def replace_devices():
    try:
        report = replace_serials_on_fmg()
    except Exception as exc:
        report = Report()
        report.job("job", "Serial replacement", False, f"stopped: {exc}")
        report.step(False, f"Replacement stopped: {exc}")
    return _render(report)

import html
import json
import threading
import time
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from utils import load_inventory, api_get, api_get_text, api_post, api_delete

router = APIRouter()

# How long the follow page waits for Fabric Studio before declaring Fail.
# A "Power-on and prepare" task runs several minutes (observed ~3m40s: VM
# state flips to Running almost immediately, the prepare/config work keeps
# going), so the budget is over the task duration, not the VM state flip.
TRACK_TIMEOUT_POWER_ON = 600    # seconds
TRACK_TIMEOUT_POWER_OFF = 120   # seconds
# Timeout used when the follow page is opened right after a reinstall: the
# runtime device is removed, installed again, then boots and prepares.
TRACK_TIMEOUT_REINSTALL = 900   # seconds
TRACK_POLL_MS = 3000            # client-side poll interval

# The follow page stamps `since` (epoch seconds) so we follow the task this
# click started and not an earlier Power-on of the same device.
TASK_SKEW_SECONDS = 15

# Status values Fabric Studio reports for a guest that is no longer running.
POWER_OFF_STATES = {"shut off", "shutoff", "off", "stopped", "powered off", "poweroff"}

# Same body the other reinstall actions send: the POST install already
# uninstalls the current instance, powers the VM on and runs license,
# configuration and post-boot, so no separate power-on is needed afterwards.
REINSTALL_PAYLOAD = {
    "power_on": True,
    "post_boot": True,
    "timeout": 0,
    "license": True,
    "configuration": True,
}

# Power-on payload for /api/v1/runtime/vm/{id}:power-on
POWER_ON_PAYLOAD = {
    "configuration": True,
    "license": True,
    "post_boot": True,
    "timeout": 0,
}

# Reconnaissance notes for the task-follow implementation:
#   GET /api/v1/task            list tasks (id, name, returncode, launched/returned_date)
#   GET /api/v1/task/{id}       one task
#   GET /api/v1/task/{id}:log   task console output as plain text (live while running)
# Power-on tasks are named "Power-on and prepare '<dev>' device"; their
# related_obj is NOT the vm id, so match on the name.

# Per-device reinstall progress, written by the background worker thread and
# read by the status endpoint so the follow page can narrate each phase.
_reinstall_state: dict = {}
_reinstall_guard = threading.Lock()


def _reinstall_worker(device: str):
    st = _reinstall_state.get(device)
    if not st:
        return

    def phase(text):
        st["phase"] = text
        try:
            print(f"[powercontrol] {device}: {text}")
        except Exception:
            pass

    try:
        phase("Removing runtime device from Fabric Studio…")
        api_delete(f"/api/v1/runtime/device/{device}")
        phase("Runtime device removed — waiting 30s before install…")
        time.sleep(30)
        phase("Installing VM again (license, configuration, post-boot)…")
        api_post(f"/api/v1/runtime/device/{device}", REINSTALL_PAYLOAD)
        phase("Install submitted — waiting for the Power-on task to start…")
        st["done"] = True
    except Exception as e:
        st["error"] = str(e)
        st["done"] = True


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _find_poweron_task(tasks, name: str, since: float):
    """Newest Power-on task for `name` launched at/after `since`, else None."""
    marker = f"'{name}'" if name else None
    best = None
    best_la = -1.0
    for t in tasks:
        tname = str(t.get("name") or "")
        if not tname.lower().startswith("power-on"):
            continue
        if marker and marker not in tname:
            continue
        launched = _parse_ts(t.get("launched_date")) or 0.0
        if launched < since:
            continue
        if launched > best_la:
            best, best_la = t, launched
    return best


def _resolve_name(device: str, name: str) -> str:
    if name:
        return name
    try:
        return str(api_get(f"/api/v1/runtime/vm/{device}").get("object", {}).get("name") or "")
    except Exception:
        return ""


def _poweron_task_status(device: str, name: str, since: float) -> dict:
    """Classify the Power-on task this click started, with its live console log.

    The VM reaches Running seconds after power-on, but the "Power-on and
    prepare" task (cabling, login script, factory config) keeps running for
    minutes. Pass only when that task returns rc=0; fail when it returns a
    non-zero rc or the task never shows up (deadline is the client's).
    `log` carries the task console output while it runs.
    """
    try:
        tasks = api_get("/api/v1/task").get("object") or []
    except Exception as e:
        return {"status": f"task list unavailable ({str(e)[:60]})", "result": "pending", "log": ""}

    name = _resolve_name(device, name)
    best = _find_poweron_task(tasks, name, since)

    if best is None:
        return {"status": "waiting for the Power-on task to be created", "result": "pending", "log": ""}

    task_id = best.get("id")
    log = ""
    try:
        log = api_get_text(f"/api/v1/task/{task_id}:log")
    except Exception:
        log = ""

    rc = best.get("returncode")
    if rc is None and not best.get("returned_date"):
        result = "pending"
        status = f"Power-on task {task_id} running (since {best.get('launched_date', '?')})"
    elif rc == 0:
        result = "pass"
        status = f"Power-on task {task_id} completed rc=0 at {best.get('returned_date', '?')}"
    else:
        result = "fail"
        err = str(best.get("monitor_error") or "").strip()
        status = f"Power-on task {task_id} failed (rc={rc}" + (f" — {err}" if err else "") + ")"

    return {"status": status, "result": result, "log": log, "task_id": task_id}


def _track_timeout(action: str, after_reinstall: bool = False) -> int:
    if after_reinstall:
        return TRACK_TIMEOUT_REINSTALL
    return TRACK_TIMEOUT_POWER_ON if action == "power-on" else TRACK_TIMEOUT_POWER_OFF


def _tracker_page(device: str, name: str, action: str, after_reinstall: bool = False) -> str:
    """Page that follows the power event on Fabric Studio and reports Pass/Fail."""
    label = html.escape(name or device)
    esc_device = html.escape(device, quote=True)
    esc_action = html.escape(action, quote=True)
    timeout_s = _track_timeout(action, after_reinstall)
    verb = "Power On" if action == "power-on" else "Power Off"
    # Captured before render; the task this click starts launches from now on.
    since = int(time.time() - TASK_SKEW_SECONDS)

    # The reinstall offer belongs to the Power On flow: a VM that will not come
    # up is the one worth installing again.
    reinstall_html = ""
    if action == "power-on":
        reinstall_html = f'''
                <form method="post" action="/powercontrol/{esc_device}/reinstall?name={html.escape(name or '', quote=True)}" style="margin-top:16px;">
                    <button class="btn danger" type="submit">Install VM again and Power On</button>
                </form>
        '''

    return f"""
    <html>
    <head>
        <title>{verb} — {label}</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 40px; text-align: center; }}
            .message-box {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); display: inline-block; min-width: 560px; max-width: 900px; text-align: left; }}
            .btn {{ padding: 10px 16px; border-radius: 4px; border: none; cursor: pointer; color: white; font-size: 14px; font-weight: 500; }}
            .danger {{ background: #dc3545; }}
            .btn:hover {{ opacity: 0.9; }}
            .pass {{ color: #2e7d32; }}
            .fail {{ color: #c62828; }}
            .pending {{ color: #555; }}
            a {{ color: #1677ff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            code {{ background: #f1f3f5; padding: 2px 6px; border-radius: 4px; }}
            h3 {{ margin: 22px 0 8px; font-size: 14px; color: #444; text-transform: uppercase; letter-spacing: .04em; }}
            #logbox {{ background: #101418; color: #d7dde3; font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px;
                       line-height: 1.45; padding: 12px 14px; border-radius: 6px; height: 340px; overflow-y: auto;
                       white-space: pre-wrap; word-break: break-word; margin: 0; text-align: left; }}
        </style>
    </head>
    <body>
        <h2>{verb} — {label}</h2>
        <div style="margin:12px 0 20px;text-align:center;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>
        <div class="message-box">
            <h2 id="headline" class="pending" style="margin-top:0;">Waiting for the {verb} event on Fabric Studio…</h2>
            <p class="pending" id="detail" style="margin-top:6px;">Live status: <b id="status">connecting…</b></p>
            <p>Following: <code>{esc_device}</code></p>
            <div id="fail-offer" style="display:none;">
                <p class="fail">Fabric Studio did not complete the {verb} task within {timeout_s} seconds.</p>
                {reinstall_html}
            </div>
            <h3>Progress log</h3>
            <pre id="logbox" role="log" aria-live="polite"></pre>
            <br>
            <a href="/powercontrol">&larr; Back to Power Control dashboard</a>
        </div>
        <script>
            const device = {json.dumps(device)};
            const name = {json.dumps(name or "")};
            const action = {json.dumps(action)};
            const verb = {json.dumps(verb)};
            const pollMs = {TRACK_POLL_MS};
            const deadline = Date.now() + {timeout_s} * 1000;
            const timeoutS = {timeout_s};
            const since = {since};
            let finished = false;
            const events = [];      // portal-side events
            let taskLog = "";       // full Fabric Studio task console, replaced each poll
            let lastStatus = null;
            let lastPhase = null;

            function now() {{
                const d = new Date();
                return d.toTimeString().slice(0, 8);
            }}

            function render() {{
                const box = document.getElementById('logbox');
                const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 10;
                let text = events.join('\\n');
                if (taskLog) {{
                    if (text) text += '\\n';
                    text += '---- Fabric Studio task console ----\\n' + taskLog.replace(/\\n+$/, '');
                }}
                box.textContent = text || '(waiting for output…)';
                if (atBottom) box.scrollTop = box.scrollHeight;
            }}

            function ev(msg) {{
                events.push(now() + '  ' + msg);
                render();
            }}

            function headline(cls, text) {{
                const h = document.getElementById('headline');
                h.className = cls;
                h.textContent = text;
            }}

            function showPass(status) {{
                finished = true;
                headline('pass', 'PASS — ' + verb + ' completed on ' + name);
                document.getElementById('detail').innerHTML =
                    'Fabric Studio: <b>' + escHtml(status) + '</b>';
                document.getElementById('fail-offer').style.display = 'none';
                ev('PASS — ' + status);
            }}

            function showFail(reason) {{
                if (finished) return;
                finished = true;
                headline('fail', 'FAIL — ' + verb + ' did not complete on ' + name);
                document.getElementById('detail').innerHTML = reason;
                document.getElementById('fail-offer').style.display = 'block';
                ev('FAIL — ' + reason.replace(/<[^>]+>/g, ''));
            }}

            function escHtml(s) {{
                return String(s).replace(/[&<>"']/g, c => (
                    {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]
                ));
            }}

            ev('{verb} requested for ' + (name || device) + ' — following Fabric Studio');

            async function tick() {{
                if (finished) return;
                if (Date.now() > deadline) {{
                    showFail('Timed out after ' + timeoutS + 's waiting for the task to finish.');
                    return;
                }}
                try {{
                    const qs = '?since=' + since + '&name=' + encodeURIComponent(name);
                    const r = await fetch('/powercontrol/' + encodeURIComponent(device) + '/status/' + encodeURIComponent(action) + qs, {{cache: 'no-store'}});
                    if (r.ok) {{
                        const d = await r.json();
                        const s = d.status || 'unknown';
                        if (s !== lastStatus) {{
                            lastStatus = s;
                            document.getElementById('status').textContent = s;
                            ev('status: ' + s);
                        }}
                        if (d.phase && d.phase !== lastPhase) {{
                            lastPhase = d.phase;
                            ev(d.phase);
                        }}
                        if (typeof d.log === 'string' && d.log !== taskLog) {{
                            taskLog = d.log;
                            render();
                        }}
                        if (d.result === 'pass') {{ showPass(d.status); return; }}
                        if (d.result === 'fail') {{ showFail('Fabric Studio: <b>' + escHtml(d.status || 'error') + '</b>'); return; }}
                    }}
                }} catch (e) {{
                    // transient API error — keep polling until the deadline
                }}
                setTimeout(tick, pollMs);
            }}
            tick();
        </script>
    </body>
    </html>
    """


def _error_page(message: str) -> str:
    return f"""
    <html>
    <head>
        <title>Power Control Error</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 40px; text-align: center; }}
            .message-box {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); display: inline-block; }}
            a {{ color: #1677ff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="message-box">
            <h2>Power Control Error</h2>
            <p>{html.escape(message)}</p>
            <br>
            <a href="/powercontrol">&larr; Back to Power Control dashboard</a>
        </div>
    </body>
    </html>
    """


@router.get("/powercontrol", response_class=HTMLResponse)
def powercontrol_dashboard():
    inventory = load_inventory()
    devices = inventory.get("powercontrol", [])

    # Pre-fetch the device objects to resolve name -> id
    try:
        dev_resp = api_get("/api/v1/runtime/device")
        all_objects = dev_resp.get("object", [])
    except Exception:
        all_objects = []

    rows = ""
    for dev in devices:
        # Find device ID for this dev name
        row = next((o for o in all_objects if o.get("name") == dev), None)
        button = ""
        status = "unknown"
        dev_id = None

        if not row:
            status = "not found in fabric backend"
        else:
            dev_id = row["id"]
            try:
                resp = api_get(f"/api/v1/runtime/vm/{dev_id}/status")
                status = resp.get("object", {}).get("status", "unknown")
            except Exception as e:
                status = f"error: {str(e)[:30]}"

            if status.lower() == "running":
                button = f'''
                <form action="/powercontrol/{dev_id}/power-off?name={dev}" method="post" style="display:inline;">
                    <button class="btn danger" type="submit">Power Off</button>
                </form>
                '''
            elif status.lower() != "error":
                button = f'''
                <form action="/powercontrol/{dev_id}/power-on?name={dev}" method="post" style="display:inline;">
                    <button class="btn primary" type="submit">Power On</button>
                </form>
                '''

        rows += f'''
        <tr>
            <td style="padding:10px; border-bottom:1px solid #ddd;">{dev}</td>
            <td style="padding:10px; border-bottom:1px solid #ddd;"><b>{status}</b></td>
            <td style="padding:10px; border-bottom:1px solid #ddd;">{button}</td>
        </tr>
        '''

    return f"""
    <html>
    <head>
        <title>Power Control Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f4f6f9; padding: 40px; }}
            .btn {{ padding: 8px 12px; border-radius: 4px; border: none; cursor: pointer; color: white; font-size: 14px; font-weight: 500; }}
            .primary {{ background: #1677ff; }}
            .danger {{ background: #dc3545; }}
            .btn:hover {{ opacity: 0.9; }}
            table {{ border-collapse: collapse; min-width: 600px; background: white; margin-top: 20px; box-shadow: 0 3px 8px rgba(0,0,0,0.08); border-radius: 8px; overflow: hidden; }}
            th {{ background: #f8f9fa; text-align: left; padding: 15px; border-bottom: 2px solid #dee2e6; color: #444; }}
            td {{ padding: 15px; border-bottom: 1px solid #eee; }}
            a {{ color: #1677ff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h2>Power Control Dashboard</h2>
        <div style="margin:12px 0 20px;"><a href="/" style="display:inline-block;text-decoration:none;background:#007bff;color:white;padding:8px 14px;border-radius:6px;font-weight:500;">&larr; Back to Home</a></div>
        <table>
            <tr><th>Device</th><th>Status</th><th>Action</th></tr>
            {rows}
        </table>
    </body>
    </html>
    """


@router.get("/powercontrol/{device}/status/{action}", response_class=JSONResponse)
def powercontrol_status(device: str, action: str, since: float = 0, name: str = ""):
    """Live follow state for the follow page.

    Power On follows the "Power-on and prepare" *task* on Fabric Studio: the
    VM status flips to Running almost immediately while the task keeps
    configuring the device, so task returncode is what makes a real
    Pass/Fail. The response also carries the task's live console log
    (`log`) and, during a reinstall, the current reinstall `phase`.
    Power Off still follows the VM status (there is no early-flip
    problem the other way).

    result: pending = keep following, pass = task finished rc=0 / VM off,
    fail = task failed or Fabric Studio reports an error state. An API
    failure returns pending so a transient error does not fail a boot; the
    page's deadline is the backstop.
    """
    if action == "power-on" and since > 0:
        resp = _poweron_task_status(device, name, since)
        st = _reinstall_state.get(device)
        if st:
            if st.get("error"):
                resp = {"status": f"Reinstall failed: {st['error']}", "result": "fail", "log": ""}
            elif st.get("phase"):
                resp["phase"] = st["phase"]
        return JSONResponse(resp)

    try:
        resp = api_get(f"/api/v1/runtime/vm/{device}/status")
        status = str(resp.get("object", {}).get("status", "unknown"))
    except Exception as e:
        return JSONResponse({"status": f"unavailable ({str(e)[:60]})", "result": "pending"})

    low = status.lower().strip()
    if action == "power-on":
        if low == "running":
            result = "pass"
        elif low == "error":
            result = "fail"
        else:
            result = "pending"
    elif action == "power-off":
        if low in POWER_OFF_STATES:
            result = "pass"
        elif low == "error":
            result = "fail"
        else:
            result = "pending"
    else:
        result = "fail"

    return JSONResponse({"status": status, "result": result})


# Registered before /powercontrol/{device}/{action} so "reinstall" is not
# swallowed by the {action} path parameter.
@router.post("/powercontrol/{device}/reinstall", response_class=HTMLResponse)
def powercontrol_reinstall(device: str, name: str = None):
    # Run delete + sleep + install in the background: the follow page must
    # render immediately and narrate the phases instead of staring at a
    # blocked browser tab for 30+ seconds.
    with _reinstall_guard:
        st = _reinstall_state.get(device)
        if not st or st.get("done"):
            _reinstall_state[device] = {"phase": "Reinstall requested…", "error": None, "done": False}
            threading.Thread(target=_reinstall_worker, args=(device,), daemon=True).start()

    return HTMLResponse(_tracker_page(device, name, "power-on", after_reinstall=True))


@router.post("/powercontrol/{device}/{action}", response_class=HTMLResponse)
def powercontrol_action(device: str, action: str, name: str = None):
    if action not in ("power-on", "power-off"):
        return HTMLResponse(_error_page("Invalid action"), status_code=400)

    payload = POWER_ON_PAYLOAD if action == "power-on" else None
    try:
        api_post(f"/api/v1/runtime/vm/{device}:{action}", payload)
    except Exception as e:
        # A Power On the API refused is a Fail — offer the reinstall path.
        page = (
            _tracker_page(device, name, action)
            if action == "power-on"
            else _error_page(f"Error sending {action} to {name or device}: {e}")
        )
        return HTMLResponse(page)

    return HTMLResponse(_tracker_page(device, name, action))

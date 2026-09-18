#!/usr/bin/env python3
"""
workshop_check.py — Cross-platform workshop status checker.

Triggers a lab-status recalculation on each portal host, then polls for
results and prints a colour summary table. No external packages required
— only the Python 3 standard library.

Usage
-----
  # Pass hosts directly on the command line:
  python3 workshop_check.py 10.0.0.1 10.0.0.2 lab3.example.com

  # Or put one host per line in a text file:
  python3 workshop_check.py --file hosts.txt

  # Or use the Instances CSV export (Instance,IP,FQDN,...):
  python3 workshop_check.py --file Instances_mexico-sase-onramp_2026-5-29.csv

  # Combine both:
  python3 workshop_check.py --file hosts.txt 10.0.0.3

  # Push an alternative inventory.yaml so every host validates a different lab:
  python3 workshop_check.py 10.0.0.1 --inventory /path/to/inventory.yaml

Hosts may be plain IPs or hostnames; http(s):// prefixes are stripped.
The portal is always reached on HTTPS port 13015.

With --inventory, the file's contents are sent to each portal as the body of the
recalculate request; the remote labstatus then validates against that inventory
instead of its own local inventory.yaml. The file is forwarded verbatim (no
parsing) so this stays stdlib-only.

When --file points to a CSV with an "Instance,IP,FQDN,..." header, the IP
column is used as the host address (preferred over FQDN). Rows missing IP,
iam_user_name, or password are marked as SKIPPED without attempting a
connection.

Results are saved to workshop_status_results.json when the run finishes.
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PORT = 13015
MAX_POLLS = 30
POLL_INTERVAL = 10          # seconds between polls
CONNECT_TIMEOUT = 20        # seconds for POST /recalculate
READ_TIMEOUT = 25           # seconds for GET /api/labstatus
MAX_CONCURRENT = 20         # parallel threads for the poll phase
RESULTS_FILE = "workshop_status_results.json"

# ---------------------------------------------------------------------------
# ANSI colour helpers (auto-disabled on Windows if the terminal doesn't support it)
# ---------------------------------------------------------------------------
def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        # Windows 10+ supports ANSI in conhost when ENABLE_VIRTUAL_TERMINAL_PROCESSING is on.
        # Enable it programmatically so colours work in cmd.exe / PowerShell.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

USE_COLOR = _supports_color()

def _c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t):   return _c("32", t)
def red(t):     return _c("31", t)
def yellow(t):  return _c("33", t)
def cyan(t):    return _c("36", t)
def bold(t):    return _c("1",  t)
def dim(t):     return _c("2",  t)

# ---------------------------------------------------------------------------
# SSL context — disables certificate verification (self-signed certs on labs)
# ---------------------------------------------------------------------------
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------------------
# Host normalisation
# ---------------------------------------------------------------------------
def normalize_host(raw: str) -> str:
    h = raw.strip()
    h = re.sub(r"^https?://", "", h, flags=re.IGNORECASE)
    h = h.rstrip("/")
    return h

def parse_hosts(values: List[str]) -> List[str]:
    hosts, seen = [], set()
    for raw in values:
        h = normalize_host(raw)
        if h and h not in seen:
            seen.add(h)
            hosts.append(h)
    return hosts

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _http_post(url: str, timeout: int, body: Optional[str] = None,
               content_type: str = "text/plain") -> Optional[str]:
    """POST to url; returns an error string or None on success.

    ``body`` is optional raw text (e.g. an inventory YAML document). When
    provided it is sent as the request body with the given ``content_type``.
    """
    data = body.encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx):
            return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:
        return str(exc)


def _http_get_json(url: str, timeout: int) -> Tuple[Optional[dict], Optional[str]]:
    """GET url and parse JSON; returns (data, error)."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            raw = resp.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "Unexpected response format"
        return data, None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"
    except Exception as exc:
        return None, str(exc)

# ---------------------------------------------------------------------------
# Per-host logic
# ---------------------------------------------------------------------------
def request_recalculate(host: str, inventory_text: Optional[str] = None) -> Optional[str]:
    url = f"https://{host}:{PORT}/labstatus/recalculate"
    for attempt in range(2):
        err = _http_post(url, timeout=CONNECT_TIMEOUT, body=inventory_text)
        if err is None:
            return None
        if attempt == 0:
            time.sleep(2)
    return err


def request_labstatus(host: str) -> Tuple[Optional[dict], Optional[str]]:
    url = f"https://{host}:{PORT}/api/labstatus"
    for attempt in range(2):
        data, err = _http_get_json(url, timeout=READ_TIMEOUT)
        if err is None and data is not None:
            if "failed_count" not in data:
                return None, "Invalid response: missing failed_count"
            return data, None
        if attempt == 0:
            time.sleep(2)
    return None, err or "Failed after retries"

# ---------------------------------------------------------------------------
# Entry state for each host
# ---------------------------------------------------------------------------
def make_entry(host: str, instance_id: str = None, fqdn: str = None) -> dict:
    return {
        "host": host,
        "fqdn": fqdn or "",
        "instance_id": instance_id,
        "state": "pending",      # pending | recalculating | done | error | timed_out | skipped
        "failed_count": None,
        "power_results": [],
        "license_results": [],
        "last_run": None,
        "error": None,
    }


def make_skipped_entry(instance_id: str, label: str, missing: List[str]) -> dict:
    return {
        "host": label,
        "instance_id": instance_id,
        "state": "skipped",
        "failed_count": None,
        "power_results": [],
        "license_results": [],
        "last_run": None,
        "error": "Missing: " + ", ".join(missing),
    }

# ---------------------------------------------------------------------------
# CSV input support
# ---------------------------------------------------------------------------
def is_csv_file(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip().startswith("Instance,")
    except OSError:
        return False


def load_from_csv(path: str) -> Tuple[List[dict], List[dict]]:
    """Parse an Instances CSV. Returns (runnable_entries, skipped_entries)."""
    runnable: List[dict] = []
    skipped: List[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                instance_id = (row.get("Instance") or "").strip()
                ip = (row.get("IP") or "").strip()
                fqdn = (row.get("FQDN") or "").strip()
                iam_username = (row.get("iam_user_name") or "").strip()
                password = (row.get("password") or "").strip()

                missing = []
                if not ip:
                    missing.append("IP")
                if not iam_username:
                    missing.append("iam_user_name")
                if not password:
                    missing.append("password")

                if missing:
                    label = fqdn or instance_id or "unknown"
                    skipped.append(make_skipped_entry(instance_id, label, missing))
                else:
                    runnable.append(make_entry(ip, instance_id=instance_id, fqdn=fqdn))
    except OSError as exc:
        print(red(f"Error reading CSV file: {exc}"), file=sys.stderr)
        sys.exit(1)
    return runnable, skipped

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def _status_label(entry: dict) -> str:
    s = entry["state"]
    if s == "done":
        fc = entry.get("failed_count", 0) or 0
        return green("PASS") if fc == 0 else red(f"FAIL ({fc} failed)")
    if s == "skipped":
        return red("SKIPPED")
    if s == "error":
        return red("ERROR")
    if s == "timed_out":
        return yellow("TIMEOUT")
    if s == "recalculating":
        return cyan("checking…")
    return dim("pending")


def _failed_names(entry: dict) -> str:
    power = [r["name"] for r in entry.get("power_results", []) if not r.get("ok")]
    lic   = [r["name"] for r in entry.get("license_results", []) if not r.get("ok")]
    parts = []
    if power:
        parts.append("Power: " + ", ".join(power))
    if lic:
        parts.append("License: " + ", ".join(lic))
    return "  |  ".join(parts)


def _labstatus_url(entry: dict) -> str:
    addr = entry.get("fqdn") or entry["host"]
    return f"https://{addr}:{PORT}/labstatus"


def print_table(entries: List[dict], title: str = ""):
    if not entries:
        return

    show_instance = any(e.get("instance_id") for e in entries)

    col_inst = 0
    if show_instance:
        col_inst = max(len(e.get("instance_id") or "") for e in entries)
        col_inst = max(col_inst, 8)  # min width for "INSTANCE" header

    col_url = max(len(_labstatus_url(e)) for e in entries)
    col_url = max(col_url, 3)  # min width for "URL" header

    if title:
        print(f"\n{bold(title)}")

    if show_instance:
        header = f"  {'INSTANCE':<{col_inst}}   {'URL':<{col_url}}   {'STATUS':<18}   DETAIL"
    else:
        header = f"  {'URL':<{col_url}}   {'STATUS':<18}   DETAIL"
    print(dim(header))
    print(dim("  " + "-" * (col_inst + col_url + (5 if show_instance else 0) + 50)))

    def _sort_key(e):
        s = e["state"]
        if s == "skipped":
            return 0
        if s == "done" and (e.get("failed_count") or 0) == 0:
            return 1
        return 2  # failed, error, timed_out

    for e in sorted(entries, key=_sort_key):
        status = _status_label(e)
        detail = ""
        if e["state"] in ("error", "skipped"):
            detail = dim(str(e.get("error") or ""))
        elif e["state"] == "done" and (e.get("failed_count") or 0) > 0:
            detail = _failed_names(e)
        raw_url = _labstatus_url(e)
        colored_url = cyan(raw_url) if e["state"] != "skipped" else dim(raw_url)
        url_pad = " " * max(0, col_url - len(raw_url))
        raw_status = e["state"]
        pad = max(0, 18 - len(raw_status) - 2)
        if show_instance:
            inst = (e.get("instance_id") or "")
            print(f"  {inst:<{col_inst}}   {colored_url}{url_pad}   {status}{' ' * pad}   {detail}")
        else:
            print(f"  {colored_url}{url_pad}   {status}{' ' * pad}   {detail}")

    print()


def print_summary(entries: List[dict]):
    passed   = sum(1 for e in entries if e["state"] == "done" and (e.get("failed_count") or 0) == 0)
    failed   = sum(1 for e in entries if e["state"] == "done" and (e.get("failed_count") or 0) > 0)
    errors   = sum(1 for e in entries if e["state"] in ("error", "timed_out"))
    skipped  = sum(1 for e in entries if e["state"] == "skipped")
    pending  = sum(1 for e in entries if e["state"] in ("pending", "recalculating"))
    total    = len(entries)

    parts = [
        green(f"{passed} passed"),
        red(f"{failed} failed"),
        yellow(f"{errors} connectivity issues"),
        red(f"{skipped} skipped (incomplete)"),
    ]
    if pending:
        parts.append(cyan(f"{pending} pending"))
    print(bold(f"Summary ({total} hosts): ") + "  ".join(parts))

# ---------------------------------------------------------------------------
# Main run logic
# ---------------------------------------------------------------------------
def run(entries: List[dict], inventory_text: Optional[str] = None) -> List[dict]:
    total = len(entries)
    lock  = threading.Lock()

    print(f"\n{bold('Phase 1:')} Triggering recalculate on {total} host(s)…")

    # Trigger recalculate concurrently
    def trigger(entry: dict):
        err = request_recalculate(entry["host"], inventory_text=inventory_text)
        with lock:
            if err:
                entry["state"] = "error"
                entry["error"] = err
                print(f"  {red('✗')} {entry['host']} — {err}")
            else:
                entry["state"] = "recalculating"
                print(f"  {green('✓')} {entry['host']} — recalculate triggered")

    threads = [threading.Thread(target=trigger, args=(e,), daemon=True) for e in entries]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n{bold('Phase 2:')} Polling for results (up to {MAX_POLLS} × {POLL_INTERVAL}s)…")

    for poll_round in range(MAX_POLLS):
        pending = [e for e in entries if e["state"] == "recalculating"]
        if not pending:
            break

        # Wait before polling so the remote portal has time to finish
        for sec in range(POLL_INTERVAL):
            remaining = POLL_INTERVAL - sec
            pct_done = sum(1 for e in entries if e["state"] in ("done", "error"))
            sys.stdout.write(
                f"\r  Poll {poll_round + 1}/{MAX_POLLS} — "
                f"{pct_done}/{total} done, "
                f"waiting {remaining}s…   "
            )
            sys.stdout.flush()
            time.sleep(1)

        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

        # Poll all still-pending hosts concurrently
        sem = threading.Semaphore(MAX_CONCURRENT)

        def poll_one(entry: dict):
            with sem:
                data, err = request_labstatus(entry["host"])
            with lock:
                if err is not None:
                    entry["state"] = "error"
                    entry["error"] = err
                    return
                if not data.get("last_run"):
                    # Remote job still running; leave as recalculating
                    return
                entry["state"] = "done"
                entry["failed_count"] = int(data.get("failed_count", 0))
                entry["power_results"] = data.get("power_results", []) or []
                entry["license_results"] = data.get("license_results", []) or []
                entry["last_run"] = data.get("last_run")

        poll_threads = [threading.Thread(target=poll_one, args=(e,), daemon=True) for e in pending]
        for t in poll_threads:
            t.start()
        for t in poll_threads:
            t.join()

        settled = sum(1 for e in entries if e["state"] in ("done", "error"))
        print(f"  Poll {poll_round + 1}/{MAX_POLLS} — {settled}/{total} settled")

        if all(e["state"] in ("done", "error") for e in entries):
            break

    # Mark anything still running as timed out
    for e in entries:
        if e["state"] == "recalculating":
            e["state"] = "timed_out"

    return entries


def save_results(entries: List[dict], path: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    payload = {
        "last_run": timestamp,
        "hosts": entries,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(dim(f"Results saved to {path}"))
    except OSError as exc:
        print(yellow(f"Warning: could not save results — {exc}"))

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Check workshop lab status across multiple portal hosts. "
            "No external packages required — only Python 3 stdlib."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "hosts",
        nargs="*",
        metavar="HOST",
        help="One or more portal hosts (IPs or hostnames). http(s):// prefixes are stripped.",
    )
    p.add_argument(
        "--file", "-f",
        metavar="FILE",
        help=(
            "Path to a hosts file. Accepts either a plain text file (one host per line, "
            "# comments ignored) or an Instances CSV with Instance,IP,FQDN,... columns."
        ),
    )
    p.add_argument(
        "--inventory", "-i",
        metavar="FILE",
        help=(
            "Path to an alternative inventory.yaml to push to every portal host. "
            "The remote labstatus will validate against this inventory instead of "
            "its own local inventory.yaml (applied to all hosts in the run)."
        ),
    )
    p.add_argument(
        "--output", "-o",
        metavar="FILE",
        default=RESULTS_FILE,
        help=f"JSON output file (default: {RESULTS_FILE})",
    )
    return p


def load_hosts_from_file(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        print(red(f"Error reading hosts file: {exc}"), file=sys.stderr)
        sys.exit(1)
    result = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(line)
    return result


def main():
    parser = build_parser()
    args = parser.parse_args()

    inventory_text: Optional[str] = None
    if args.inventory:
        try:
            with open(args.inventory, encoding="utf-8") as f:
                inventory_text = f.read()
        except OSError as exc:
            print(red(f"Error reading inventory file: {exc}"), file=sys.stderr)
            sys.exit(1)

    skipped_entries: List[dict] = []
    runnable_entries: Optional[List[dict]] = None  # set when CSV mode is active

    if args.file:
        if is_csv_file(args.file):
            runnable_entries, skipped_entries = load_from_csv(args.file)
            # Add any CLI hosts as plain entries
            cli_entries = [make_entry(h) for h in parse_hosts(list(args.hosts))]
            runnable_entries = runnable_entries + cli_entries
        else:
            raw_hosts: List[str] = list(args.hosts)
            raw_hosts.extend(load_hosts_from_file(args.file))
            runnable_entries = None  # use plain path below
    else:
        raw_hosts = list(args.hosts)
        runnable_entries = None

    if runnable_entries is None:
        # Plain-text / CLI path
        if not raw_hosts:
            print("No hosts provided. Enter hosts one per line (blank line to finish):")
            while True:
                try:
                    line = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                raw_hosts.append(line)

        hosts = parse_hosts(raw_hosts)
        if not hosts and not skipped_entries:
            print(red("No valid hosts found. Exiting."), file=sys.stderr)
            sys.exit(1)
        runnable_entries = [make_entry(h) for h in hosts]

    total_active = len(runnable_entries)
    total_skipped = len(skipped_entries)

    print(bold(f"\nWorkshop Status Check — {total_active} host(s) to check, {total_skipped} skipped"))
    if inventory_text is not None:
        print(f"  {dim('·')} pushing inventory from {args.inventory}")
    for e in runnable_entries:
        label = f"{e['host']}  (instance {e['instance_id']})" if e.get("instance_id") else e["host"]
        print(f"  {dim('·')} {label}")
    if skipped_entries:
        print(bold(f"\nSkipped ({total_skipped}) — missing required fields:"))
        for e in skipped_entries:
            inst = f"instance {e['instance_id']} / " if e.get("instance_id") else ""
            print(f"  {red('✗')} {inst}{e['host']} — {e.get('error', '')}")

    if not runnable_entries:
        # Nothing to run; just display skipped and exit
        all_entries = skipped_entries
        print_table(all_entries, title="Results")
        print_summary(all_entries)
        print()
        save_results(all_entries, args.output)
        sys.exit(1)

    try:
        completed_entries = run(runnable_entries, inventory_text=inventory_text)
    except KeyboardInterrupt:
        print(f"\n{yellow('Interrupted.')}")
        sys.exit(130)

    all_entries = completed_entries + skipped_entries

    print_table(all_entries, title="Results")
    print_summary(all_entries)
    print()

    save_results(all_entries, args.output)

    any_bad = any(
        e["state"] in ("error", "timed_out", "skipped") or (e.get("failed_count") or 0) > 0
        for e in all_entries
    )
    sys.exit(1 if any_bad else 0)


if __name__ == "__main__":
    main()

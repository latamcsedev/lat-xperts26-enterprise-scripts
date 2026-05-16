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

  # Combine both:
  python3 workshop_check.py --file hosts.txt 10.0.0.3

Hosts may be plain IPs or hostnames; http(s):// prefixes are stripped.
The portal is always reached on HTTPS port 13015.

Results are saved to workshop_status_results.json when the run finishes.
"""

import argparse
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
def _http_post(url: str, timeout: int) -> Optional[str]:
    """POST to url; returns an error string or None on success."""
    req = urllib.request.Request(url, data=b"", method="POST")
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
def request_recalculate(host: str) -> Optional[str]:
    url = f"https://{host}:{PORT}/labstatus/recalculate"
    for attempt in range(2):
        err = _http_post(url, timeout=CONNECT_TIMEOUT)
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
def make_entry(host: str) -> dict:
    return {
        "host": host,
        "state": "pending",      # pending | recalculating | done | error | timed_out
        "failed_count": None,
        "power_results": [],
        "license_results": [],
        "last_run": None,
        "error": None,
    }

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def _status_label(entry: dict) -> str:
    s = entry["state"]
    if s == "done":
        fc = entry.get("failed_count", 0) or 0
        return green("PASS") if fc == 0 else red(f"FAIL ({fc} failed)")
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


def print_table(entries: List[dict], title: str = ""):
    col_host  = max(len(e["host"]) for e in entries) if entries else 20
    col_host  = max(col_host, 4)

    if title:
        print(f"\n{bold(title)}")

    header = (
        f"  {'HOST':<{col_host}}   {'STATUS':<18}   DETAIL"
    )
    print(dim(header))
    print(dim("  " + "-" * (col_host + 50)))

    for e in entries:
        status = _status_label(e)
        detail = ""
        if e["state"] == "error":
            detail = dim(str(e.get("error") or ""))
        elif e["state"] == "done" and (e.get("failed_count") or 0) > 0:
            detail = _failed_names(e)
        # Pad status text without ANSI codes for alignment
        raw_status = e["state"]
        pad = max(0, 18 - len(raw_status) - 2)
        print(f"  {e['host']:<{col_host}}   {status}{' ' * pad}   {detail}")

    print()


def print_summary(entries: List[dict]):
    passed   = sum(1 for e in entries if e["state"] == "done" and (e.get("failed_count") or 0) == 0)
    failed   = sum(1 for e in entries if e["state"] == "done" and (e.get("failed_count") or 0) > 0)
    errors   = sum(1 for e in entries if e["state"] in ("error", "timed_out"))
    pending  = sum(1 for e in entries if e["state"] in ("pending", "recalculating"))
    total    = len(entries)

    parts = [
        green(f"{passed} passed"),
        red(f"{failed} failed"),
        yellow(f"{errors} connectivity issues"),
    ]
    if pending:
        parts.append(cyan(f"{pending} pending"))
    print(bold(f"Summary ({total} hosts): ") + "  ".join(parts))

# ---------------------------------------------------------------------------
# Main run logic
# ---------------------------------------------------------------------------
def run(hosts: List[str]) -> List[dict]:
    entries: List[dict] = [make_entry(h) for h in hosts]
    total = len(entries)
    lock  = threading.Lock()

    print(f"\n{bold('Phase 1:')} Triggering recalculate on {total} host(s)…")

    # Trigger recalculate concurrently
    def trigger(entry: dict):
        err = request_recalculate(entry["host"])
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


def save_results(entries: List[dict]):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    payload = {
        "last_run": timestamp,
        "hosts": entries,
    }
    try:
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(dim(f"Results saved to {RESULTS_FILE}"))
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
        help="Path to a text file with one host per line (comments with # are ignored).",
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

    raw_hosts: List[str] = list(args.hosts)
    if args.file:
        raw_hosts.extend(load_hosts_from_file(args.file))

    if not raw_hosts:
        # Interactive fallback: ask user to paste hosts
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

    if not hosts:
        print(red("No valid hosts found. Exiting."), file=sys.stderr)
        sys.exit(1)

    print(bold(f"\nWorkshop Status Check — {len(hosts)} host(s)"))
    for h in hosts:
        print(f"  {dim('·')} {h}")

    try:
        entries = run(hosts)
    except KeyboardInterrupt:
        print(f"\n{yellow('Interrupted.')}")
        sys.exit(130)

    print_table(entries, title="Results")
    print_summary(entries)
    print()

    # Save results
    global RESULTS_FILE
    RESULTS_FILE = args.output
    save_results(entries)

    # Exit with non-zero code if any host failed or had connectivity issues
    any_bad = any(
        e["state"] in ("error", "timed_out") or (e.get("failed_count") or 0) > 0
        for e in entries
    )
    sys.exit(1 if any_bad else 0)


if __name__ == "__main__":
    main()

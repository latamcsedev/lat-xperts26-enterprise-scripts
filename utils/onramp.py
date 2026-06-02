#!/usr/bin/env python3
"""
onramp.py — FortiSASE on-ramp deployment and status tool.

Reads instances from a CSV export and either checks their on-ramp status
or deploys on-ramp connections in batches. Deployment progress is tracked
in a JSON state file so runs can be incremental.

Usage
-----
  # Check status of all valid instances:
  python3 utils/onramp.py -f utils/Instances_mexico-sase-onramp_2026-5-29.csv

  # Deploy next 10 instances to Plano:
  python3 utils/onramp.py -f utils/Instances_mexico-sase-onramp_2026-5-29.csv \\
      --deploy --location Plano --count 10

  # Deploy 5 to Ashburn with custom connection limit:
  python3 utils/onramp.py -f utils/Instances_mexico-sase-onramp_2026-5-29.csv \\
      --deploy --location Ashburn --count 5 --connections 150

CSV columns required: Instance, IP, FQDN, Passphrase, account_id, iam_user_name, password
Rows missing IP, iam_user_name, or password are silently discarded.

Locations: Vancouver, France, SanJose, Ashburn, Plano, Madrid
"""

import argparse
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib3.exceptions import InsecureRequestWarning

# ---------------------------------------------------------------------------
# SSL suppression (self-signed certs on demo portal)
# ---------------------------------------------------------------------------
os.environ["REQUESTS_CA_BUNDLE"] = ""
os.environ["CURL_CA_BUNDLE"] = ""
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
IPSEC_URL = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec"
ONRAMP_URL = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec/on-ramp/connection_limit"

REGION_MAP = {
    "vancouver": "region1",
    "france":    "region2",
    "sanjose":   "region3",
    "ashburn":   "region4",
    "plano":     "region5",
    "madrid":    "region6",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    """Return valid rows from CSV — discards rows missing IP, iam_user_name, or password."""
    rows = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ip = (row.get("IP") or "").strip()
            user = (row.get("iam_user_name") or "").strip()
            pwd = (row.get("password") or "").strip()
            if not ip or not user or not pwd:
                skipped += 1
                continue
            rows.append({
                "instance": (row.get("Instance") or "").strip(),
                "ip": ip,
                "fqdn": (row.get("FQDN") or "").strip(),
                "iam_user_name": user,
                "password": pwd,
            })
    if skipped:
        print(f"[info] Skipped {skipped} row(s) with missing IP, iam_user_name, or password.")
    return rows


def state_path(csv_path: str) -> Path:
    p = Path(csv_path)
    return p.parent / (p.stem + "_state.json")


def load_state(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"deployed": {}}


def save_state(path: Path, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"[warn] Could not write state file: {e}")


def get_token(username: str, password: str) -> str | None:
    """Authenticate and return bearer token, or None on failure."""
    payload = {
        "username": username,
        "password": password,
        "client_id": "FortiSASE",
        "client_secret": "",
        "grant_type": "password",
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(AUTH_URL, json=payload, headers=headers, verify=False, timeout=30)
        if resp.status_code != 200:
            print(f"  [auth] Failed ({resp.status_code}), retrying in 10s...")
            time.sleep(10)
            resp = requests.post(AUTH_URL, json=payload, headers=headers, verify=False, timeout=30)
        if resp.status_code != 200:
            print(f"  [auth] Failed after retry ({resp.status_code}): {resp.text[:200]}")
            return None
        return resp.json()["access_token"]
    except Exception as e:
        print(f"  [auth] Exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------

def cmd_status(rows: list[dict], state: dict) -> None:
    deployed_ids = set(state.get("deployed", {}).keys())
    total = len(rows)
    checked = 0
    errors = 0

    col_w = [10, 22, 8, 18, 18]
    header = (
        f"{'Instance':<{col_w[0]}}  {'iam_user_name':<{col_w[1]}}  "
        f"{'Deployed':<{col_w[2]}}  {'resource_status':<{col_w[3]}}  {'state':<{col_w[4]}}"
    )
    print()
    print(header)
    print("-" * (sum(col_w) + 8))

    for row in rows:
        instance = row["instance"]
        user = row["iam_user_name"]
        is_deployed = "YES" if instance in deployed_ids else "NO"

        token = get_token(user, row["password"])
        if not token:
            errors += 1
            print(
                f"{instance:<{col_w[0]}}  {user:<{col_w[1]}}  "
                f"{is_deployed:<{col_w[2]}}  {'AUTH_ERROR':<{col_w[3]}}  {'':<{col_w[4]}}"
            )
            continue

        try:
            resp = requests.get(
                IPSEC_URL,
                headers={"Authorization": f"Bearer {token}"},
                verify=False,
                timeout=30,
            )
            data = resp.json()
            resource_status = data["data"]["config_sites"][0]["resource_status"]
            ipsec_state = data["data"]["state"]
        except Exception:
            resource_status = "ERROR"
            ipsec_state = ""
            errors += 1

        checked += 1
        print(
            f"{instance:<{col_w[0]}}  {user:<{col_w[1]}}  "
            f"{is_deployed:<{col_w[2]}}  {str(resource_status):<{col_w[3]}}  {str(ipsec_state):<{col_w[4]}}"
        )

    print()
    pending = total - len(deployed_ids)
    print(f"Summary: {total} instances  |  {len(deployed_ids)} deployed  |  {pending} pending  |  {errors} error(s)")


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def cmd_deploy(rows: list[dict], state: dict, state_file: Path,
               region_code: str, location_name: str, count: int, connections: int) -> None:
    deployed = state.setdefault("deployed", {})
    pending = [r for r in rows if r["instance"] not in deployed]

    if not pending:
        print("All valid instances are already deployed.")
        return

    if count > len(pending):
        print(f"[warn] Only {len(pending)} undeployed instance(s) available; deploying all of them.")
        count = len(pending)

    batch = pending[:count]
    success = 0
    failed = 0

    print(f"\nDeploying {count} instance(s) to {location_name} ({region_code}), {connections} connections each.\n")

    for row in batch:
        instance = row["instance"]
        user = row["iam_user_name"]
        print(f"  [{instance}] {user}", end=" ... ", flush=True)

        token = get_token(user, row["password"])
        if not token:
            print("FAILED (auth)")
            failed += 1
            continue

        try:
            payload = {"regions": [{"name": region_code, "connections": connections}]}
            resp = requests.post(
                ONRAMP_URL,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                verify=False,
                timeout=30,
            )
            if resp.status_code == 200:
                deployed[instance] = {
                    "deployed_at": datetime.now(timezone.utc).isoformat(),
                    "location": location_name,
                    "connections": connections,
                }
                save_state(state_file, state)
                print("OK")
                success += 1
            else:
                print(f"FAILED ({resp.status_code}): {resp.text[:200]}")
                failed += 1
        except Exception as e:
            print(f"FAILED (exception): {e}")
            failed += 1

    total_deployed = len(deployed)
    remaining = len([r for r in rows if r["instance"] not in deployed])
    print(f"\nThis run: {success} deployed, {failed} failed.")
    print(f"Overall: {total_deployed} deployed, {remaining} remaining.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-f", "--file", metavar="CSV", required=True,
                        help="Path to the instances CSV file")
    parser.add_argument("--deploy", action="store_true",
                        help="Deploy on-ramp (default: status check)")
    parser.add_argument("--location", metavar="NAME",
                        help=f"Deployment region: {', '.join(k.title() for k in REGION_MAP)}")
    parser.add_argument("--count", type=int, metavar="N",
                        help="Number of instances to deploy this run")
    parser.add_argument("--connections", type=int, default=200, metavar="N",
                        help="Connection limit per instance (default: 200)")
    args = parser.parse_args()

    if args.deploy:
        if not args.location:
            parser.error("--location is required when using --deploy")
        if not args.count:
            parser.error("--count is required when using --deploy")
        region_key = args.location.lower().replace(" ", "")
        if region_key not in REGION_MAP:
            valid = ", ".join(k.title() for k in REGION_MAP)
            parser.error(f"Unknown location '{args.location}'. Valid options: {valid}")
        region_code = REGION_MAP[region_key]

    rows = load_csv(args.file)
    if not rows:
        print("No valid rows found in CSV. Exiting.")
        sys.exit(1)
    print(f"Loaded {len(rows)} valid instance(s) from {args.file}")

    sf = state_path(args.file)
    state = load_state(sf)

    if args.deploy:
        cmd_deploy(rows, state, sf, region_code, args.location.title(),
                   args.count, args.connections)
    else:
        cmd_status(rows, state)


if __name__ == "__main__":
    main()

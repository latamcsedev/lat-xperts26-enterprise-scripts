# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LATAM Automation Portal — a FastAPI web app for managing Fortinet lab environments (FortiGate, FortiManager, FortiAnalyzer). It controls VM power states, validates lab readiness, simulates network conditions, and orchestrates device provisioning via a Fabric Studio backend API.

## Running locally

```bash
/opt/homebrew/bin/python3 -m venv nosync/py-env
. nosync/py-env/bin/activate
pip install fastapi uvicorn paramiko pyyaml paramiko_expect python-multipart requests jinja2

cd web-int
export $(cat ../nosync/credentials.env | sed 's/ *= */=/g' | xargs)
python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
```

For hot-reload during development: `uvicorn main:app --reload` (run from `web-int/`).

## Deploying to production

Run `./deploy.sh` on the target Linux VM. It installs packages, creates a venv at `py-env/`, registers a systemd service (`latam-portal`) that serves HTTPS on port 443, and runs `postdeploy_configuration.py`. The service runs with `WorkingDirectory=/opt/lat-scripts/web-int`.

Check service logs: `journalctl -u latam-portal`

## Architecture

```
web-int/
├── main.py          # Registers all routers into the FastAPI app
├── utils.py         # Shared layer: SSH (paramiko), Fabric Studio OAuth, inventory loader
├── inventory.yaml   # Source of truth: device IPs, credentials, expected power/license states
├── templates/       # Jinja2 templates (only traffic.html uses this; other routers render HTML inline)
└── routers/         # One file per feature area
```

**To add a new feature:** create `routers/new_feature.py` with an `APIRouter`, add it to `main.py` via `app.include_router()`. Put shared SSH/API logic in `utils.py`.

## Key subsystems

**Fabric Studio API (`utils.py`):** OAuth2 client-credentials token cached in memory with auto-refresh. `api_get`, `api_post`, `api_delete` handle bearer auth and 401 retry. Credentials (`FABRIC_HOST`, `CREDENTIAL`) come from `/fabric/credentials.env` on the VM — never committed to the repo.

**SSH to FortiOS (`utils.py`, `routers/lab_validation.py`):** Paramiko with fallback to blank password on first boot, then forced password change. FortiOS paginates output with `--More--`; `read_ssh_output_complete()` handles this by sending spaces.

**`inventory.yaml`:** Drives everything. Contains `fgt_user`/`fgt_password` for SSH, device IPs under `sites`/`sites_v8`, `powercheck` (expected VM states), `licensecheck` (IPs for SSH license queries), and `sum_sate` (expected total OK count for workshop validation). Note: the key is misspelled `sum_sate` — `workshop_status.py` handles both spellings.

**Background jobs:** Long-running checks (lab validation, workshop status) run in `threading.Thread` (daemon). Progress is tracked in a module-level `job_state` dict protected by `threading.Lock`. The frontend polls `/*/status` JSON endpoints every 2 seconds and reloads the page on completion.

**Caching:** Results are written to JSON files in the working directory (`labstatus_cache.json`, `labstatus_partial.json`, `workshop_status_cache.json`). These are gitignored except `workshop_status_cache.json`.

**HTML rendering:** Most routers build HTML strings directly in Python using f-strings and `html.escape()`. Only `traffic_control.py` and `home.py` use Jinja2 templates from `web-int/templates/`.

## Lab validation (`routers/lab_validation.py`)

- `/labstatus` — dashboard showing power + license state for all devices in `inventory.yaml`
- `/labstatus/recalculate` (POST) — starts background refresh; JS polls `/labstatus/status`
- `/api/labstatus` — JSON endpoint (used by `workshop_status.py` on remote portals)
- License status is parsed from `get system status` SSH output; `clean_license_output()` strips FortiOS prompts and `--More--` markers
- Debug log written to `/root/log.txt` for parse failures

## Workshop status (`routers/workshop_status.py`)

Multi-lab dashboard that hits up to 100 remote portal instances (at `https://<host>:13015`). Calls `/labstatus/recalculate` to trigger refresh on each, then reads back results from `/api/labstatus`. Compares each lab's `sum_state` against the local `inventory.yaml` value to show match/mismatch.

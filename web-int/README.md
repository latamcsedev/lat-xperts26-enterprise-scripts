# LATAM Automation Portal

This repository contains the backend API and frontend views for the LATAM Automation Portal. It is built using FastAPI and organized in a modular structure for readability and scalability.

## Directory Structure

```text
web-int/
├── main.py                # Application entry point. Registers all modular routers with FastAPI.
├── utils.py               # Shared utility layer bridging authentication, SSH connections, inventory parsing, and Jinja templates.
├── inventory.yaml         # Contains site information, credentials, and blueprint details used across the portal.
├── templates/             # HTML Jinja2 templates for rendering frontend pages (e.g., traffic.html).
└── routers/               # Isolated API endpoints (controls) split by feature area:
    ├── home.py            # Renders the main portal dashboard.
    ├── inventory.py       # Exposes the active FortiGate inventory as a downloadable CSV.
    ├── factory_reset.py   # Executes global factory resets on configured FortiGate devices.
    ├── fmg_replacement.py # Orchestrates FortiManager device swaps and serial replacement logic.
    ├── power_control.py   # Connects to backend APIs to report and toggle virtual machine power states.
    ├── traffic_control.py # Emulates network conditions (delay, loss, corruption) and manages simulated cable breakages.
    └── debug.py           # Authenticates with the Fabric OAuth API and validates credentials. 
```

## Running Locally

To manually spin up the portal locally with hot-reloading:

```bash
uvicorn main:app --reload
```

## Development and Extension

When adding new controls or pages:
1. Create a newly isolated route file inside the `routers/` directory (e.g. `routers/new_feature.py`).
2. Instantiate an `APIRouter()` within the script and bind your new endpoints to it.
3. Inject the router into the main FastAPI application stream via `app.include_router()` inside `main.py`.
4. Add any shared integrations, connections, or parser logic into `utils.py`.

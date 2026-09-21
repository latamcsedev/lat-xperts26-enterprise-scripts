import base64
import os
import secrets

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from routers import (
    home,
    inventory,
    fmg_replacement,
    factory_reset,
    traffic_control,
    power_control,
    labstatus,
    fos80labtools,
    saselabtools,
    toolhost_reinstall,
)

_AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_auth.env")


def _load_portal_auth(path: str) -> tuple[str, str, bool]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError as exc:
        raise RuntimeError(
            f"Missing portal auth file {path} — it must be deployed with the app."
        ) from exc
    enabled = (values.get("PORTAL_AUTH") or "enabled").strip().lower() != "disabled"
    user = values.get("PORTAL_USER") or ""
    password = values.get("PORTAL_PASSWORD") or ""
    if enabled and (not user or not password):
        raise RuntimeError(
            f"portal_auth.env must set PORTAL_USER and PORTAL_PASSWORD ({path})"
        )
    return user, password, enabled


PORTAL_USER, PORTAL_PASSWORD, PORTAL_AUTH_ENABLED = _load_portal_auth(_AUTH_FILE)


def _credentials_ok(authorization: str) -> bool:
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    user_ok = secrets.compare_digest(username, PORTAL_USER)
    pass_ok = secrets.compare_digest(password, PORTAL_PASSWORD)
    return user_ok and pass_ok


app = FastAPI()


@app.middleware("http")
async def require_portal_auth(request: Request, call_next):
    # Middleware covers every path, including /docs and /openapi.json.
    # FastAPI(dependencies=...) does not apply to those built-in routes.
    if not PORTAL_AUTH_ENABLED:
        return await call_next(request)
    if not _credentials_ok(request.headers.get("Authorization") or ""):
        return JSONResponse(
            {"detail": "Not authenticated"},
            status_code=401,
            headers={"WWW-Authenticate": "Basic"},
        )
    return await call_next(request)


app.include_router(home.router)
app.include_router(inventory.router)
app.include_router(fmg_replacement.router)
app.include_router(factory_reset.router)
app.include_router(traffic_control.router)
app.include_router(power_control.router)
app.include_router(labstatus.router)
app.include_router(fos80labtools.router)
app.include_router(saselabtools.router)
app.include_router(toolhost_reinstall.router)

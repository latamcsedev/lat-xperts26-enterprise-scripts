from fastapi import FastAPI
from routers import (
    home,
    inventory,
    fmg_replacement,
    factory_reset,
    traffic_control,
    power_control,
    labstatus,
    workshopstatus,
    fos80labtools,
    saselabtools,
    toolhost_reinstall,
    debug,
)

app = FastAPI()

app.include_router(home.router)
app.include_router(inventory.router)
app.include_router(fmg_replacement.router)
app.include_router(factory_reset.router)
app.include_router(traffic_control.router)
app.include_router(power_control.router)
app.include_router(labstatus.router)
app.include_router(workshopstatus.router)
app.include_router(fos80labtools.router)
app.include_router(saselabtools.router)
app.include_router(toolhost_reinstall.router)
app.include_router(debug.router)
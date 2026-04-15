from fastapi import FastAPI
from routers import (
    home,
    inventory,
    fmg_replacement,
    factory_reset,
    traffic_control,
    power_control,
    lab_validation,
    debug,
)

app = FastAPI()

app.include_router(home.router)
app.include_router(inventory.router)
app.include_router(fmg_replacement.router)
app.include_router(factory_reset.router)
app.include_router(traffic_control.router)
app.include_router(power_control.router)
app.include_router(lab_validation.router)
app.include_router(debug.router)
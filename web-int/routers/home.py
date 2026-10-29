from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
    <head>
        <title>Automation Portal</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f4f6f9;
                padding: 40px;
            }

            h1 {
                margin-bottom: 30px;
            }

            .box {
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
                margin-bottom: 25px;
            }

            .box h2 {
                margin-top: 0;
                margin-bottom: 20px;
                font-size: 18px;
                color: #444;
            }

            .actions {
                display: flex;
                flex-wrap: wrap;
                gap: 15px;
            }

            .btn {
                display: inline-block;
                padding: 12px 18px;
                border-radius: 8px;
                border: none;
                cursor: pointer;
                font-size: 14px;
                font-weight: 500;
            }

            .primary {
                background: #1677ff;
                color: white;
            }

            .secondary {
                background: #6c757d;
                color: white;
            }

            .danger {
                background: #dc3545;
                color: white;
            }

            .btn:hover {
                opacity: 0.9;
            }
        </style>
    </head>
    <body>

    <h1>Automation Portal 1.06</h1>

    <!-- ── Labs ─────────────────────────────────────────────────────────── -->
    <div class="box">
        <h2>Labs</h2>
        <div class="actions">

            <form action="/download" method="get">
                <button class="btn primary" type="submit">
                    Inventory CSV
                </button>
            </form>

            <form action="/factoryreset" method="get">
                <button class="btn danger" type="submit">
                    Factory Reset All Sites
                </button>
            </form>

            <form action="/traffic" method="get">
                <button class="btn primary" type="submit">
                    Traffic Control
                </button>
            </form>

            <form action="/powercontrol" method="get">
                <button class="btn primary" type="submit">
                    Power Control
                </button>
            </form>

        </div>
    </div>

    <!-- ── Lab Building tools ────────────────────────────────────────────── -->
    <div class="box">
        <h2>Lab Building tools</h2>
        <div class="actions">

            <form action="/fos80labtools" method="get">
                <button class="btn primary" type="submit">
                    FortiOS 8.0 Lab Tools
                </button>
            </form>

            <form action="/saselabtools" method="get">
                <button class="btn primary" type="submit">
                    SASE Lab Tools
                </button>
            </form>

            <form action="/publicipfix" method="get">
                <button class="btn primary" type="submit">
                    Public IP Fix
                </button>
            </form>

        </div>
    </div>

    <!-- ── Management ───────────────────────────────────────────────────── -->
    <div class="box">
        <h2>Management</h2>
        <div class="actions">

            <form action="/labstatus" method="get">
                <button class="btn primary" type="submit">
                    Lab Validation
                </button>
            </form>

            <form action="/replace" method="post">
                <button class="btn secondary" type="submit">
                    FMG Serial Replacement
                </button>
            </form>

            <form action="/resetsdwan" method="get">
                <button class="btn danger" type="submit">
                    Reset SD-WAN
                </button>
            </form>

            <form action="/updateportal" method="get">
                <button class="btn danger" type="submit">
                    Update Automation Portal
                </button>
            </form>

        </div>
    </div>

    </body>
    </html>
    """

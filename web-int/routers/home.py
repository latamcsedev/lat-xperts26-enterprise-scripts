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

            .grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 25px;
            }

            .section {
                background: white;
                padding: 25px;
                border-radius: 12px;
                box-shadow: 0 3px 8px rgba(0,0,0,0.08);
            }

            .section h2 {
                margin-top: 0;
                font-size: 18px;
                color: #444;
            }

            .btn {
                display: inline-block;
                padding: 12px 18px;
                margin-top: 15px;
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

            .danger-title {
                color: #dc3545;
            }

            /* Responsive fallback */
            @media (max-width: 900px) {
                .grid {
                    grid-template-columns: 1fr;
                }
            }
        </style>
    </head>
    <body>

    <h1>Automation Portal 1.04</h1>

    <div class="grid">

        <!-- Inventory CSV -->
        <div class="section">
            <h2>Inventory</h2>
            <form action="/download" method="get">
                <button class="btn primary" type="submit">
                    Download Inventory CSV
                </button>
            </form>
        </div>

        <!-- Factory Reset -->
        <div class="section">
            <h2 class="danger-title">Factory Reset</h2>
            <form action="/factoryreset" method="get">
                <button class="btn danger" type="submit">
                    Factory Reset All Sites
                </button>
            </form>
        </div>

        <!-- Traffic Control -->
        <div class="section">
            <h2>Traffic Control</h2>
            <form action="/traffic" method="get">
                <button class="btn primary" type="submit">
                    Traffic Control Dashboard
                </button>
            </form>
        </div>

        <!-- FMG Replacement -->
        <div class="section">
            <h2>FMG Serial Replacement</h2>
            <form action="/replace" method="post">
                <button class="btn secondary" type="submit">
                    Replace Serials on FortiManager
                </button>
            </form>
        </div>

        <!-- Power Control -->
        <div class="section">
            <h2>Power Control</h2>
            <form action="/powercontrol" method="get">
                <button class="btn primary" type="submit">
                    Power Control Dashboard
                </button>
            </form>
        </div>

        <!-- Lab Validation -->
        <div class="section">
            <h2>Lab Validation</h2>
            <form action="/labstatus" method="get">
                <button class="btn primary" type="submit">
                    Lab Validation Dashboard
                </button>
            </form>
        </div>

        <!-- FortiOS 8.0 Lab Tools -->
        <div class="section">
            <h2>FortiOS 8.0 Lab Tools</h2>
            <form action="/fos80labtools" method="get">
                <button class="btn primary" type="submit">
                    FortiOS 8.0 Lab Tools
                </button>
            </form>
        </div>

        <!-- SASE Lab Tools -->
        <div class="section">
            <h2>SASE Lab Tools</h2>
            <form action="/saselabtools" method="get">
                <button class="btn primary" type="submit">
                    SASE Lab Tools
                </button>
            </form>
        </div>

    </div>

    </body>
    </html>
    """

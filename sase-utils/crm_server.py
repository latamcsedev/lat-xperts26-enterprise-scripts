import http.server
import socketserver
import urllib.request
import json
import random
from datetime import datetime

PORT = 8090
SERVER_NAME = "Xperts 26 CRM Server"

def get_random_leads():
    """Fetches real dummy data to simulate new incoming CRM leads."""
    try:
        url = "[https://randomuser.me/api/?results=3&inc=name,location,email](https://randomuser.me/api/?results=3&inc=name,location,email)"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            return data['results']
    except:
        return []

class CRMHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        leads = get_random_leads()
        leads_html = ""
        for lead in leads:
            leads_html += f"""
            <div class="lead-card">
                <div class="lead-info">
                    <strong>{lead['name']['first']} {lead['name']['last']}</strong><br>
                    <small>{lead['email']}</small>
                </div>
                <div class="status-badge">New Lead</div>
            </div>
            """

        # Simulated dynamic "Attention Grabbers"
        deal_value = random.randint(15000, 50000)
        target_pct = random.randint(65, 98)

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{SERVER_NAME}</title>
            <meta http-equiv="refresh" content="15">
            <style>
                :root {{ --neon-blue: #00d4ff; --neon-purple: #9d50bb; --bg: #05070a; }}
                body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: #fff; margin: 0; padding: 20px; }}
                .dashboard {{ max-width: 900px; margin: auto; display: grid; grid-template-columns: 1fr 1.5fr; gap: 20px; }}
                .box {{ background: #10141d; border: 1px solid #1f2937; padding: 20px; border-radius: 12px; position: relative; overflow: hidden; }}
                .box::before {{ content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%; background: var(--neon-blue); }}

                h1 {{ color: var(--neon-blue); text-transform: uppercase; letter-spacing: 2px; font-size: 1.2rem; margin-bottom: 25px; }}
                .stat-value {{ font-size: 2.5rem; font-weight: bold; color: #fff; text-shadow: 0 0 10px rgba(0,212,255,0.3); }}

                .progress-container {{ background: #1f2937; height: 10px; border-radius: 5px; margin: 15px 0; }}
                .progress-bar {{ background: linear-gradient(90deg, var(--neon-blue), var(--neon-purple)); height: 100%; border-radius: 5px; width: {target_pct}%; transition: 1s; }}

                .lead-card {{ background: #1a1f2e; margin-bottom: 10px; padding: 12px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; border-left: 3px solid var(--neon-purple); }}
                .status-badge {{ background: rgba(157, 80, 187, 0.2); color: #d4a5ff; padding: 4px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; }}

                .pulse-container {{ display: flex; align-items: center; gap: 10px; color: #4ade80; font-size: 0.8rem; font-weight: bold; }}
                .pulse {{ width: 10px; height: 10px; background: #4ade80; border-radius: 50%; box-shadow: 0 0 10px #4ade80; animation: pulse 1.5s infinite; }}
                @keyframes pulse {{ 0% {{ transform: scale(1); opacity: 1; }} 50% {{ transform: scale(1.5); opacity: 0.5; }} 100% {{ transform: scale(1); opacity: 1; }} }}
            </style>
        </head>
        <body>
            <div style="text-align:center; margin-bottom: 30px;">
                <h2 style="color: #6366f1;">{SERVER_NAME}</h2>
                <div class="pulse-container" style="justify-content:center;">
                    <div class="pulse"></div> ENGINE ACTIVE - LIVE SYNC
                </div>
            </div>

            <div class="dashboard">
                <div class="box">
                    <h1>Monthly Revenue</h1>
                    <div class="stat-value">${deal_value:,}</div>
                    <p style="color: #94a3b8;">Current Sales Pipeline</p>
                    <div class="progress-container"><div class="progress-bar"></div></div>
                    <small>Target: {target_pct}% achieved</small>
                </div>

                <div class="box" style="border-color: var(--neon-purple);">
                    <h1>Recent Leads</h1>
                    {leads_html}
                    <div style="margin-top: 15px; font-size: 0.8rem; color: #475569;">
                        Last updated: {datetime.now().strftime('%H:%M:%S')}
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

with socketserver.TCPServer(("0.0.0.0", PORT), CRMHandler) as httpd:
    print(f"CRM Server deployed on port {PORT}")
    httpd.serve_forever()
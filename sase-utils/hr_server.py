import http.server
import socketserver
import json
import random

PORT = 8080
SERVER_NAME = "Xpert26 HR Dashboard"

# Sample HR Metrics
def get_hr_metrics():
    """Generates mock real-time HR data for the dashboard."""
    # In a real scenario, this would fetch from an API like Workday, BambooHR, or a database.
    return [
        {"label": "Total Headcount", "value": "1,248", "sub": "+12 this month", "class": "up"},
        {"label": "Open Positions", "value": "42", "sub": "8 urgent", "class": "down"},
        {"label": "Pending Leave", "value": "15", "sub": "Requires approval", "class": "neutral"},
        {"label": "Employee Pulse", "value": "8.4", "sub": "Avg. Satisfaction", "class": "up"}
    ]

class XpertHRHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        # Build the HR metric rows
        metrics_html = ""
        for m in get_hr_metrics():
            metrics_html += f"""
            <div class="stat-row">
                <span class="label">{m['label']}</span>
                <span class="value">{m['value']} <small class="{m['class']}">{m['sub']}</small></span>
            </div>
            """

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{SERVER_NAME}</title>
            <meta http-equiv="refresh" content="30">
            <style>
                body {{ font-family: 'Inter', system-ui, sans-serif; background: #0f172a; color: #f1f5f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
                .container {{ background: #1e293b; padding: 2.5rem; border-radius: 1.5rem; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); width: 440px; border: 1px solid #334155; }}
                h1 {{ color: #818cf8; margin-top: 0; font-size: 1.75rem; letter-spacing: -0.025em; border-bottom: 2px solid #334155; padding-bottom: 1.2rem; margin-bottom: 1rem; }}
                .stat-row {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 0; border-bottom: 1px solid #334155; }}
                .stat-row:last-of-type {{ border-bottom: none; }}
                .label {{ font-weight: 600; color: #94a3b8; font-size: 1rem; }}
                .value {{ font-family: 'Inter', sans-serif; font-size: 1.2rem; font-weight: 700; text-align: right; }}
                .value small {{ display: block; font-size: 0.75rem; font-weight: 400; margin-top: 2px; }}
                .up {{ color: #34d399; }} /* Positive growth */
                .down {{ color: #fb7185; }} /* Needs attention */
                .neutral {{ color: #fbbf24; }} /* Pending */
                .footer {{ font-size: 0.8rem; color: #64748b; margin-top: 2rem; text-align: center; }}
                .pulse {{ width: 8px; height: 8px; background: #818cf8; border-radius: 50%; display: inline-block; margin-right: 5px; animation: blink 2s infinite; }}
                @keyframes blink {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} 100% {{ opacity: 1; }} }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>{SERVER_NAME}</h1>
                {metrics_html}
                <div class="footer">
                    <span class="pulse"></span> Internal HR Portal • Auto-refresh active
                </div>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))

with socketserver.TCPServer(("0.0.0.0", PORT), XpertHRHandler) as httpd:
    print(f"{SERVER_NAME} is LIVE at http://localhost:{PORT}")
    httpd.serve_forever()
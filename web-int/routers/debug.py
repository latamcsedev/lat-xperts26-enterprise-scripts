import os
import requests
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/debug/auth", response_class=HTMLResponse)
def debug_auth():
    import traceback
    host = os.getenv("FABRIC_HOST", "NOT_SET")
    cred = os.getenv("CREDENTIAL", "NOT_SET")
    
    masked_cred = cred[:4] + "***" + cred[-4:] if len(cred) > 8 else "***"
    
    try:
        # attempt to obtain token
        r = requests.post(
            f"https://{host}/oauth2/token/",
            headers={
                "Authorization": f"Basic {cred}",
                "Cache-Control": "no-cache",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            verify=False,
        )
        r.raise_for_status()
        token = r.json().get("access_token", "No token field")
        status_msg = f"Success! Token received: {token[:10]}..."
    except Exception as e:
        status_msg = f"Failed to get token: {str(e)}\n\nResponse Text (if applicable):\n{getattr(e, 'response', None) and getattr(e.response, 'text', '')}\n\nTraceback:\n{traceback.format_exc()}"
        
    return f"""
    <html>
    <body>
        <h2>Auth Debug</h2>
        <p><b>FABRIC_HOST:</b> {host}</p>
        <p><b>CREDENTIAL (masked):</b> {masked_cred}</p>
        <h3>Token Status:</h3>
        <pre>{status_msg}</pre>
        <br>
        <a href="/">Back to Home</a>
    </body>
    </html>
    """

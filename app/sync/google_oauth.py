"""Google Sign-In for the desktop client via the OAuth 2.0 loopback flow.

Standard "installed app" pattern: open the system browser at Google's consent
screen and receive the authorization code on a short-lived local HTTP server.

The authorization code is then handed to our own backend (`POST /auth/google`),
which performs the code->token exchange with Google (it holds the OAuth
*client secret*), verifies the resulting Google ID token and issues our own
access/refresh tokens. Google is only ever used to prove "this is
<email>@gmail.com", nothing else in the app talks to Google again after that.

Security note: the desktop client deliberately does **not** contain the OAuth
client secret. Only the client *id* is embedded here — it is not confidential
(it is visible in the browser URL during consent) — and the flow is bound with
PKCE so the intercepted code is useless without the verifier. The client secret
lives exclusively in the backend's environment and is never shipped in a
release binary.

Must be called off the Tk main thread (it blocks waiting for the browser).
"""

import base64
import hashlib
import http.server
import os
import secrets
import urllib.parse
import webbrowser

# Public "Desktop app" OAuth client id. Not a secret. Overridable for
# self-hosted deployments via the KQNOTE_GOOGLE_CLIENT_ID environment variable.
_DEFAULT_CLIENT_ID = (
    "379908990291-36934dgf21e8u90qj8f5ldnoij76ij1k.apps.googleusercontent.com"
)
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_SCOPE = "openid email profile"

_LOGO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "..", "assets", "logo-kqnote.png"
)


def _client_id():
    return os.environ.get("KQNOTE_GOOGLE_CLIENT_ID", "").strip() or _DEFAULT_CLIENT_ID


def _logo_data_uri():
    try:
        with open(_LOGO_PATH, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except OSError:
        return ""


_PAGE_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>KQ Note</title>
<style>
  :root {{
    --bg: #0f0f11; --card: #18181b; --border: #2c2c30;
    --text: #e7e7ea; --muted: #9a9aa2; --accent: #5b9df0;
    --ok: #4fd1a5; --err: #e0707a;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg: #f2f3f5; --card: #ffffff; --border: #e2e4e8;
             --text: #1c1c1f; --muted: #6b6b74; }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", -apple-system, sans-serif;
    display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    width: min(90vw, 380px); background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 36px 32px; text-align: center;
    box-shadow: 0 20px 50px rgba(0,0,0,0.25);
  }}
  .logo {{ width: 40px; height: 40px; border-radius: 10px; margin-bottom: 18px; }}
  .badge {{
    width: 52px; height: 52px; border-radius: 50%; margin: 0 auto 18px;
    display: flex; align-items: center; justify-content: center;
    background: {badge_bg};
  }}
  .badge svg {{ width: 26px; height: 26px; }}
  h1 {{ font-size: 17px; margin: 0 0 8px; font-weight: 600; }}
  p {{ font-size: 13px; color: var(--muted); margin: 0; line-height: 1.6; }}
  .brand {{ margin-top: 24px; font-size: 11px; color: var(--muted); letter-spacing: .04em; }}
</style>
</head>
<body>
  <div class="card">
    <img class="logo" src="{logo}" alt="KQ Note">
    <div class="badge">{icon}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <div class="brand">KQ NOTE</div>
  </div>
  <script>
    // Best-effort: only works if the browser permits scripts to close a tab
    // it didn't itself open, so this silently no-ops in most browsers.
    setTimeout(function () {{ try {{ window.close(); }} catch (e) {{}} }}, 2500);
  </script>
</body>
</html>"""

_CHECK_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="3" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M5 13l4 4L19 7"/></svg>'
)
_ERROR_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="3" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M6 6l12 12M18 6L6 18"/></svg>'
)


def _render_page(title, message, ok=True):
    html = _PAGE_TEMPLATE.format(
        logo=_logo_data_uri(),
        icon=_CHECK_ICON if ok else _ERROR_ICON,
        badge_bg="var(--ok)" if ok else "var(--err)",
        title=title,
        message=message,
    )
    return html.encode("utf-8")


SUCCESS_PAGE = _render_page(
    "Đăng nhập thành công",
    "Bạn có thể đóng tab này và quay lại KQ Note.",
    ok=True,
)


class GoogleLoginError(Exception):
    pass


def _make_pkce_pair():
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        error = params.get("error", [None])[0]
        _CallbackHandler.result = {
            "code": params.get("code", [None])[0],
            "state": params.get("state", [None])[0],
            "error": error,
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if error:
            page = _render_page(
                "Đăng nhập không thành công",
                "Bạn đã huỷ đăng nhập hoặc Google từ chối yêu cầu. Quay lại KQ Note để thử lại.",
                ok=False,
            )
        else:
            page = SUCCESS_PAGE
        self.wfile.write(page)

    def log_message(self, format, *args):
        pass  # silence default request logging to stderr


def run_oauth_flow(timeout=180):
    """Blocks until the user finishes the Google consent flow.

    Returns a dict ``{"code", "code_verifier", "redirect_uri"}`` to be exchanged
    for tokens by the backend. Raises :class:`GoogleLoginError` on any failure.
    """
    client_id = _client_id()

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://localhost:{server.server_port}/"

    state = secrets.token_urlsafe(24)
    verifier, challenge = _make_pkce_pair()

    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    })
    try:
        opened = webbrowser.open(f"{_AUTH_URI}?{query}")
    except webbrowser.Error:
        opened = False
    if not opened:
        server.server_close()
        raise GoogleLoginError(
            "Không mở được trình duyệt để đăng nhập Google. "
            "Hãy đặt một trình duyệt mặc định rồi thử lại."
        )

    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()

    result = _CallbackHandler.result
    if result.get("error"):
        raise GoogleLoginError(f"Google từ chối đăng nhập: {result['error']}")
    if not result.get("code"):
        raise GoogleLoginError("Hết thời gian chờ đăng nhập Google")
    if result.get("state") != state:
        raise GoogleLoginError("Phản hồi OAuth không hợp lệ (state không khớp)")

    return {
        "code": result["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }

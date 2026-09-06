"""
Minimal web server (Flask) for Raspberry Pi #2: displays the project's HTML
pages (network/code report, plus any other page dropped into pages/ later)
on the local network, no build step required.

Pages are protected by a login (username + password): see README.md for
setting up the credentials in .env.

Run with:
    pip install -r requirements.txt
    python3 app.py

Then, from any device on the same WiFi:
    http://<PI_2_IP>:5000/

Commands typed into the /control console are relayed to the robot's TCP
control server (Raspberry Pi #1, link/server.py in the robot repo) over
plain sockets -- see robot_link.py and pages/protocole_controle.html for
the NMEA-style sentence format.
"""
import functools
import os
import secrets

import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

from robot_link import send_command

load_dotenv()

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".ogg", ".mov")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# Where the robot's control server (Pi #1, link/server.py) listens.
ROBOT_HOST = os.environ.get("ROBOT_HOST", "192.168.1.180")
ROBOT_PORT = int(os.environ.get("ROBOT_PORT", "5050"))

# Where the robot's live camera stream (Pi #1, camera/stream_server.py)
# listens. Same host as ROBOT_HOST -- only the port and path differ.
CAMERA_PORT = int(os.environ.get("CAMERA_PORT", "8000"))
CAMERA_STREAM_PATH = os.environ.get("CAMERA_STREAM_PATH", "/stream.mjpg")
CAMERA_TIMEOUT = 3  # seconds -- how long to wait before giving up on the robot's camera

# Credentials expected in .env (see .env.example): WEBSERVER_USERNAME and
# WEBSERVER_PASSWORD_HASH (hash generated with generate_password.py, never
# the plaintext password). FLASK_SECRET_KEY signs the session cookie; if
# missing, a random key is generated at startup -- fine for quick testing,
# but every server restart then logs everyone out.
WEBSERVER_USERNAME = os.environ.get("WEBSERVER_USERNAME", "")
WEBSERVER_PASSWORD_HASH = os.environ.get("WEBSERVER_PASSWORD_HASH", "")

# Optional second, read-only account (e.g. for sharing the live status page
# with someone over the internet without letting them drive the robot).
# A viewer session can load /control and watch the status bar/telemetry,
# but the Controls buttons and console command input are hidden client-side
# AND rejected server-side in /api/send (except the harmless, argument-less
# STA query, needed to keep the status bar working) -- see api_send() below.
# Leave these two variables empty in .env to disable the viewer account
# entirely (default).
WEBSERVER_VIEWER_USERNAME = os.environ.get("WEBSERVER_VIEWER_USERNAME", "")
WEBSERVER_VIEWER_PASSWORD_HASH = os.environ.get("WEBSERVER_VIEWER_PASSWORD_HASH", "")

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Commands a viewer/guest session is allowed to send via /api/send. STA has
# no fields and only reads telemetry back, so it can't affect the robot --
# every other sentence type (STP, DRV, NAV, MOD, PID, CAM...) is refused
# for that role regardless of what the client sends.
VIEWER_ALLOWED_COMMANDS = {"STA"}


def _command_type(command):
    """Returns the sentence type (first comma-separated token, upper-cased)
    of a raw console command, e.g. "drv,120,120" -> "DRV"."""
    return command.split(",", 1)[0].strip().upper()

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


def login_required(view):
    """Decorator: redirects to /login if the visitor hasn't authenticated yet."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _list_media(directory, extensions):
    """Returns the sorted list of filenames in `directory` matching
    `extensions`, or an empty list if the directory doesn't exist yet."""
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.lower().endswith(extensions))


def _page(title, body):
    """Small shared page shell, same look as the rest of the site."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 80px auto; padding: 0 20px; color: #1a1a1a; }}
    h1 {{ font-size: 20px; margin-bottom: 24px; }}
    label {{ display: block; font-size: 13px; margin: 14px 0 4px; color: #444; }}
    input {{ width: 100%; box-sizing: border-box; padding: 9px 10px; font-size: 15px; border: 1px solid #ccc; border-radius: 6px; }}
    button {{ margin-top: 20px; padding: 9px 16px; font-size: 15px; border: none; border-radius: 6px; background: #1a56db; color: white; cursor: pointer; }}
    button:hover {{ background: #1544ab; }}
    .error {{ background: #fdecea; color: #a12622; padding: 10px 12px; border-radius: 6px; font-size: 14px; margin-top: 16px; }}
    li {{ margin: 8px 0; }}
    a {{ font-size: 16px; }}
    .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .logout {{ font-size: 13px; color: #666; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def _login_page(next_url, error=None):
    """Login page: dark theme, live clock (client-side), deliberately
    distinct from the rest of the site -- it's the entry screen."""
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #0d1117;
      color: #e6edf3;
      font-family: system-ui, sans-serif;
    }}

    .card {{
      width: 100%;
      max-width: 360px;
      padding: 32px 28px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    }}

    .clock {{
      font-family: "Courier New", monospace;
      font-size: 13px;
      color: #8b949e;
      text-align: right;
      margin-bottom: 8px;
      letter-spacing: 0.03em;
    }}

    h1 {{ font-size: 22px; margin: 0 0 24px; font-weight: 600; }}
    label {{ display: block; font-size: 13px; margin: 14px 0 4px; color: #8b949e; }}

    input {{
      width: 100%;
      padding: 9px 10px;
      font-size: 15px;
      border: 1px solid #30363d;
      border-radius: 6px;
      background: #0d1117;
      color: #e6edf3;
    }}
    input:focus {{ outline: none; border-color: #58a6ff; }}

    button {{
      width: 100%;
      margin-top: 20px;
      padding: 10px 16px;
      font-size: 15px;
      border: none;
      border-radius: 6px;
      background: #1f6feb;
      color: white;
      cursor: pointer;
    }}
    button:hover {{ background: #388bfd; }}

    .error {{
      background: #2d1214;
      color: #ff7b72;
      border: 1px solid #6e2426;
      padding: 10px 12px;
      border-radius: 6px;
      font-size: 14px;
      margin-top: 16px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="clock" id="clock">--:--:--</div>
    <h1>Welcome</h1>

    <form method="post" action="{url_for('login')}">
      <input type="hidden" name="next" value="{next_url}">

      <label for="username">Username</label>
      <input type="text" id="username" name="username" autocomplete="username" required autofocus>

      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required>

      <button type="submit">Log in</button>

      {error_html}
    </form>
  </div>

  <script>
    function updateClock() {{
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, "0");
      const mm = String(now.getMinutes()).padStart(2, "0");
      const ss = String(now.getSeconds()).padStart(2, "0");
      document.getElementById("clock").textContent = `${{hh}}:${{mm}}:${{ss}}`;
    }}
    updateClock();
    setInterval(updateClock, 1000);
  </script>
</body>
</html>"""


def _control_page(videos=None, images=None, role="admin"):
    """Robot control page (landing page after login): clock fixed top-right,
    center split into three panels flowing left to right (Controls -- still
    a placeholder / Video feed / Images), bottom third is a terminal-style
    console. The rightmost panel (Images) is shortened with a top margin so
    it doesn't visually collide with the clock above it. No backend wired in
    yet -- the robot communication protocol is still to be defined (see the
    report's "A suivre" section), so the console just echoes what you type.

    videos: filenames in media/videos/, played one at a time in a loop --
    a single <video> element whose source is advanced to the next file
    (client-side JS) when the current one ends, wrapping back to the first
    after the last. Muted by default, native controls let sound be turned
    on manually.
    images: filenames in media/images/, cycled automatically every 3s as a
    slideshow (client-side JS).
    role: "admin" (full access) or "viewer" (read-only guest account, see
    WEBSERVER_VIEWER_USERNAME above) -- a viewer never sees the Controls
    buttons or gets a usable console input, so there is no client-side way
    to type or trigger a driving command; /api/send enforces the same
    restriction server-side regardless, so this is a UX nicety, not the
    actual security boundary.

    Video feed panel: the client-side JS also tries to load the robot's
    live camera feed (proxied from Pi #1 through /media/camera, see that
    route below) into a hidden <img>. As soon as it loads, it's shown in
    place of the recorded video playlist/placeholder; if it errors out
    (robot's camera script not running, wrong IP...) the recorded
    playlist/placeholder is shown instead, and the camera is retried every
    few seconds in the background -- so the panel switches over
    automatically whenever the live feed becomes available, no reload
    needed."""
    videos = videos or []
    images = images or []
    is_viewer = role == "viewer"

    if is_viewer:
        controls_html = (
            '<div class="placeholder">Read-only access<br>'
            "(controls disabled for this account)</div>"
        )
    else:
        controls_html = '<div class="controls-buttons" id="controlsButtons"></div>'

    if is_viewer:
        console_input_html = """
      <span class="prompt">&gt;</span>
      <input id="consoleInput" type="text" placeholder="Read-only account -- command input disabled" disabled>
"""
    else:
        console_input_html = """
      <span class="prompt">&gt;</span>
      <input id="consoleInput" type="text" placeholder="Type a command and press Enter" autocomplete="off">
"""

    if videos:
        video_urls_js = ", ".join(f'"{url_for("media_video", filename=v)}"' for v in videos)
        video_html = '<video class="video-player" id="videoPlayer" muted controls playsinline></video>'
    else:
        video_urls_js = ""
        video_html = '<div class="placeholder">Live video coming soon<br>(drop files into media/videos/)</div>'

    if images:
        image_urls_js = ", ".join(f'"{url_for("media_image", filename=i)}"' for i in images)
        images_html = '<img class="slideshow-img" id="slideshowImg" alt="Robot snapshot">'
    else:
        image_urls_js = ""
        images_html = '<div class="placeholder">Snapshots coming soon<br>(drop files into media/images/)</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Robot Control</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; margin: 0; }}
    body {{
      display: flex;
      flex-direction: column;
      background: #0d1117;
      color: #e6edf3;
      font-family: system-ui, sans-serif;
      overflow: hidden;
    }}

    /* Top-left: quick links to the rest of the site. */
    .nav {{
      position: fixed;
      top: 16px;
      left: 20px;
      display: flex;
      gap: 14px;
      font-size: 13px;
      z-index: 10;
    }}
    .nav a {{ color: #8b949e; text-decoration: none; }}
    .nav a:hover {{ color: #e6edf3; }}
    .viewer-badge {{
      color: #d29922;
      border: 1px solid #6e5b1f;
      background: #3a2f0f;
      padding: 1px 8px;
      border-radius: 10px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}

    /* Clock, fixed top-right corner, always visible. */
    .clock {{
      position: fixed;
      top: 16px;
      right: 20px;
      font-family: "Courier New", monospace;
      font-size: 14px;
      color: #8b949e;
      background: #161b22;
      border: 1px solid #30363d;
      padding: 6px 12px;
      border-radius: 6px;
      z-index: 10;
      letter-spacing: 0.05em;
    }}

    /* Status bar: below the fixed nav links, above the panels. Not fixed
       itself (normal flow), so it needs a top margin to clear the fixed
       .nav/.clock rows above it. Polled from the robot's STA telemetry
       every few seconds (see script below). */
    .status-bar {{
      flex: 0 0 auto;
      margin-top: 56px;
      padding: 10px 24px;
      border-bottom: 1px solid #30363d;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      flex-wrap: wrap;
    }}
    .status-group {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 150px;
    }}
    .status-group.status-group-right {{ align-items: flex-end; text-align: right; }}
    .status-group.status-group-center {{
      flex-direction: row;
      gap: 28px;
      flex: 1;
      justify-content: center;
      min-width: 220px;
    }}
    .status-metric {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2px;
      min-width: 64px;
    }}
    .status-label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: #8b949e;
    }}
    .status-value {{
      font-family: "Courier New", monospace;
      font-size: 13.5px;
      color: #e6edf3;
      white-space: nowrap;
    }}

    /* Main area: top ~two thirds, panels flow left to right. */
    .main-area {{
      flex: 2;
      display: flex;
      flex-direction: row;
      background: #0d1117;
      overflow: hidden;
    }}

    .panel {{
      flex: 1;
      background: #0d1117;
      border-left: 1px solid #30363d;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .panel:first-child {{ border-left: none; }}

    /* Rightmost panel (Images): shortened so it clears the clock above it. */
    .panel.panel-right {{ margin-top: 64px; }}

    .panel h2 {{
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #8b949e;
      margin: 0 0 10px;
    }}

    .panel .placeholder {{
      border: 1px dashed #30363d;
      border-radius: 8px;
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
      color: #484f58;
      text-align: center;
      padding: 12px;
      line-height: 1.6;
    }}

    /* Video panel: one video at a time, auto-advances through the whole
       set on a loop (see script below). Muted by default, native controls
       let sound be turned on manually. */
    .video-player {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      background: #000;
    }}

    /* Video feed panel wrapper: holds either the live camera image or the
       recorded video/placeholder, never both at once (see script below). */
    .video-feed-area {{
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    /* Live camera feed, proxied from the robot through /media/camera.
       Hidden by default -- shown only once it actually loads. */
    .camera-stream {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      object-fit: contain;
    }}
    .camera-stream[hidden] {{ display: none; }}

    /* Images panel: single <img>, source swapped every 3s by JS below. */
    .slideshow-img {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      object-fit: contain;
    }}

    /* Console: bottom third of the window. min-height: 0 overrides the
       flex default (min-height: auto), which otherwise lets this box grow
       past its flex:1 share to fit however many lines consoleLog/tcpLog
       accumulate -- without it the whole console panel silently got
       taller (and scrolled off screen) as commands piled up instead of
       staying put and scrolling internally. */
    .console {{
      flex: 1;
      min-height: 0;
      background: #010409;
      border-top: 1px solid #30363d;
      display: flex;
      flex-direction: column;
      font-family: "Courier New", monospace;
      font-size: 13px;
    }}

    .console-header {{
      padding: 6px 14px;
      color: #8b949e;
      border-bottom: 1px solid #30363d;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      display: flex;
      align-items: center;
    }}

    /* Console tabs: "Console" (user-typed commands only, unchanged
       behaviour) vs "TCP (all)" (every request/response this page sends to
       the robot, including the background status-bar polling that never
       appears in the Console tab). Only one of #consoleLog/#tcpLog is
       shown at a time -- see showConsoleTab() below. */
    .console-tabs {{ display: flex; gap: 4px; }}
    .console-tab {{
      background: transparent;
      border: none;
      color: #8b949e;
      font-family: inherit;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 3px 8px;
      border-radius: 4px;
      cursor: pointer;
    }}
    .console-tab:hover {{ color: #e6edf3; }}
    .console-tab.active {{ color: #e6edf3; background: #21262d; }}

    .console-log {{
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}
    .console-log[hidden] {{ display: none; }}

    /* TCP tab: each request/response pair is grouped into one .tcp-frame
       so old ones can be pruned as a unit (max MAX_TCP_FRAMES in the
       script below, oldest dropped first) -- the tab stays a fixed size
       instead of growing without bound. */
    .tcp-frame {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding-bottom: 6px;
      margin-bottom: 6px;
      border-bottom: 1px solid #21262d;
    }}
    .tcp-frame:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}

    .console-log .line {{ white-space: pre-wrap; }}
    .console-log .ts  {{ color: #6e7681; margin-right: 8px; }}
    .console-log .sys {{ color: #58a6ff; }}
    .console-log .ok  {{ color: #3fb950; }}
    .console-log .err {{ color: #ff7b72; }}
    .console-log .cmd {{ color: #e6edf3; }}

    /* Controls panel: one full-width button per predefined NMEA sentence
       (see pages/protocole_controle.html for the full reference) -- a
       click doesn't send anything by itself, it just fills the console
       input below with that sentence's beginning so the fields can be
       completed before pressing Enter. */
    .controls-buttons {{
      width: 100%;
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
      /* flex:1 + min-height:0 make this box's height explicitly "whatever
         is left in .panel below the Controls <h2>", rather than relying on
         the (real but easy-to-break) flexbox rule that an item with a
         non-visible overflow gets an automatic min-height of 0 instead of
         its content size. Either way this box was already scrolling once
         "GPS route" became the 8th button and pushed the list past that
         space -- verified with Playwright at several window sizes -- but
         the scroll was easy to miss with no visible scrollbar (a native
         overlay scrollbar, invisible until hovered/dragged on macOS and
         very thin on Windows/Linux). The rules below force a slim but
         always-visible scrollbar so a cut-off button is never silently
         hidden again, on any platform. */
      flex: 1 1 auto;
      min-height: 0;
      scrollbar-width: thin;              /* Firefox */
      scrollbar-color: #30363d #0d1117;   /* Firefox: thumb, track */
    }}
    .controls-buttons::-webkit-scrollbar {{ width: 8px; }}
    .controls-buttons::-webkit-scrollbar-track {{ background: #0d1117; }}
    .controls-buttons::-webkit-scrollbar-thumb {{
      background: #30363d;
      border-radius: 4px;
    }}
    .controls-buttons::-webkit-scrollbar-thumb:hover {{ background: #58a6ff; }}
    .cmd-button {{
      width: 100%;
      padding: 10px 12px;
      background: #161b22;
      color: #e6edf3;
      border: 1px solid #30363d;
      border-radius: 6px;
      font-family: "Courier New", monospace;
      font-size: 13px;
      text-align: left;
      cursor: pointer;
    }}
    .cmd-button:hover {{ background: #21262d; border-color: #58a6ff; }}
    .cmd-button:active {{ background: #1c2129; }}

    .console-input {{
      display: flex;
      align-items: center;
      border-top: 1px solid #30363d;
      padding: 8px 14px;
      gap: 8px;
    }}
    .console-input .prompt {{ color: #3fb950; }}
    .console-input input {{
      flex: 1;
      background: transparent;
      border: none;
      outline: none;
      color: #e6edf3;
      font-family: inherit;
      font-size: 13px;
    }}
  </style>
</head>
<body>

  <div class="nav">
    <a href="{url_for('pages_index')}">Pages</a>
    <a href="{url_for('logout')}">Log out</a>
    {'<span class="viewer-badge">Read-only access</span>' if is_viewer else ''}
  </div>
  <div class="clock" id="clock">--:--:--</div>

  <div class="status-bar">
    <div class="status-group">
      <span class="status-label">Current position</span>
      <span class="status-value" id="statusCurrentPos">--</span>
    </div>
    <div class="status-group status-group-center">
      <div class="status-metric">
        <span class="status-label">Distance</span>
        <span class="status-value" id="statusDistance">--</span>
      </div>
      <div class="status-metric">
        <span class="status-label">Heading</span>
        <span class="status-value" id="statusBearing">--</span>
      </div>
      <div class="status-metric">
        <span class="status-label">Speed</span>
        <span class="status-value" id="statusSpeed">--</span>
      </div>
      <div class="status-metric">
        <span class="status-label">Motor L</span>
        <span class="status-value" id="statusMotorLeft">--</span>
      </div>
      <div class="status-metric">
        <span class="status-label">Motor R</span>
        <span class="status-value" id="statusMotorRight">--</span>
      </div>
    </div>
    <div class="status-group status-group-right">
      <span class="status-label">Target position</span>
      <span class="status-value" id="statusTargetPos">--</span>
    </div>
  </div>

  <div class="main-area">
    <div class="panel">
      <h2>Controls</h2>
      {controls_html}
    </div>
    <div class="panel">
      <h2>Video feed</h2>
      <div class="video-feed-area">
        <img class="camera-stream" id="cameraStream" alt="Live camera feed" hidden>
        <div id="recordedVideoArea">{video_html}</div>
      </div>
    </div>
    <div class="panel panel-right">
      <h2>Images</h2>
      {images_html}
    </div>
  </div>

  <div class="console">
    <div class="console-header">
      <div class="console-tabs">
        <button type="button" class="console-tab active" id="tabConsoleBtn">Console</button>
        <button type="button" class="console-tab" id="tabTcpBtn">TCP (all)</button>
      </div>
    </div>
    <div class="console-log" id="consoleLog">
      <div class="line"><span class="ts">00:00:00</span><span class="sys">system: waiting for connection...</span></div>
    </div>
    <div class="console-log" id="tcpLog" hidden>
      <div class="line"><span class="ts">00:00:00</span><span class="sys">system: raw TCP traffic to the robot appears here, including background polling hidden from the Console tab...</span></div>
    </div>
    <div class="console-input">{console_input_html}</div>
  </div>

  <script>
    function pad(n) {{ return String(n).padStart(2, "0"); }}

    function updateClock() {{
      const now = new Date();
      document.getElementById("clock").textContent =
        `${{pad(now.getHours())}}:${{pad(now.getMinutes())}}:${{pad(now.getSeconds())}}`;
    }}
    updateClock();
    setInterval(updateClock, 1000);

    function timestamp() {{
      const now = new Date();
      return `${{pad(now.getHours())}}:${{pad(now.getMinutes())}}:${{pad(now.getSeconds())}}`;
    }}

    // logLine() writes one line to the visible Console tab (user-typed
    // commands only). tcpLogFrame() writes to the TCP tab, which sees
    // every request this page sends the robot, visible command or not --
    // each call groups its lines (request + response) into one "frame"
    // div so old frames can be pruned as a unit (see MAX_TCP_FRAMES
    // below): the TCP tab must never grow past a handful of exchanges,
    // oldest dropped first, so it stays a fixed size instead of scrolling
    // forever.
    function logLine(text, cls) {{
      const log = document.getElementById("consoleLog");
      const div = document.createElement("div");
      div.className = "line";
      div.innerHTML = `<span class="ts">${{timestamp()}}</span><span class="${{cls}}">${{text}}</span>`;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }}

    const MAX_TCP_FRAMES = 10;
    function tcpLogFrame(entries) {{
      const log = document.getElementById("tcpLog");
      const frame = document.createElement("div");
      frame.className = "tcp-frame";
      for (const {{text, cls}} of entries) {{
        const div = document.createElement("div");
        div.className = "line";
        div.innerHTML = `<span class="ts">${{timestamp()}}</span><span class="${{cls}}">${{text}}</span>`;
        frame.appendChild(div);
      }}
      log.appendChild(frame);
      // Drop the oldest frame(s) first (document order) until at most
      // MAX_TCP_FRAMES remain -- the initial "system: ..." line isn't a
      // .tcp-frame, so it's never counted or removed here.
      while (log.querySelectorAll(".tcp-frame").length > MAX_TCP_FRAMES) {{
        log.querySelector(".tcp-frame").remove();
      }}
      log.scrollTop = log.scrollHeight;
    }}

    function escapeHtml(text) {{
      const div = document.createElement("div");
      div.textContent = text;
      return div.innerHTML;
    }}

    // Sends one command to the robot via the Flask relay (/api/send), which
    // opens a TCP connection to the robot's control server (Pi #1,
    // link/server.py) and returns its ACK/ERR/STA response. Every call is
    // logged to the TCP tab (tcpLogFrame) -- that tab is meant to show
    // *all* communication, including the background status-bar polling
    // below -- and additionally to the visible Console tab (logLine) only
    // when logToConsole is true, i.e. for commands the user actually typed.
    async function sendToRobot(command, {{logToConsole = false}} = {{}}) {{
      let data;
      try {{
        const res = await fetch("{url_for('api_send')}", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{command: command}}),
        }});
        data = await res.json();
      }} catch (err) {{
        const cmdText = "&gt; " + escapeHtml(command);
        const errText = "request failed: " + escapeHtml(String(err));
        tcpLogFrame([{{text: cmdText, cls: "cmd"}}, {{text: errText, cls: "err"}}]);
        if (logToConsole) {{
          logLine(cmdText, "cmd");
          logLine(errText, "err");
        }}
        return {{ok: false, error: String(err)}};
      }}

      const cmdText = "&gt; " + escapeHtml(data.sent || command);
      const resultText = data.ok
        ? escapeHtml(data.raw_response || "")
        : escapeHtml(data.error || data.raw_response || "unknown error");
      const resultCls = data.ok ? "ok" : "err";

      tcpLogFrame([{{text: cmdText, cls: "cmd"}}, {{text: resultText, cls: resultCls}}]);
      if (logToConsole) {{
        logLine(cmdText, "cmd");
        logLine(resultText, resultCls);
      }}

      return data;
    }}

    async function sendCommand(cmd) {{
      await sendToRobot(cmd, {{logToConsole: true}});
    }}

    const input = document.getElementById("consoleInput");
    input.addEventListener("keydown", (e) => {{
      if (e.key === "Enter" && input.value.trim() !== "") {{
        const cmd = input.value.trim();
        input.value = "";
        sendCommand(cmd);
      }}
    }});

    // Console/TCP tab toggle: only one of #consoleLog/#tcpLog is visible
    // at a time, both keep logging in the background regardless of which
    // is shown.
    const consoleLogEl = document.getElementById("consoleLog");
    const tcpLogEl = document.getElementById("tcpLog");
    const tabConsoleBtn = document.getElementById("tabConsoleBtn");
    const tabTcpBtn = document.getElementById("tabTcpBtn");

    function showConsoleTab(tab) {{
      const showTcp = tab === "tcp";
      consoleLogEl.hidden = showTcp;
      tcpLogEl.hidden = !showTcp;
      tabConsoleBtn.classList.toggle("active", !showTcp);
      tabTcpBtn.classList.toggle("active", showTcp);
    }}
    tabConsoleBtn.addEventListener("click", () => showConsoleTab("console"));
    tabTcpBtn.addEventListener("click", () => showConsoleTab("tcp"));

    // Status bar: polls the robot for STA telemetry every few seconds and
    // fills in current/target GPS position, distance/heading to the
    // target (computed here client-side from the two positions -- kept
    // independent from gps/gps_delta.py in the robot repo, which has a
    // known bug), and the two motors' current PWM. Silent on failure (no
    // console spam); falls back to "--" rather than showing a stale or
    // made-up value.
    const statusCurrentPos = document.getElementById("statusCurrentPos");
    const statusTargetPos = document.getElementById("statusTargetPos");
    const statusDistance = document.getElementById("statusDistance");
    const statusBearing = document.getElementById("statusBearing");
    const statusSpeed = document.getElementById("statusSpeed");
    const statusMotorLeft = document.getElementById("statusMotorLeft");
    const statusMotorRight = document.getElementById("statusMotorRight");

    // Converts one NMEA ddmm.mmmm-style field (as used by NAV/STA) into
    // decimal degrees. Works for both 2-digit (latitude) and 3-digit
    // (longitude) degree prefixes without needing to know which: dividing
    // by 100 always isolates the whole degrees, whatever their digit count.
    function nmeaToDecimal(raw, dir) {{
      const val = parseFloat(raw);
      if (raw === undefined || raw === null || isNaN(val)) return null;
      const deg = Math.trunc(val / 100);
      const min = val - deg * 100;
      let dec = deg + min / 60;
      if (dir === "S" || dir === "W") dec = -dec;
      return dec;
    }}

    function haversineMeters(lat1, lon1, lat2, lon2) {{
      const R = 6371000;
      const toRad = (d) => (d * Math.PI) / 180;
      const dLat = toRad(lat2 - lat1);
      const dLon = toRad(lon2 - lon1);
      const a =
        Math.sin(dLat / 2) ** 2 +
        Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
      return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }}

    function bearingDegrees(lat1, lon1, lat2, lon2) {{
      const toRad = (d) => (d * Math.PI) / 180;
      const toDeg = (r) => (r * 180) / Math.PI;
      const y = Math.sin(toRad(lon2 - lon1)) * Math.cos(toRad(lat2));
      const x =
        Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
        Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(toRad(lon2 - lon1));
      return (toDeg(Math.atan2(y, x)) + 360) % 360;
    }}

    function formatLatLon(lat, lon) {{
      return lat.toFixed(5) + "°, " + lon.toFixed(5) + "°";
    }}

    function resetStatusBar() {{
      statusCurrentPos.textContent = "--";
      statusTargetPos.textContent = "--";
      statusDistance.textContent = "--";
      statusBearing.textContent = "--";
      statusSpeed.textContent = "--";
    }}

    let statusPollBusy = false;
    async function pollStatus() {{
      if (statusPollBusy) return;
      statusPollBusy = true;
      try {{
        // logToConsole defaults to false: this background poll is exactly
        // the "hidden" traffic that should only ever show up in the TCP
        // tab, never spam the visible Console tab.
        const data = await sendToRobot("STA");
        if (!data.ok || !data.fields || data.fields.length < 14) {{
          resetStatusBar();
          return;
        }}
        // STA field order (see link/server.py and protocole_controle.html):
        // lat, lat_dir, lon, lon_dir, cap, speed, left_pwm, right_pwm,
        // battery, mode, target_lat, target_lat_dir, target_lon, target_lon_dir.
        const [
          lat, latDir, lon, lonDir, /* cap */, speed, leftPwm, rightPwm,
          /* battery */, /* mode */,
          targetLat, targetLatDir, targetLon, targetLonDir,
        ] = data.fields;

        statusMotorLeft.textContent = leftPwm;
        statusMotorRight.textContent = rightPwm;
        statusSpeed.textContent = speed + " km/h";

        // lat/latDir and lon/lonDir are already grouped as
        // (value, direction) pairs on the wire, so no assumption about
        // hemisphere is made here: this robot operates just west of the
        // meridian, so hardcoding "E" would actually be wrong for its own
        // real position.
        const curLat = nmeaToDecimal(lat, latDir);
        const curLon = nmeaToDecimal(lon, lonDir);
        const hasCurrent = curLat !== null && (curLat !== 0 || curLon !== 0);
        statusCurrentPos.textContent = hasCurrent
          ? formatLatLon(curLat, curLon)
          : "GPS unavailable";

        const tLat = nmeaToDecimal(targetLat, targetLatDir);
        const tLon = nmeaToDecimal(targetLon, targetLonDir);
        const hasTarget = tLat !== null && (tLat !== 0 || tLon !== 0);
        statusTargetPos.textContent = hasTarget
          ? formatLatLon(tLat, tLon)
          : "No target (NAV)";

        if (hasCurrent && hasTarget) {{
          statusDistance.textContent = haversineMeters(curLat, curLon, tLat, tLon).toFixed(1) + " m";
          statusBearing.textContent = bearingDegrees(curLat, curLon, tLat, tLon).toFixed(0) + "°";
        }} else {{
          statusDistance.textContent = "--";
          statusBearing.textContent = "--";
        }}
      }} catch (err) {{
        resetStatusBar();
      }} finally {{
        statusPollBusy = false;
      }}
    }}
    pollStatus();
    setInterval(pollStatus, 3000);

    // Controls panel: one full-width button per predefined NMEA sentence
    // (see pages/protocole_controle.html for the full field reference).
    // Clicking most of these buttons never sends anything by itself -- it
    // only fills the console input with that sentence's beginning (its
    // type plus a trailing comma for types that take fields) so the
    // specific values can be typed in before pressing Enter. The button's
    // own label shows a complete example so it's clear what to fill in.
    // "GPS route" is the one exception (action instead of prefix): a route
    // can be arbitrarily long, so there's no reasonable text to prefill --
    // it opens a file picker instead and sends a whole RTE sentence built
    // from the file's content once one is chosen (see gpsRouteInput below).
    const commandButtons = [
      {{label: "STP",                          prefix: "STP"}},
      {{label: "DRV,120,120",                   prefix: "DRV,"}},
      {{label: "NAV,4723.492,N,00044.340,W",    prefix: "NAV,"}},
      {{label: "GPS route",                     action: "gpsRoute"}},
      {{label: "MOD,MANUAL",                    prefix: "MOD,"}},
      {{label: "PID,D,1.0,0.0,0.5",             prefix: "PID,"}},
      {{label: "CAM,SNAP",                      prefix: "CAM,"}},
      {{label: "STA",                          prefix: "STA"}},
    ];

    // Converts a signed decimal-degrees coordinate into this protocol's
    // on-the-wire (ddmm.mmmm string, direction letter) pair -- a JS mirror
    // of link/nmea.py's decimal_to_nmea() in the robot repo, needed here
    // because an uploaded route file gives plain decimal coordinates but
    // RTE (like NAV) is sent in NMEA ddmm.mmmm form.
    function decimalToNmea(value, isLongitude) {{
      const direction = isLongitude ? (value < 0 ? "W" : "E") : (value < 0 ? "S" : "N");
      const magnitude = Math.abs(value);
      const degrees = Math.floor(magnitude);
      const minutes = (magnitude - degrees) * 60;
      const degDigits = isLongitude ? 3 : 2;
      const degStr = String(degrees).padStart(degDigits, "0");
      const minStr = minutes.toFixed(3).padStart(6, "0");
      return [degStr + minStr, direction];
    }}

    // Parses a GPS route file: one point per line as "lat,lon" in decimal
    // degrees (e.g. 47.391534,-0.739006), blank lines and lines starting
    // with "#" ignored. Extra columns past the first two are ignored too,
    // so an unmodified 2-column export from a spreadsheet works fine.
    function parseGpsRouteFile(text) {{
      const points = [];
      for (const rawLine of text.split(/\\r\\n|\\r|\\n/)) {{
        const line = rawLine.trim();
        if (!line || line.startsWith("#")) continue;
        const parts = line.split(",");
        if (parts.length < 2) continue;
        const lat = parseFloat(parts[0]);
        const lon = parseFloat(parts[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        points.push([lat, lon]);
      }}
      return points;
    }}

    function buildRteCommand(points) {{
      const fields = [String(points.length)];
      for (const [lat, lon] of points) {{
        const [latStr, latDir] = decimalToNmea(lat, false);
        const [lonStr, lonDir] = decimalToNmea(lon, true);
        fields.push(latStr, latDir, lonStr, lonDir);
      }}
      return "RTE," + fields.join(",");
    }}

    // Hidden file input backing the "GPS route" button -- only a real user
    // click (not a script) is allowed to open the browser's file picker,
    // so the button's own click handler below just forwards to this
    // input's click() instead of showing a picker UI itself.
    const gpsRouteInput = document.createElement("input");
    gpsRouteInput.type = "file";
    gpsRouteInput.accept = ".txt,.csv";
    gpsRouteInput.hidden = true;
    document.body.appendChild(gpsRouteInput);

    gpsRouteInput.addEventListener("change", () => {{
      const file = gpsRouteInput.files[0];
      gpsRouteInput.value = "";  // lets the same file be re-picked later
      if (!file) return;

      const reader = new FileReader();
      reader.onload = () => {{
        const points = parseGpsRouteFile(String(reader.result));
        if (points.length === 0) {{
          logLine(
            "GPS route: no valid point found in " + escapeHtml(file.name) +
              " (expected one 'lat,lon' per line, decimal degrees)",
            "err"
          );
          return;
        }}
        sendCommand(buildRteCommand(points));
      }};
      reader.onerror = () => {{
        logLine("GPS route: could not read " + escapeHtml(file.name), "err");
      }};
      reader.readAsText(file);
    }});

    // controlsButtons doesn't exist for a read-only (viewer) session --
    // that panel shows a plain message instead, see _control_page().
    const controlsButtons = document.getElementById("controlsButtons");
    if (controlsButtons) {{
      for (const buttonDef of commandButtons) {{
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cmd-button";
        btn.textContent = buttonDef.label;
        if (buttonDef.action === "gpsRoute") {{
          btn.title = "Pick a text file with one 'lat,lon' GPS point per line";
          btn.addEventListener("click", () => gpsRouteInput.click());
        }} else {{
          btn.addEventListener("click", () => {{
            input.value = buttonDef.prefix;
            input.focus();
            input.setSelectionRange(buttonDef.prefix.length, buttonDef.prefix.length);
          }});
        }}
        controlsButtons.appendChild(btn);
      }}
    }}

    // Video playlist: one video at a time, auto-advances to the next when
    // the current one ends, wrapping back to the first after the last.
    const playlistVideos = [{video_urls_js}];
    if (playlistVideos.length > 0) {{
      const player = document.getElementById("videoPlayer");
      let videoIndex = 0;

      function playVideoAt(i) {{
        player.src = playlistVideos[i];
        player.play().catch(() => {{}}); // autoplay may be blocked in rare cases; muted so it usually isn't
      }}

      player.addEventListener("ended", () => {{
        videoIndex = (videoIndex + 1) % playlistVideos.length;
        playVideoAt(videoIndex);
      }});

      playVideoAt(0);
    }}

    // Live camera feed: tries to load the robot's live MJPEG stream
    // (proxied through /media/camera so it stays behind this site's
    // login). As soon as it loads, it takes the place of the recorded
    // video playlist/placeholder; on error (robot's camera script not
    // running, wrong IP...) the recorded playlist/placeholder is shown
    // instead, and the feed is retried every few seconds -- so the panel
    // switches over on its own once the live feed becomes available.
    const cameraStream = document.getElementById("cameraStream");
    const recordedVideoArea = document.getElementById("recordedVideoArea");
    const CAMERA_URL = "{url_for('media_camera')}";
    const CAMERA_RETRY_MS = 5000;

    function tryCameraStream() {{
      // Cache-bust so each retry is a fresh connection attempt instead of
      // reusing a broken one.
      cameraStream.src = CAMERA_URL + "?t=" + Date.now();
    }}

    cameraStream.addEventListener("load", () => {{
      cameraStream.hidden = false;
      recordedVideoArea.hidden = true;
    }});

    cameraStream.addEventListener("error", () => {{
      cameraStream.hidden = true;
      recordedVideoArea.hidden = false;
      setTimeout(tryCameraStream, CAMERA_RETRY_MS);
    }});

    tryCameraStream();

    // Images slideshow: swap the <img> source every 3 seconds.
    const slideshowImages = [{image_urls_js}];
    if (slideshowImages.length > 0) {{
      const img = document.getElementById("slideshowImg");
      let slideshowIndex = 0;
      img.src = slideshowImages[0];
      if (slideshowImages.length > 1) {{
        setInterval(() => {{
          slideshowIndex = (slideshowIndex + 1) % slideshowImages.length;
          img.src = slideshowImages[slideshowIndex];
        }}, 3000);
      }}
    }}
  </script>

</body>
</html>"""


@app.route("/control")
@login_required
def control():
    videos = _list_media(VIDEOS_DIR, VIDEO_EXTENSIONS)
    images = _list_media(IMAGES_DIR, IMAGE_EXTENSIONS)
    return _control_page(videos, images, role=session.get("role", "admin"))


@app.route("/api/send", methods=["POST"])
@login_required
def api_send():
    """Relays one console command to the robot's control server and
    returns its ACK/ERR/STA response as JSON. `command` is the sentence
    type + fields the user typed, e.g. "STP" or "DRV,120,120" -- the
    "$PROV," prefix and checksum are added by robot_link.send_command.

    A viewer/guest session (see WEBSERVER_VIEWER_USERNAME) is rejected here
    with 403 for anything but the read-only STA query, regardless of what
    the client sends -- the /control page already hides the means to send
    other commands, but that's just UX: this check is the actual boundary,
    since a guest could otherwise call this endpoint directly."""
    data = request.get_json(silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"ok": False, "error": "EMPTY_COMMAND"}), 400

    if session.get("role") == "viewer" and _command_type(command) not in VIEWER_ALLOWED_COMMANDS:
        return jsonify({"ok": False, "error": "FORBIDDEN_READ_ONLY_ACCOUNT"}), 403

    result = send_command(command, ROBOT_HOST, ROBOT_PORT)
    return jsonify(result)


@app.route("/media/camera")
@login_required
def media_camera():
    """Proxies the robot's live MJPEG camera stream (Pi #1,
    camera/stream_server.py in the robot repo) so the browser only ever
    talks to this server -- the feed stays behind the login instead of
    being reachable directly on the LAN. Returns 502 quickly (instead of
    hanging) if the robot's camera script isn't running or unreachable, so
    the /control page's <img> "error" handler fires and falls back to the
    recorded video playlist."""
    stream_url = f"http://{ROBOT_HOST}:{CAMERA_PORT}{CAMERA_STREAM_PATH}"
    try:
        upstream = requests.get(stream_url, stream=True, timeout=CAMERA_TIMEOUT)
    except requests.exceptions.RequestException:
        abort(502)

    if upstream.status_code != 200:
        upstream.close()
        abort(502)

    content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace")

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(relay(), mimetype=content_type)


@app.route("/media/videos/<path:filename>")
@login_required
def media_video(filename):
    """Serves a video from media/videos/ as-is."""
    if not filename.lower().endswith(VIDEO_EXTENSIONS):
        abort(404)
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/media/images/<path:filename>")
@login_required
def media_image(filename):
    """Serves an image from media/images/ as-is."""
    if not filename.lower().endswith(IMAGE_EXTENSIONS):
        abort(404)
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login form. On success, redirects to the originally requested page
    (the 'next' parameter), otherwise to the control page (the landing page
    after login). Two independent credential sets are accepted: the main
    account (WEBSERVER_USERNAME/HASH, full access, role "admin") and an
    optional read-only guest account (WEBSERVER_VIEWER_USERNAME/HASH, role
    "viewer") -- see api_send() and _control_page() for what the "viewer"
    role actually restricts."""
    error = None
    next_url = request.values.get("next") or url_for("control")

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        admin_configured = bool(WEBSERVER_USERNAME and WEBSERVER_PASSWORD_HASH)
        viewer_configured = bool(WEBSERVER_VIEWER_USERNAME and WEBSERVER_VIEWER_PASSWORD_HASH)

        is_admin = (
            admin_configured
            and username == WEBSERVER_USERNAME
            and check_password_hash(WEBSERVER_PASSWORD_HASH, password)
        )
        is_viewer = (
            not is_admin
            and viewer_configured
            and username == WEBSERVER_VIEWER_USERNAME
            and check_password_hash(WEBSERVER_VIEWER_PASSWORD_HASH, password)
        )

        if is_admin or is_viewer:
            session.clear()
            session["logged_in"] = True
            session["username"] = username
            session["role"] = "admin" if is_admin else "viewer"
            return redirect(next_url)

        if not admin_configured:
            error = "No credentials configured on the server -- see .env.example in README.md."
        else:
            error = "Incorrect username or password."

    return _login_page(next_url, error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    """Root URL: /control is the landing page after login, so this just
    redirects straight there. Kept as its own route (rather than moving
    the redirect into login()) so that a stray "next=/" -- e.g. someone's
    old bookmark of the bare root URL, which used to be the pages list --
    still ends up on /control instead of resurrecting that old page."""
    return redirect(url_for("control"))


@app.route("/pages")
@login_required
def pages_index():
    """Lists the HTML pages available in pages/ (moved here from / now
    that /control is the landing page after login)."""
    if not os.path.isdir(PAGES_DIR):
        return _page("Robot web server", "<p>pages/ folder not found.</p>"), 500

    files = sorted(f for f in os.listdir(PAGES_DIR) if f.lower().endswith(".html"))
    if not files:
        list_html = "<p>No HTML pages in pages/ yet.</p>"
    else:
        items = "".join(f'<li><a href="/pages/{f}">{f}</a></li>' for f in files)
        list_html = f"<ul>{items}</ul>"

    body = f"""
  <div class="top">
    <h1>Available pages</h1>
    <a class="logout" href="{url_for('logout')}">Log out</a>
  </div>
  <p><a href="{url_for('control')}">&larr; Back to control page</a></p>
  {list_html}
"""
    return _page("Robot web server", body)


@app.route("/pages/<path:filename>")
@login_required
def serve_page(filename):
    """Serves an HTML page from pages/ as-is (no Jinja templating, to avoid
    breaking the HTML/CSS of pages generated elsewhere)."""
    if not filename.lower().endswith(".html"):
        abort(404)
    return send_from_directory(PAGES_DIR, filename)


if __name__ == "__main__":
    if not (WEBSERVER_USERNAME and WEBSERVER_PASSWORD_HASH):
        print("WARNING: WEBSERVER_USERNAME / WEBSERVER_PASSWORD_HASH not set in .env "
              "-- nobody will be able to log in. See README.md.")
    # host="0.0.0.0": reachable from other devices on the WiFi, not just
    # from the Pi itself (127.0.0.1). threaded=True: without it, Flask's
    # dev server handles one request at a time -- the live camera proxy
    # (/media/camera) holds its connection open for as long as it's being
    # viewed, which would otherwise block every other page/API call.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

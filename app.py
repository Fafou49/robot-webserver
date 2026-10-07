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

from power_history_client import PowerHistoryError, fetch_power_history, fetch_solar_map
from robot_link import send_command

load_dotenv()

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".ogg", ".mov")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# /control's GPS map (2026-10-05): on-demand, short-lived cache for the
# photo/video thumbnails shown when hovering a violet marker (see
# media_thumb() below) -- these files live on Pi #1's camera process, NOT
# in MEDIA_DIR/videos or images above (that's a separate, manually curated
# folder, see _list_media()), and are only ever downloaded to Pi #2 when
# the two Pis are actually in contact (explicit user request). Wiped every
# time /control is (re)loaded (see control() below) rather than kept
# indefinitely -- it mirrors whatever's CURRENTLY hoverable on the map,
# not a permanent archive.
MEDIA_THUMB_CACHE_DIR = os.path.join(MEDIA_DIR, "tmp_media_cache")

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

# Commands a viewer/guest session is allowed to send via /api/send. STA,
# PWR, WPT, GRT and MED all take no fields and only read telemetry back
# (PWR added with the /power page, 2026-10-03; WPT/GRT/MED added with
# /control's GPS map, 2026-10-05), so none of them can affect the robot --
# every other sentence type (STP, DRV, NAV, MOD, PID, CAM...) is refused
# for that role regardless of what the client sends.
VIEWER_ALLOWED_COMMANDS = {"STA", "PWR", "WPT", "GRT", "MED"}


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


def _clear_media_thumb_cache():
    """Wipes MEDIA_THUMB_CACHE_DIR (see its own comment above) -- called
    every time /control is (re)loaded, per explicit user request ("sur la
    pi2 c'est dans un fichier tmp qui se supprime au rafraichissement de
    la page"): this is a short-lived cache for whatever's currently
    hoverable on the map, not a permanent store, so clearing it on every
    page load keeps it from ever growing unbounded or outliving the DB
    rows/files it mirrors on Pi #1."""
    if not os.path.isdir(MEDIA_THUMB_CACHE_DIR):
        return
    for name in os.listdir(MEDIA_THUMB_CACHE_DIR):
        try:
            os.remove(os.path.join(MEDIA_THUMB_CACHE_DIR, name))
        except OSError:
            pass  # already gone, or not a plain file -- fine, not worth failing the page load over


def _page(title, body):
    """Small shared page shell, used by /pages (listing + its 500 fallback).
    Background image + link color added 2026-09-09: the tech-stack graphic
    (static/tech_stack.png, HTML/CSS/JS/Python/Raspberry Pi around Claude)
    as a fixed page background, links in the same blue (#58a6ff) as the
    circle around Claude in that image -- also /control's existing accent
    color, so it stays visually consistent with the rest of the site.

    background-size: contain (not cover) so the whole image is always
    visible on a standard screen, never cropped -- the tradeoff is empty
    space on the sides (image is square, most screens aren't). That empty
    space is covered by background-color below, set to the exact same
    #0d1117 the image itself was generated with as its own background, so
    the letterboxing blends into the image instead of showing as a
    visibly different-colored border. If tech_stack.png is ever replaced
    with an image using a different background color, update this
    background-color to match, or the seam will show again.

    The content sits in a translucent dark panel so it stays readable no
    matter which part of the image ends up behind it at a given viewport
    size."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    html, body {{ height: 100%; }}
    body {{
      font-family: system-ui, sans-serif;
      color: #e6edf3;
      margin: 0;
      min-height: 100vh;
      background-image: url('/static/tech_stack.png');
      background-size: contain;
      background-position: center;
      background-repeat: no-repeat;
      background-attachment: fixed;
      background-color: #0d1117;
    }}
    .panel {{
      max-width: 420px;
      margin: 80px auto;
      padding: 24px 28px;
      background: rgba(13, 17, 23, 0.78);
      border: 1px solid #30363d;
      border-radius: 12px;
      backdrop-filter: blur(3px);
    }}
    h1 {{ font-size: 20px; margin-bottom: 24px; }}
    label {{ display: block; font-size: 13px; margin: 14px 0 4px; color: #c9d1d9; }}
    input {{ width: 100%; box-sizing: border-box; padding: 9px 10px; font-size: 15px; border: 1px solid #30363d; border-radius: 6px; background: #0d1117; color: #e6edf3; }}
    button {{ margin-top: 20px; padding: 9px 16px; font-size: 15px; border: none; border-radius: 6px; background: #1a56db; color: white; cursor: pointer; }}
    button:hover {{ background: #1544ab; }}
    .error {{ background: #fdecea; color: #a12622; padding: 10px 12px; border-radius: 6px; font-size: 14px; margin-top: 16px; }}
    li {{ margin: 8px 0; }}
    a {{ font-size: 16px; color: #58a6ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .logout {{ font-size: 13px; color: #8b949e; }}
  </style>
</head>
<body>
<div class="panel">
{body}
</div>
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


def _control_page(role="admin"):
    """Robot control page (landing page after login): clock fixed top-right,
    main area is the Controls panel (still a placeholder), bottom third is
    a terminal-style console. No backend wired in yet -- the robot
    communication protocol is still to be defined (see the report's "A
    suivre" section), so the console just echoes what you type.

    The Video feed / Images panels that used to live inline in this page
    moved out to their own /media page on 2026-10-05 (see _media_page
    below and the "Media" nav link) so this page stays focused on driving
    the robot. A plain live-camera column came back on 2026-10-06
    (explicit user request: actually piloting the robot needs to see
    where it's pointed) -- deliberately NOT the full Video feed panel
    from /media, just the bare live stream with no recorded-video
    fallback, in a new middle column between Controls and Map.

    role: "admin" (full access) or "viewer" (read-only guest account, see
    WEBSERVER_VIEWER_USERNAME above) -- a viewer never sees the Controls
    buttons or gets a usable console input, so there is no client-side way
    to type or trigger a driving command; /api/send enforces the same
    restriction server-side regardless, so this is a UX nicety, not the
    actual security boundary."""
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

    /* Waypoint-return / GPS-driving-route progress bar (2026-10-07): sits
       directly under the status bar, same left/right padding and
       border-bottom rhythm so it reads as part of the same status region
       rather than a new panel. Hidden entirely (see updateReturnProgress()
       below) whenever there's no active route or it's just been
       completed. Two modes, same component: GRT's trailing mode field
       (see link/server.py) is "RETURN" for a BTN_A waypoint return (blue,
       matching .map-dot-waypoint) or "DRIVE" for an uploaded/typed RTE
       GPS driving route (red, matching .map-dot-route) -- only the color
       and title/legend text change, the distance/ETA math underneath is
       identical. Intermediate points are spaced by real cumulative
       distance (haversine from the robot's live position), not by
       count. */
    .return-progress {{
      padding: 10px 24px 14px;
      border-bottom: 1px solid #30363d;
    }}
    .return-progress-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 10px;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .return-progress-title {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: #8b949e;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .return-progress-title .dot {{
      width: 7px; height: 7px; border-radius: 50%;
      background: #58a6ff;
      box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.18);
    }}
    .return-progress.mode-route .return-progress-title .dot {{
      background: #f85149;
      box-shadow: 0 0 0 3px rgba(248, 81, 73, 0.18);
    }}
    .return-progress.mode-route .return-track-fill {{ background: #f85149; }}
    .return-progress.mode-route .return-point:not(.target) {{ background: #f85149; }}
    .return-progress-stat {{
      font-family: "Courier New", monospace;
      font-size: 13px;
      color: #e6edf3;
    }}
    .return-progress-stat .muted {{ color: #8b949e; font-family: system-ui, sans-serif; font-size: 11.5px; }}

    .return-track-wrap {{
      position: relative;
      padding: 14px 0 26px;
    }}
    .return-track {{
      position: relative;
      height: 6px;
      background: #21262d;
      border-radius: 3px;
      overflow: visible;
    }}
    .return-track-fill {{
      position: absolute;
      left: 0; top: 0; bottom: 0;
      background: #58a6ff;
      border-radius: 3px;
      transition: width 0.6s ease;
    }}
    /* Current-position marker: always the left/0% end -- 0% is the
       position the route/return started from as of THIS poll, not a
       continuously-recomputed "current position" dragged along a fixed
       scale (each poll re-derives a fresh "distance from here" snapshot,
       so the fill is simply reset to 0% every time rather than
       animated). */
    .return-current {{
      position: absolute;
      top: 50%;
      left: 0;
      width: 13px; height: 13px;
      margin-left: -6.5px; margin-top: -6.5px;
      border-radius: 50%;
      background: #0d1117;
      border: 2px solid #3fb950;
      box-shadow: 0 0 0 3px rgba(63, 185, 80, 0.18);
    }}
    .return-point {{
      position: absolute;
      top: 50%;
      width: 10px; height: 10px;
      margin-left: -5px; margin-top: -5px;
      border-radius: 50%;
      background: #58a6ff;
      border: 1px solid #1b1f24;
      cursor: default;
    }}
    .return-point.target {{
      width: 12px; height: 12px;
      margin-left: -6px; margin-top: -6px;
      background: #3fb950;
    }}
    .return-point-label {{
      position: absolute;
      top: 16px;
      transform: translateX(-50%);
      font-family: "Courier New", monospace;
      font-size: 10.5px;
      color: #8b949e;
      white-space: nowrap;
      text-align: center;
    }}
    .return-point-label .idx {{ color: #e6edf3; }}

    /* Hover tooltip on a point -- width:max-content is required here: an
       absolutely-positioned ::after with no explicit width otherwise
       shrink-wraps to its tiny containing block (the 10px dot itself),
       which wraps white-space:pre-line onto one word per line. */
    .return-point::after {{
      content: attr(data-tip);
      position: absolute;
      bottom: 22px;
      left: 50%;
      transform: translateX(-50%);
      background: #161b22;
      border: 1px solid #30363d;
      color: #e6edf3;
      font-family: "Courier New", monospace;
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 5px;
      white-space: pre-line;
      width: max-content;
      max-width: 320px;
      text-align: left;
      line-height: 1.5;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.12s ease;
      z-index: 5;
    }}
    .return-point:hover::after {{ opacity: 1; }}

    .return-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      font-size: 11px;
      color: #8b949e;
      margin-top: 2px;
    }}
    .return-legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
    .return-legend .swatch {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; border: 1px solid #1b1f24; }}
    .return-legend .swatch.blue {{ background: #58a6ff; }}
    .return-legend .swatch.red {{ background: #f85149; }}
    .return-legend .ring {{
      display: inline-block; width: 9px; height: 9px; border-radius: 50%;
      border: 2px solid #3fb950; background: #0d1117;
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
    /* Bugfix (2026-10-06, see the identical fix + full explanation in
       _media_page's own copy of this rule): `display: flex` above beats
       the browser's default "[hidden] {{ display: none }}" on specificity,
       so toggling cameraPlaceholder.hidden below would otherwise do
       nothing. */
    .panel .placeholder[hidden] {{ display: none; }}

    /* Camera panel (2026-10-06, explicit user request): a middle column
       between Controls and Map showing the robot's live feed, so driving
       doesn't require switching to the separate /media page to see where
       the robot is pointed. Deliberately simpler than /media's own
       "Video feed" panel: no recorded-video fallback here (stale footage
       is actively misleading while actually piloting) -- just the live
       stream, or a plain placeholder while it's unavailable. */
    .camera-feed-area {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .camera-stream {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      object-fit: contain;
    }}
    .camera-stream[hidden] {{ display: none; }}

    /* GPS map panel (2026-10-05): overrides .panel's own center/center
       alignment (meant for a single centered placeholder) so the map
       actually fills the panel instead of shrinking to its own size. */
    .panel-map {{
      align-items: stretch;
      justify-content: flex-start;
    }}
    .map-wrap {{
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    /* Light background on purpose: the map is a plain lat/lon scatter
       plot (a grid, not real map tiles -- this robot operates outdoors
       without a reliable data connection, so no OpenStreetMap/Leaflet
       tile fetch), and a light surface reads more like "paper/plotted
       map" than the page's own dark chrome around it. */
    .gps-map {{
      flex: 1;
      min-height: 0;
      width: 100%;
      background: #eef0f2;
      background-image:
        linear-gradient(to right, #dde1e6 1px, transparent 1px),
        linear-gradient(to bottom, #dde1e6 1px, transparent 1px);
      background-size: 40px 40px;
      border-radius: 8px;
    }}
    .map-legend {{
      flex: 0 0 auto;
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      font-size: 11px;
      color: #8b949e;
    }}
    .map-legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
    .map-hint {{ color: #656d76; font-style: italic; }}
    .map-dot {{
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      border: 1px solid #1b1f24;
    }}
    .map-dot-robot {{ background: #3fb950; }}
    .map-dot-waypoint {{ background: #58a6ff; }}
    .map-dot-route {{ background: #f85149; }}
    .map-dot-media {{ background: #8957e5; }}

    /* Solar-exposure map overlay (2026-10-07, explicit user request):
       a toggle row under the existing map-legend, shown whenever the
       map panel is visible (not gated on the checkbox itself -- the
       legend/gradient only has to look right once data has loaded, see
       updateSolarLegend() below). Same dashed-divider trick as
       .return-progress's own panel-within-a-panel styling elsewhere in
       this file, to visually separate it from the legend above. */
    .solar-toggle-row {{
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      font-size: 11px;
      color: #8b949e;
      padding-top: 6px;
      border-top: 1px dashed #30363d;
    }}
    .solar-toggle-label {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: #c9d1d9;
      cursor: pointer;
      user-select: none;
    }}
    .solar-toggle-label input {{ accent-color: #ffb454; cursor: pointer; }}
    .solar-legend {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 10.5px;
      color: #8b949e;
    }}
    /* Without this, the [hidden] attribute's own display:none loses to
       the class rule above (same specificity, class rule comes later in
       the cascade) -- same fix this file already applies to
       .camera-stream[hidden] above for the exact same reason. */
    .solar-legend[hidden] {{ display: none; }}
    /* Sequential scale (one tonal journey, dark -> warm/bright): low PV
       power reads as dark navy, high PV power as bright yellow -- same
       4-stop gradient drawn both here (CSS, for the legend swatch) and
       in JS (solarColorForRatio() below, for the actual grid squares,
       which need a single interpolated color per cell rather than a
       fixed CSS gradient). */
    .solar-gradient-bar {{
      width: 90px;
      height: 8px;
      border-radius: 4px;
      border: 1px solid #1b1f24;
      background: linear-gradient(to right, #1a1f3d, #6a3d9a, #e8572c, #ffd23f);
    }}
    .solar-legend-note {{ color: #656d76; font-style: italic; }}

    /* Distance scale bar (bottom-left of the map, drawn fresh on every
       redraw -- see drawMapScaleBar() below): plain SVG line/text
       elements, styled here rather than with setAttribute so they read
       the same light-surface colour scheme as the grid they sit on. */
    .map-scale-bar line {{ stroke: #57606a; stroke-width: 1.5; }}
    .map-scale-bar text {{ font-size: 10px; font-family: "Courier New", monospace; fill: #41464c; }}

    /* Hover tooltip: floats over the map at the cursor position (see the
       script below) -- pointer-events:none so it can never itself be the
       thing the mouse is "over", which would otherwise flicker the
       underlying marker's mouseleave on and off. */
    .map-tooltip {{
      position: absolute;
      pointer-events: none;
      max-width: 240px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 12px;
      line-height: 1.5;
      color: #e6edf3;
      z-index: 20;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }}
    .map-tooltip-thumb {{
      display: block;
      margin-top: 6px;
      max-width: 220px;
      max-height: 160px;
      border-radius: 4px;
      background: #000;
    }}
    .map-tooltip-note {{
      margin-top: 6px;
      color: #8b949e;
      font-style: italic;
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
    /* STOP: the one emergency, direct-send button (see commandButtons
       below) -- visually set apart from the fill-then-Enter buttons so
       it reads as "press this and it's already sent", not "press this to
       start typing". */
    .cmd-button-danger {{
      background: #2d1214;
      border-color: #f85149;
      color: #ffb3ac;
      font-weight: 600;
      text-align: center;
    }}
    .cmd-button-danger:hover {{ background: #3d181b; border-color: #ff7b72; }}
    .cmd-button-danger:active {{ background: #24100f; }}

    /* "Distance + angle" (2026-09-12): a small inline form that drops
       down under its button instead of prefilling the console -- the two
       numbers it needs (distance, angle) don't fit the "prefix + type the
       rest" pattern the other buttons use, and unlike GPS Driving's file
       picker there's no native browser widget for this input. Hidden
       until its button is clicked (see the "open" class below). */
    .cmd-distance-panel {{
      display: none;
      flex-direction: column;
      gap: 6px;
      padding: 10px 12px;
      margin-top: -4px;
      background: #0d1117;
      border: 1px solid #30363d;
      border-top: none;
      border-radius: 0 0 6px 6px;
      font-family: "Courier New", monospace;
      font-size: 12.5px;
    }}
    .cmd-distance-panel.open {{ display: flex; }}
    .cmd-distance-panel label {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: #8b949e;
    }}
    .cmd-distance-panel input[type="number"] {{
      width: 90px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 4px;
      color: #e6edf3;
      font-family: inherit;
      font-size: inherit;
      padding: 4px 6px;
    }}
    .cmd-distance-panel button {{
      margin-top: 2px;
      padding: 6px 10px;
      background: #1f6feb;
      border: 1px solid #388bfd;
      border-radius: 6px;
      color: #e6edf3;
      font-family: inherit;
      font-size: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    .cmd-distance-panel button:hover {{ background: #388bfd; }}

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
    <a href="{url_for('power')}">Power</a>
    <a href="{url_for('media')}">Media</a>
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

  <!-- Waypoint-return / GPS-driving-route progress bar (2026-10-07): hidden
       by default, shown by updateReturnProgress() (see script below) only
       while GRT reports a non-empty route still to drive. -->
  <div class="return-progress" id="returnProgress" hidden>
    <div class="return-progress-head">
      <span class="return-progress-title"><span class="dot" id="returnModeDot"></span><span id="returnModeTitle">Waypoint return (BTN_A)</span></span>
      <span class="return-progress-stat" id="returnStatLine">--</span>
    </div>
    <div class="return-track-wrap">
      <div class="return-track">
        <div class="return-track-fill" id="returnTrackFill"></div>
        <div class="return-current"></div>
        <div id="returnPointsLayer"></div>
      </div>
    </div>
    <div class="return-legend">
      <span><span class="ring"></span>Current position</span>
      <span><span class="swatch" id="returnLegendSwatch"></span><span id="returnLegendPointLabel">Remaining waypoint</span></span>
      <span><span class="swatch" style="background:#3fb950;"></span>Target / final point</span>
    </div>
  </div>

  <div class="main-area">
    <div class="panel">
      <h2>Controls</h2>
      {controls_html}
    </div>
    <div class="panel">
      <h2>Camera</h2>
      <div class="camera-feed-area">
        <img class="camera-stream" id="cameraStream" alt="Live camera feed" hidden>
        <div class="placeholder" id="cameraPlaceholder">Camera unavailable &mdash; retrying...</div>
      </div>
    </div>
    <div class="panel panel-map">
      <h2>Map</h2>
      <div class="map-wrap">
        <svg id="gpsMap" class="gps-map" viewBox="0 0 400 400" preserveAspectRatio="xMidYMid meet"></svg>
        <div class="map-tooltip" id="mapTooltip" hidden></div>
        <div class="map-legend">
          <span><i class="map-dot map-dot-robot"></i>Robot</span>
          <span><i class="map-dot map-dot-waypoint"></i>Waypoints</span>
          <span><i class="map-dot map-dot-route"></i>NAV / GPS Driving</span>
          <span><i class="map-dot map-dot-media"></i>Photos / Videos</span>
          <span class="map-hint">Scroll to zoom &middot; double-click to reset &middot; right-click any point to delete</span>
        </div>
        <div class="solar-toggle-row">
          <label class="solar-toggle-label">
            <input type="checkbox" id="solarMapToggle">
            Solar exposure map
          </label>
          <span class="solar-legend" id="solarLegend" hidden>
            Low
            <span class="solar-gradient-bar"></span>
            High
            <span class="solar-legend-note" id="solarLegendNote"></span>
          </span>
        </div>
      </div>
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

    // Live camera feed (2026-10-06): same proxied-MJPEG-through-/media/camera
    // approach as /media's own "Video feed" panel (see _media_page below),
    // just without that page's recorded-video fallback -- see the CSS
    // comment above .camera-feed-area for why. Retries on its own every
    // few seconds until the robot's camera script is actually reachable.
    const cameraStream = document.getElementById("cameraStream");
    const cameraPlaceholder = document.getElementById("cameraPlaceholder");
    const CAMERA_URL = "{url_for('media_camera')}";
    const CAMERA_RETRY_MS = 5000;

    function tryCameraStream() {{
      // Cache-bust so each retry is a fresh connection attempt instead of
      // reusing a broken one.
      cameraStream.src = CAMERA_URL + "?t=" + Date.now();
    }}

    cameraStream.addEventListener("load", () => {{
      cameraStream.hidden = false;
      cameraPlaceholder.hidden = true;
    }});

    cameraStream.addEventListener("error", () => {{
      cameraStream.hidden = true;
      cameraPlaceholder.hidden = false;
      setTimeout(tryCameraStream, CAMERA_RETRY_MS);
    }});

    tryCameraStream();

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
    // GPS map (2026-10-05): the last NAV sentence sent from this console
    // (manually typed, or via the "Distance + angle" panel below), kept
    // client-side only and reset on page reload -- there is no server-
    // side record of "the last manual NAV" (STA.target already reflects
    // whatever's currently being chased, which an active route also
    // overwrites), so the map's red "NAV sent" marker is tracked here
    // instead, the moment a NAV actually succeeds. See sendToRobot()
    // below (where it's set) and pollMap() further down (where it's
    // read).
    let lastNavSent = null;

    async function sendToRobot(command, {{logToConsole = false}} = {{}}) {{
      // Auto-fixes a decimal-degrees NAV/RTE point before it ever leaves
      // the browser -- see normalizeGpsCommand() below for why. A no-op
      // for GPS Driving/Distance+angle (already correct NMEA) and for
      // anything that isn't NAV/RTE.
      command = normalizeGpsCommand(command);
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

      if (data.ok && command.trim().toUpperCase().startsWith("NAV,")) {{
        const parts = command.split(",").map((p) => p.trim());
        if (parts.length === 5) {{
          const navLat = nmeaToDecimal(parts[1], parts[2]);
          const navLon = nmeaToDecimal(parts[3], parts[4]);
          if (navLat !== null) {{
            lastNavSent = {{lat: navLat, lon: navLon, sentAt: Math.floor(Date.now() / 1000)}};
          }}
        }}
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

    // Inverse of haversineMeters/bearingDegrees above: given a starting
    // point, a distance in meters and a bearing in degrees (0 = North,
    // clockwise), returns the [lat, lon] destination point -- the direct
    // ("forward") geodesic problem, same spherical-Earth model (R) as
    // haversineMeters so the two stay consistent with each other. Used by
    // the "Distance + angle" button to turn "go 12 m at bearing 40°" into
    // an actual GPS point to send as NAV.
    function destinationPoint(lat, lon, distanceMeters, bearingDeg) {{
      const R = 6371000;
      const toRad = (d) => (d * Math.PI) / 180;
      const toDeg = (r) => (r * 180) / Math.PI;
      const delta = distanceMeters / R;
      const theta = toRad(bearingDeg);
      const phi1 = toRad(lat);
      const lambda1 = toRad(lon);

      const phi2 = Math.asin(
        Math.sin(phi1) * Math.cos(delta) + Math.cos(phi1) * Math.sin(delta) * Math.cos(theta)
      );
      const lambda2 =
        lambda1 +
        Math.atan2(
          Math.sin(theta) * Math.sin(delta) * Math.cos(phi1),
          Math.cos(delta) - Math.sin(phi1) * Math.sin(phi2)
        );

      // Normalize longitude back into [-180, 180) -- only matters for a
      // destination point near the antimeridian, but cheap to always do.
      return [toDeg(phi2), ((toDeg(lambda2) + 540) % 360) - 180];
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

    // GPS map (2026-10-05): plots Robot (green, live STA position),
    // Waypoints (blue, WPT -- gamepad's X button, joined by thin segments
    // in save order), NAV sent + GPS Driving (red, both grouped under one
    // colour -- the last manual NAV from this console and the currently
    // active GRT route), and Photos/Videos (violet, MED -- geotagged
    // camera snapshots/recordings still present on Pi #1's camera
    // process). A plain lat/lon scatter plot on a grid, not real map
    // tiles (this robot operates outdoors without a reliable data
    // connection) -- see pages/protocole_controle.html for the WPT/GRT/
    // MED sentence formats this decodes.
    const gpsMapSvg = document.getElementById("gpsMap");
    const mapTooltip = document.getElementById("mapTooltip");
    const MAP_NS = "http://www.w3.org/2000/svg";
    const MAP_VIEW_SIZE = 400;
    const MAP_COLORS = {{robot: "#3fb950", waypoint: "#58a6ff", route: "#f85149", media: "#8957e5"}};

    // ---- Solar-exposure map overlay (2026-10-07, explicit user request) ----
    // Toggleable grid of colored squares showing average PV power per
    // ~5m cell (see link/solar_map.py + link/power_history.py's
    // solar_map_cells, fetched via /api/solar_map -> power_history_
    // client.py's fetch_solar_map() -> link/server.py's SMP sentence).
    // Drawn as its own layer, behind every existing marker/line (see
    // drawSolarCells() below), and deliberately excluded from the map's
    // own auto-fit bounds computation (computeBounds() only ever sees
    // mapLastPoints, never solarCells) -- the overlay should never be
    // what decides the zoom level.
    const solarMapToggle = document.getElementById("solarMapToggle");
    const solarLegend = document.getElementById("solarLegend");
    const solarLegendNote = document.getElementById("solarLegendNote");
    let solarMapEnabled = false;
    let solarCells = []; // [{{lat, lon, avg_pv_power, sample_count, last_ts}}, ...]
    let solarPollBusy = false;

    // Sequential scale, one tonal journey from dark (low PV power) to
    // bright warm (high PV power) -- same 4 stops as the CSS
    // .solar-gradient-bar legend swatch above, interpolated here in
    // plain RGB since each cell needs one specific color rather than a
    // fixed CSS gradient.
    const SOLAR_COLOR_STOPS = [
      [0.00, [0x1a, 0x1f, 0x3d]],
      [0.33, [0x6a, 0x3d, 0x9a]],
      [0.66, [0xe8, 0x57, 0x2c]],
      [1.00, [0xff, 0xd2, 0x3f]],
    ];

    function solarColorForRatio(t) {{
      t = Math.max(0, Math.min(1, t));
      for (let i = 0; i + 1 < SOLAR_COLOR_STOPS.length; i++) {{
        const [t0, c0] = SOLAR_COLOR_STOPS[i];
        const [t1, c1] = SOLAR_COLOR_STOPS[i + 1];
        if (t <= t1 || i + 2 === SOLAR_COLOR_STOPS.length) {{
          const span = t1 - t0 || 1;
          const frac = Math.max(0, Math.min(1, (t - t0) / span));
          const [r0, g0, b0] = c0, [r1, g1, b1] = c1;
          const r = Math.round(r0 + (r1 - r0) * frac);
          const g = Math.round(g0 + (g1 - g0) * frac);
          const b = Math.round(b0 + (b1 - b0) * frac);
          return `rgb(${{r}}, ${{g}}, ${{b}})`;
        }}
      }}
      return "rgb(255, 210, 63)";
    }}

    // Draws every currently-known solar cell as a square centered on its
    // own (lat, lon), sized from SOLAR_MAP_CELL_SIZE_DEG via the SAME
    // projection currently in effect for every other marker -- inserted
    // as the new first child of the <svg> (i.e. the bottom-most layer,
    // behind the waypoint/route lines and every point marker already
    // drawn by the time this runs, see redrawMap() below). Color is
    // relative to THIS batch's own min/max avg_pv_power (same
    // "normalize to what's actually on screen" spirit as
    // drawMapScaleBar()'s "nice number" picking above), since the
    // panel's absolute wattage rating isn't known here.
    const SOLAR_MAP_CELL_SIZE_DEG = 0.000045; // must match link/solar_map.py's own constant

    function drawSolarCells(project) {{
      if (!solarMapEnabled || solarCells.length === 0) return;
      const powers = solarCells.map((c) => c.avg_pv_power);
      const minP = Math.min(...powers), maxP = Math.max(...powers);
      const span = (maxP - minP) || 1;

      const group = document.createElementNS(MAP_NS, "g");
      group.setAttribute("opacity", "0.78");
      for (const cell of solarCells) {{
        const [cx, cy] = project(cell.lat, cell.lon);
        const [cx2, cy2] = project(
          cell.lat + SOLAR_MAP_CELL_SIZE_DEG, cell.lon + SOLAR_MAP_CELL_SIZE_DEG
        );
        const w = Math.abs(cx2 - cx) || 2, h = Math.abs(cy2 - cy) || 2;
        const rect = document.createElementNS(MAP_NS, "rect");
        rect.setAttribute("x", cx - w / 2);
        rect.setAttribute("y", cy - h / 2);
        rect.setAttribute("width", w);
        rect.setAttribute("height", h);
        rect.setAttribute("fill", solarColorForRatio((cell.avg_pv_power - minP) / span));
        group.appendChild(rect);
      }}
      // Inserted before whatever is currently the first child (the
      // waypoint/route lines, drawn earlier in redrawMap()) so the
      // whole overlay sits at the very back -- see this function's own
      // comment above.
      gpsMapSvg.insertBefore(group, gpsMapSvg.firstChild);
    }}

    function updateSolarLegendNote() {{
      if (!solarMapEnabled || solarCells.length === 0) {{
        solarLegend.hidden = true;
        return;
      }}
      const totalSamples = solarCells.reduce((sum, c) => sum + c.sample_count, 0);
      solarLegendNote.textContent =
        `(avg PV power per ~5m cell, ${{solarCells.length}} cell` +
        `${{solarCells.length === 1 ? "" : "s"}}, ${{totalSamples}} point` +
        `${{totalSamples === 1 ? "" : "s"}})`;
      solarLegend.hidden = false;
    }}

    async function pollSolarMap() {{
      if (!solarMapEnabled || solarPollBusy) return;
      solarPollBusy = true;
      try {{
        const res = await fetch("/api/solar_map");
        const data = await res.json();
        if (data.ok) {{
          solarCells = data.cells || [];
          updateSolarLegendNote();
          redrawMap();
        }}
      }} catch (err) {{
        // Silent on failure, same spirit as pollMap() above -- the
        // overlay just keeps showing whatever it last had.
      }} finally {{
        solarPollBusy = false;
      }}
    }}

    solarMapToggle.addEventListener("change", () => {{
      solarMapEnabled = solarMapToggle.checked;
      if (solarMapEnabled) {{
        pollSolarMap(); // immediate fetch, don't wait for the next interval tick
      }} else {{
        solarLegend.hidden = true;
        redrawMap();
      }}
    }});
    // The grid only changes on Pi #1's own 5-minute logging tick (and
    // only while it's idle, see link/power_history.py's
    // PowerHistoryLogger._log_once()) -- a slower poll than pollMap()'s
    // 5s is plenty, and pollSolarMap() itself is a no-op whenever the
    // checkbox is off, so this never fires an unnecessary request.
    setInterval(pollSolarMap, 30000);

    // ---- Waypoint-return / GPS-driving-route progress bar (2026-10-07) ----
    // Reuses haversineMeters/nmeaToDecimal/decodeLatLonList above and is
    // fed from pollMap()'s own GRT+STA fetches (no separate polling loop).
    const returnProgress = document.getElementById("returnProgress");
    const returnModeTitle = document.getElementById("returnModeTitle");
    const returnStatLine = document.getElementById("returnStatLine");
    const returnTrackFill = document.getElementById("returnTrackFill");
    const returnPointsLayer = document.getElementById("returnPointsLayer");
    const returnLegendSwatch = document.getElementById("returnLegendSwatch");
    const returnLegendPointLabel = document.getElementById("returnLegendPointLabel");

    const RETURN_MODE_TEXT = {{
      RETURN: {{title: "Waypoint return (BTN_A)", legend: "Remaining waypoint", swatch: "blue"}},
      DRIVE: {{title: "GPS driving route", legend: "Remaining route point", swatch: "red"}},
    }};

    // Below this speed the robot is effectively stopped -- showing an ETA
    // would mean dividing by (near) zero, so an honest placeholder is
    // shown instead (same convention as "GPS unavailable" / "No target
    // (NAV)" elsewhere on this page).
    const RETURN_MIN_MOVING_SPEED_KMH = 0.05;

    function etaFor(distanceMeters, speedKmh) {{
      if (!(speedKmh > RETURN_MIN_MOVING_SPEED_KMH)) return "ETA unavailable (stopped)";
      const speedMs = (speedKmh * 1000) / 3600;
      const totalSeconds = distanceMeters / speedMs;
      if (totalSeconds < 60) return "~" + Math.round(totalSeconds) + " s";
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = Math.round(totalSeconds % 60);
      return "~" + minutes + " min" + (seconds > 0 ? " " + seconds + " s" : "");
    }}

    // remainingPoints: the tail of GRT's route (route[route_index:], see
    // link/robot_state.py's get_route_progress()) -- points already
    // behind the robot are NOT shown here (they still exist in GRT's
    // full list so the map's own markers are unaffected, see pollMap()
    // below). routeIndex is each remaining point's position in that FULL
    // list, used only to label intermediate points the same way the map
    // already does ("Waypoint 3" / "GPS Driving 5"), so the two views
    // cross-reference. mode is GRT's trailing "RETURN"/"DRIVE" field.
    function updateReturnProgress(remainingPoints, routeIndex, mode, robotLat, robotLon, speedKmh) {{
      if (!remainingPoints.length || robotLat === null || robotLon === null) {{
        returnProgress.hidden = true;
        return;
      }}

      const modeText = RETURN_MODE_TEXT[mode] || RETURN_MODE_TEXT.DRIVE;
      returnProgress.hidden = false;
      returnProgress.classList.toggle("mode-route", mode !== "RETURN");
      returnModeTitle.textContent = modeText.title;
      returnLegendPointLabel.textContent = modeText.legend;
      returnLegendSwatch.className = "swatch " + modeText.swatch;

      // Cumulative REAL distance (haversine) from the robot's current
      // position, through each remaining point in order -- deliberately
      // not spaced evenly by count, which would misrepresent legs of very
      // different lengths.
      let prevLat = robotLat, prevLon = robotLon;
      let cumulative = 0;
      const withCumulative = remainingPoints.map((p, i) => {{
        cumulative += haversineMeters(prevLat, prevLon, p.lat, p.lon);
        prevLat = p.lat; prevLon = p.lon;
        return {{lat: p.lat, lon: p.lon, cumulative, originalIndex: routeIndex + i}};
      }});
      const total = withCumulative[withCumulative.length - 1].cumulative;

      returnPointsLayer.innerHTML = "";
      withCumulative.forEach((p, i) => {{
        const isTarget = i === withCumulative.length - 1;
        const pct = total > 0 ? (p.cumulative / total) * 100 : 100;
        // Intermediate points keep the map's own numbering (original
        // route index + 1); the last point in the full route is always
        // "Target", whichever mode this is.
        const pointLabel = isTarget
          ? "Target"
          : (mode === "RETURN" ? "Waypoint " + (p.originalIndex + 1) : "GPS Driving " + (p.originalIndex + 1));

        const dot = document.createElement("div");
        dot.className = "return-point" + (isTarget ? " target" : "");
        dot.style.left = pct + "%";
        const remainingAfter = total - p.cumulative;
        const line1 = pointLabel + " · " + p.cumulative.toFixed(0) + " m from here" +
          (remainingAfter > 0.5 ? " · " + remainingAfter.toFixed(0) + " m to go after" : " · arrives at target");
        const line2 = "ETA " + etaFor(p.cumulative, speedKmh) +
          (speedKmh > RETURN_MIN_MOVING_SPEED_KMH ? " at " + speedKmh + " km/h" : "");
        dot.setAttribute("data-tip", line1 + "\\n" + line2);
        returnPointsLayer.appendChild(dot);

        const label = document.createElement("div");
        label.className = "return-point-label";
        label.style.left = pct + "%";
        label.innerHTML = '<span class="idx">' + pointLabel + "</span><br>" + p.cumulative.toFixed(0) + " m";
        returnPointsLayer.appendChild(label);
      }});

      // The marker/fill always start at 0% -- see the .return-current
      // CSS comment above: each poll is a fresh snapshot "from here", not
      // an animation of the robot's actual movement.
      returnTrackFill.style.width = "0%";

      const remainingCount = withCumulative.length;
      returnStatLine.innerHTML = total.toFixed(0) + ' m remaining <span class="muted">· ' +
        remainingCount + " point" + (remainingCount === 1 ? "" : "s") + " left</span>";
    }}
    // Fixed margin/usable area, in the same MAP_VIEW_SIZE x MAP_VIEW_SIZE
    // units as the <svg>'s own (unchanging) viewBox -- see
    // projectorsFromBounds() below for why zoom never touches the
    // viewBox itself, only which lat/lon rectangle maps onto it.
    const MAP_MARGIN = MAP_VIEW_SIZE * 0.08;
    const MAP_USABLE = MAP_VIEW_SIZE - MAP_MARGIN * 2;
    const MAP_MIN_SPAN_DEG = 0.00004; // roughly 4m at mid-latitudes -- deepest zoom-in allowed

    // Current lat/lon rectangle the map displays. Auto-fit to whatever
    // points are currently known UNLESS the user has manually zoomed
    // (mapUserZoomed) -- see redrawMap()/the wheel handler below. Reset
    // to auto-fit again on a double-click.
    let mapBounds = null;
    let mapUserZoomed = false;
    let mapLastPoints = [];
    let mapLastWaypointPts = [];
    // The robot's own last-known position (from STA), kept outside
    // pollMap()'s own scope so showMapTooltip() can compute "distance to
    // robot" for a hovered marker -- null whenever there's no live fix,
    // so the tooltip can say so rather than show a stale distance.
    let lastRobotLatLon = null;

    // Computes the auto-fit bounding box of `points`' own lat/lon values,
    // with a small fixed-size padding on a degenerate (0/1 distinct
    // position) box instead of a zero-size span, which would otherwise
    // divide by zero.
    function computeBounds(points) {{
      const lats = points.map((p) => p.lat);
      const lons = points.map((p) => p.lon);
      let minLat = Math.min(...lats), maxLat = Math.max(...lats);
      let minLon = Math.min(...lons), maxLon = Math.max(...lons);
      const PAD_DEG = 0.0002; // roughly 20m at mid-latitudes
      if (maxLat - minLat < PAD_DEG) {{ minLat -= PAD_DEG; maxLat += PAD_DEG; }}
      if (maxLon - minLon < PAD_DEG) {{ minLon -= PAD_DEG; maxLon += PAD_DEG; }}
      return {{minLat, maxLat, minLon, maxLon}};
    }}

    // Builds the (lat, lon) <-> [x, y] projection for one bounds
    // rectangle, in MAP_VIEW_SIZE x MAP_VIEW_SIZE viewBox units. Mouse-
    // wheel zoom (see below) works entirely by choosing a SMALLER (or
    // bigger) `bounds` rectangle to map onto the same fixed 0..400
    // square -- the <svg>'s own viewBox attribute never changes, which
    // keeps this simple and keeps every existing screen-space assumption
    // (margins, hit areas, tooltip positioning) valid at any zoom level.
    function projectorsFromBounds(bounds) {{
      const {{minLat, maxLat, minLon, maxLon}} = bounds;
      const latSpan = (maxLat - minLat) || MAP_MIN_SPAN_DEG;
      const lonSpan = (maxLon - minLon) || MAP_MIN_SPAN_DEG;
      const project = (lat, lon) => [
        MAP_MARGIN + ((lon - minLon) / lonSpan) * MAP_USABLE,
        // Latitude increases northward, SVG y increases downward -- flip.
        MAP_MARGIN + (1 - (lat - minLat) / latSpan) * MAP_USABLE,
      ];
      const invert = (x, y) => [
        minLat + (1 - (y - MAP_MARGIN) / MAP_USABLE) * latSpan,
        minLon + ((x - MAP_MARGIN) / MAP_USABLE) * lonSpan,
      ];
      return {{project, invert}};
    }}

    function clearMap() {{
      while (gpsMapSvg.firstChild) gpsMapSvg.removeChild(gpsMapSvg.firstChild);
    }}

    function hideMapTooltip() {{
      mapTooltip.hidden = true;
      mapTooltip.innerHTML = "";
    }}

    function positionMapTooltip(evt) {{
      const wrap = gpsMapSvg.closest(".map-wrap").getBoundingClientRect();
      mapTooltip.style.left = (evt.clientX - wrap.left + 14) + "px";
      mapTooltip.style.top = (evt.clientY - wrap.top + 14) + "px";
    }}

    function formatMeters(d) {{
      return d < 1000 ? `${{d.toFixed(1)}} m` : `${{(d / 1000).toFixed(2)}} km`;
    }}

    function showMapTooltip(evt, data) {{
      let html = `<strong>${{escapeHtml(data.label)}}</strong><br>${{data.lat.toFixed(5)}}°, ${{data.lon.toFixed(5)}}°`;
      // Distance-to-robot (2026-10-05 explicit user request): every
      // marker except the robot's own (nothing to measure) and the
      // violet photo/video markers (which show a thumbnail instead, see
      // below) gets its live distance to the robot's current position.
      if (!data.media && data.label !== "Robot") {{
        if (lastRobotLatLon) {{
          const d = haversineMeters(lastRobotLatLon.lat, lastRobotLatLon.lon, data.lat, data.lon);
          html += `<br>Distance to robot: ${{formatMeters(d)}}`;
        }} else {{
          html += "<br>Distance to robot: unavailable (no GPS fix)";
        }}
      }}
      if (data.ts) {{
        html += `<br>${{new Date(data.ts * 1000).toLocaleString()}}`;
      }} else if (data.noTimestamp) {{
        html += "<br>no timestamp available";
      }}
      if (data.deleteAction) {{
        html += '<br><span class="map-tooltip-note">right-click to delete</span>';
      }}
      mapTooltip.innerHTML = html;
      mapTooltip.hidden = false;
      if (data.media) {{
        // Lazy: only actually downloaded from Pi #1 once hovered (see
        // /media/thumb/<kind>/<filename> -- cached in a tmp folder on
        // Pi #2 for the rest of this page view, cleared on next reload).
        const isVideo = data.media.kind === "video";
        const thumb = document.createElement(isVideo ? "video" : "img");
        thumb.className = "map-tooltip-thumb";
        thumb.src = `/media/thumb/${{data.media.kind}}/${{encodeURIComponent(data.media.filename)}}`;
        if (isVideo) {{
          thumb.muted = true;
          thumb.preload = "metadata";
          // Explicit user request: never actually play the video here --
          // seeking to a tiny offset once metadata loads renders that one
          // frame as a static image, with .play() never called.
          thumb.addEventListener("loadedmetadata", () => {{ thumb.currentTime = 0.1; }});
        }}
        thumb.addEventListener("error", () => {{
          const note = document.createElement("div");
          note.className = "map-tooltip-note";
          note.textContent = "file no longer available (rotated out on Pi #1)";
          mapTooltip.appendChild(note);
        }});
        mapTooltip.appendChild(thumb);
      }}
      positionMapTooltip(evt);
    }}

    // Right-click delete (2026-10-05 explicit user request, extended
    // 2026-10-06 to cover every marker colour): blue waypoints, red
    // route/photos-videos... every marker except the robot's own live
    // position now carries a `deleteAction` (see pollMap() below) --
    // there's nothing meaningful to delete the robot's own position FROM,
    // so that one alone still falls through to the browser's normal
    // context menu.
    async function deleteWaypointPoint(index) {{
      const res = await sendToRobot(`WPD,${{index}}`);
      if (res.ok) pollMap();
    }}

    async function deleteMediaPoint(filename, kind) {{
      const what = kind === "video" ? "this video" : "this photo";
      const ok = confirm(
        `Delete ${{what}} (${{filename}})? This removes it from the robot's camera ` +
        "buffer (and its saved position) -- it cannot be undone."
      );
      if (!ok) return;
      const wireKind = kind === "video" ? "VID" : "SNAP";
      const res = await sendToRobot(`MDD,${{filename}},${{wireKind}}`);
      if (res.ok) pollMap();
    }}

    // Red "GPS Driving" points (2026-10-06): backed by the robot's own
    // live route, same non-destructive "edits in-memory navigation state,
    // nothing on disk" spirit as WPD above -- no confirmation needed, same
    // as WPD, see pages/protocole_controle.html's RTD row.
    async function deleteRoutePoint(index) {{
      const res = await sendToRobot(`RTD,${{index}}`);
      if (res.ok) pollMap();
    }}

    // Red "NAV sent" point (2026-10-06): unlike every other marker, this
    // one has no backing store on the robot at all -- it's purely this
    // browser tab's own memory of the last NAV it sent (see lastNavSent
    // above), already gone the moment the page reloads. "Deleting" it is
    // therefore just forgetting it locally and redrawing -- nothing to
    // send the robot (a NAV target it already has stays active until a
    // fresh NAV/RTE/STP replaces it, exactly as before this marker
    // existed; this only stops the page from still showing an old one).
    function deleteNavSentPoint() {{
      lastNavSent = null;
      pollMap();
    }}

    function addMapCircle(x, y, radius, data) {{
      const circle = document.createElementNS(MAP_NS, "circle");
      circle.setAttribute("cx", x);
      circle.setAttribute("cy", y);
      circle.setAttribute("r", radius);
      circle.setAttribute("fill", data.color);
      circle.setAttribute("stroke", "#1b1f24");
      circle.setAttribute("stroke-width", "1");
      circle.style.cursor = "pointer";
      circle.addEventListener("mouseenter", (evt) => showMapTooltip(evt, data));
      circle.addEventListener("mousemove", positionMapTooltip);
      circle.addEventListener("mouseleave", hideMapTooltip);
      if (data.deleteAction) {{
        circle.addEventListener("contextmenu", (evt) => {{
          evt.preventDefault();
          hideMapTooltip();
          if (data.deleteAction.type === "waypoint") {{
            deleteWaypointPoint(data.deleteAction.index);
          }} else if (data.deleteAction.type === "media") {{
            deleteMediaPoint(data.deleteAction.filename, data.deleteAction.kind);
          }} else if (data.deleteAction.type === "route") {{
            deleteRoutePoint(data.deleteAction.index);
          }} else if (data.deleteAction.type === "navSent") {{
            deleteNavSentPoint();
          }}
        }});
      }}
      gpsMapSvg.appendChild(circle);
    }}

    function addMapLine(x1, y1, x2, y2, color) {{
      const line = document.createElementNS(MAP_NS, "line");
      line.setAttribute("x1", x1);
      line.setAttribute("y1", y1);
      line.setAttribute("x2", x2);
      line.setAttribute("y2", y2);
      line.setAttribute("stroke", color);
      line.setAttribute("stroke-width", "1.5");
      line.setAttribute("stroke-opacity", "0.6");
      gpsMapSvg.insertBefore(line, gpsMapSvg.firstChild); // behind every point marker
    }}

    // Distance scale bar (2026-10-05 explicit user request), bottom-left,
    // in fixed SVG viewBox units (so it never has to deal with the CSS
    // pixel size the <svg> happens to be rendered at). Standard map
    // scale-bar algorithm: measure how many real meters the *maximum*
    // bar width currently represents (via the live projection's own
    // invert()), then pick the largest "nice" round number of meters
    // that still fits in that width, and draw the bar at the (shorter,
    // exact) pixel length THAT nice number actually needs.
    const MAP_SCALE_BAR_MAX_UNITS = 80;
    const MAP_SCALE_NICE_METERS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];

    function drawMapScaleBar(invert) {{
      const x0 = 14, x1 = x0 + MAP_SCALE_BAR_MAX_UNITS, y = MAP_VIEW_SIZE - 14;
      const [latA, lonA] = invert(x0, y);
      const [latB, lonB] = invert(x1, y);
      const metersForMaxWidth = haversineMeters(latA, lonA, latB, lonB);
      if (!isFinite(metersForMaxWidth) || metersForMaxWidth <= 0) return;
      const metersPerUnit = metersForMaxWidth / MAP_SCALE_BAR_MAX_UNITS;

      let niceMeters = MAP_SCALE_NICE_METERS[0];
      for (const m of MAP_SCALE_NICE_METERS) {{
        if (m / metersPerUnit <= MAP_SCALE_BAR_MAX_UNITS) niceMeters = m;
        else break;
      }}
      const barWidthUnits = niceMeters / metersPerUnit;

      const group = document.createElementNS(MAP_NS, "g");
      group.setAttribute("class", "map-scale-bar");
      const line = document.createElementNS(MAP_NS, "line");
      line.setAttribute("x1", x0);
      line.setAttribute("x2", x0 + barWidthUnits);
      line.setAttribute("y1", y);
      line.setAttribute("y2", y);
      group.appendChild(line);
      for (const tx of [x0, x0 + barWidthUnits]) {{
        const tick = document.createElementNS(MAP_NS, "line");
        tick.setAttribute("x1", tx);
        tick.setAttribute("x2", tx);
        tick.setAttribute("y1", y - 3);
        tick.setAttribute("y2", y + 3);
        group.appendChild(tick);
      }}
      const label = document.createElementNS(MAP_NS, "text");
      label.setAttribute("x", x0);
      label.setAttribute("y", y - 6);
      label.textContent = niceMeters >= 1000
        ? `${{(niceMeters / 1000).toFixed(niceMeters % 1000 === 0 ? 0 : 1)}} km`
        : `${{niceMeters}} m`;
      group.appendChild(label);
      gpsMapSvg.appendChild(group);
    }}

    // Redraws every currently-known point (mapLastPoints/
    // mapLastWaypointPts, refreshed by pollMap() every 5s) against
    // mapBounds -- called both after a fresh poll and immediately after
    // a wheel-zoom/double-click reset, so zooming feels instant rather
    // than waiting for the next network round-trip.
    function redrawMap() {{
      const points = mapLastPoints;
      clearMap();
      if (points.length === 0) return;
      if (!mapUserZoomed || !mapBounds) {{
        mapBounds = computeBounds(points);
      }}
      const {{project, invert}} = projectorsFromBounds(mapBounds);

      for (let i = 0; i + 1 < mapLastWaypointPts.length; i++) {{
        const [x1, y1] = project(mapLastWaypointPts[i].lat, mapLastWaypointPts[i].lon);
        const [x2, y2] = project(mapLastWaypointPts[i + 1].lat, mapLastWaypointPts[i + 1].lon);
        addMapLine(x1, y1, x2, y2, MAP_COLORS.waypoint);
      }}

      for (const p of points) {{
        const [x, y] = project(p.lat, p.lon);
        addMapCircle(x, y, p.radius, p);
      }}

      // Inserted last but pushed to the very back of the <svg> (see its
      // own comment) -- so the overlay never hides a waypoint/route
      // line or a marker, only the plain grid background beneath them.
      drawSolarCells(project);

      drawMapScaleBar(invert);
    }}

    // Mouse-wheel zoom (2026-10-05 explicit user request), targeting the
    // CURSOR's position, not the robot's: shrinks (or grows) mapBounds
    // around the lat/lon currently under the pointer, keeping that exact
    // point under the pointer on screen before and after. Once the user
    // has zoomed at all, pollMap()'s auto-fit stops moving the view out
    // from under them (see redrawMap() above) -- double-click resets it.
    gpsMapSvg.addEventListener("wheel", (evt) => {{
      if (!mapBounds) return;
      evt.preventDefault();
      const rect = gpsMapSvg.getBoundingClientRect();
      const svgX = ((evt.clientX - rect.left) / rect.width) * MAP_VIEW_SIZE;
      const svgY = ((evt.clientY - rect.top) / rect.height) * MAP_VIEW_SIZE;
      const {{invert}} = projectorsFromBounds(mapBounds);
      const [curLat, curLon] = invert(svgX, svgY);

      const ZOOM_STEP = 1.25;
      const zoomingIn = evt.deltaY < 0;
      const factor = zoomingIn ? 1 / ZOOM_STEP : ZOOM_STEP;
      const latSpan = mapBounds.maxLat - mapBounds.minLat;
      const lonSpan = mapBounds.maxLon - mapBounds.minLon;
      const newLatSpan = Math.max(MAP_MIN_SPAN_DEG, latSpan * factor);
      const newLonSpan = Math.max(MAP_MIN_SPAN_DEG, lonSpan * factor);
      if (newLatSpan === latSpan && newLonSpan === lonSpan) return; // already at the zoom-in floor

      // The cursor's point sat at fraction (fracX, fracYFromTop) of the
      // usable area before -- keep it at that same fraction after the
      // span changes (same derivation as projectorsFromBounds()'s own
      // project()/invert(), solved for the new bounds).
      const fracX = (svgX - MAP_MARGIN) / MAP_USABLE;
      const fracYFromTop = (svgY - MAP_MARGIN) / MAP_USABLE;
      const newMinLon = curLon - fracX * newLonSpan;
      const newMaxLat = curLat + fracYFromTop * newLatSpan;
      mapBounds = {{
        minLon: newMinLon, maxLon: newMinLon + newLonSpan,
        maxLat: newMaxLat, minLat: newMaxLat - newLatSpan,
      }};
      mapUserZoomed = true;
      redrawMap();
    }}, {{passive: false}});

    gpsMapSvg.addEventListener("dblclick", () => {{
      mapUserZoomed = false;
      redrawMap();
    }});

    // Decodes WPT/GRT's shared "count, then count*4 fields
    // (lat,lat_dir,lon,lon_dir)" shape into plain {{lat, lon}} pairs.
    function decodeLatLonList(fields) {{
      const out = [];
      if (!fields || fields.length < 1) return out;
      const count = parseInt(fields[0], 10) || 0;
      for (let i = 0; i < count; i++) {{
        const [lat, latDir, lon, lonDir] = fields.slice(1 + i * 4, 5 + i * 4);
        const dLat = nmeaToDecimal(lat, latDir);
        const dLon = nmeaToDecimal(lon, lonDir);
        if (dLat !== null) out.push({{lat: dLat, lon: dLon}});
      }}
      return out;
    }}

    let mapPollBusy = false;
    async function pollMap() {{
      if (mapPollBusy) return;
      mapPollBusy = true;
      try {{
        const [staData, wptData, grtData, medData] = await Promise.all([
          sendToRobot("STA"), sendToRobot("WPT"), sendToRobot("GRT"), sendToRobot("MED"),
        ]);

        const points = [];
        let robotFixOk = false;
        let robotLatForProgress = null, robotLonForProgress = null;
        // STA field order (see pollStatus() above/link/server.py): speed
        // is fields[5] -- pollStatus()'s own parsed copy is out of scope
        // here, hence this second, independent read from pollMap()'s own
        // STA fetch for the progress bar's ETA.
        let speedKmhForProgress = 0;

        if (staData.ok && staData.fields && staData.fields.length >= 4) {{
          const [lat, latDir, lon, lonDir] = staData.fields;
          const curLat = nmeaToDecimal(lat, latDir);
          const curLon = nmeaToDecimal(lon, lonDir);
          if (curLat !== null && (curLat !== 0 || curLon !== 0)) {{
            points.push({{lat: curLat, lon: curLon, color: MAP_COLORS.robot, radius: 6, label: "Robot"}});
            lastRobotLatLon = {{lat: curLat, lon: curLon}};
            robotFixOk = true;
            robotLatForProgress = curLat;
            robotLonForProgress = curLon;
          }}
          if (staData.fields.length >= 6) {{
            speedKmhForProgress = parseFloat(staData.fields[5]) || 0;
          }}
        }}
        if (!robotFixOk) lastRobotLatLon = null;

        const waypointPts = wptData.ok ? decodeLatLonList(wptData.fields) : [];
        waypointPts.forEach((p, i) => {{
          points.push({{
            lat: p.lat, lon: p.lon, color: MAP_COLORS.waypoint, radius: 4,
            label: `Waypoint ${{i + 1}}`, noTimestamp: true,
            deleteAction: {{type: "waypoint", index: i}},
          }});
        }});

        // GRT's two trailing fields (route_index, "RETURN"|"DRIVE" --
        // 2026-10-07, see link/server.py's GRT handler) feed the progress
        // bar below; decodeLatLonList() only reads the leading
        // count+4*count fields so it still returns the FULL route
        // unaffected (the map's own markers below are unchanged).
        let grtRouteIndex = 0;
        let grtMode = "DRIVE";
        let grtFullRoute = [];
        if (grtData.ok) {{
          grtFullRoute = decodeLatLonList(grtData.fields);
          const trailingStart = 1 + grtFullRoute.length * 4;
          if (grtData.fields.length >= trailingStart + 2) {{
            grtRouteIndex = parseInt(grtData.fields[trailingStart], 10) || 0;
            grtMode = grtData.fields[trailingStart + 1] === "RETURN" ? "RETURN" : "DRIVE";
          }}
          grtFullRoute.forEach((p, i) => {{
            points.push({{
              lat: p.lat, lon: p.lon, color: MAP_COLORS.route, radius: 4,
              label: `GPS Driving ${{i + 1}}`, noTimestamp: true,
              deleteAction: {{type: "route", index: i}},
            }});
          }});
        }}
        updateReturnProgress(
          grtFullRoute.slice(grtRouteIndex), grtRouteIndex, grtMode,
          robotLatForProgress, robotLonForProgress, speedKmhForProgress
        );

        if (lastNavSent) {{
          points.push({{
            lat: lastNavSent.lat, lon: lastNavSent.lon, color: MAP_COLORS.route, radius: 5,
            label: "NAV sent", ts: lastNavSent.sentAt,
            deleteAction: {{type: "navSent"}},
          }});
        }}

        if (medData.ok && medData.fields && medData.fields.length >= 1) {{
          const count = parseInt(medData.fields[0], 10) || 0;
          for (let i = 0; i < count; i++) {{
            const [filename, kind, lat, latDir, lon, lonDir, ts] = medData.fields.slice(1 + i * 7, 8 + i * 7);
            const dLat = nmeaToDecimal(lat, latDir);
            const dLon = nmeaToDecimal(lon, lonDir);
            if (dLat === null) continue;
            const mediaKind = kind === "VID" ? "video" : "photo";
            points.push({{
              lat: dLat, lon: dLon, color: MAP_COLORS.media, radius: 4,
              label: kind === "VID" ? "Video" : "Photo",
              ts: parseInt(ts, 10) || null,
              media: {{filename, kind: mediaKind}},
              deleteAction: {{type: "media", filename, kind: mediaKind}},
            }});
          }}
        }}

        mapLastPoints = points;
        mapLastWaypointPts = waypointPts;
        redrawMap();
      }} catch (err) {{
        // Silent on failure, same spirit as pollStatus() above.
      }} finally {{
        mapPollBusy = false;
      }}
    }}
    pollMap();
    setInterval(pollMap, 5000);

    // Controls panel: one full-width button per command a human actually
    // needs a shortcut for (see pages/protocole_controle.html for the
    // full protocol reference -- DRV, MOD, PID and STA are all still
    // valid sentences over the wire and typeable in the console below,
    // they just no longer get their own quick-fill button here, see the
    // 2026-09-12 redesign notes there). Order matters: this is the order
    // they're actually needed in, top to bottom -- set a target, arm
    // driving to it, check the camera, and STOP always last/lowest so it
    // never moves under a moving thumb.
    //
    // Most of these buttons never send anything by themselves -- clicking
    // just fills the console input with that sentence's beginning (type
    // plus a trailing comma) so the specific values can be typed in before
    // pressing Enter; the label shows a complete example of what to fill
    // in. Three exceptions (action instead of prefix):
    // - "GPS Driving" (was "GPS route"): a route can be arbitrarily long,
    //   so there's no reasonable text to prefill -- it opens a file picker
    //   instead and sends a whole RTE sentence built from the file's
    //   content once one is chosen (see gpsRouteInput below).
    // - "Distance + angle" (2026-09-12): drops down a small inline form
    //   (see buildDistanceAnglePanel below) instead of a file picker or a
    //   console prefix -- it fetches the robot's current position and
    //   heading (STA), computes the GPS point that many meters away at
    //   (current heading + the entered angle), and sends that point as a
    //   plain NAV -- no new sentence type needed, this just automates
    //   picking the NAV,lat,lon fields instead of typing them by hand.
    //   Placed directly above STOP (second-to-last) at the user's request.
    // - "STOP" (was "STP"): an emergency stop must not wait on a second
    //   keystroke -- clicking it sends STP immediately, no console step at
    //   all (see the "stop" action branch below).
    const commandButtons = [
      {{label: "NAV,4723.492,N,00044.340,W",    prefix: "NAV,"}},
      {{label: "GPS Driving",                   action: "gpsRoute"}},
      {{label: "CAM,SNAP",                      prefix: "CAM,"}},
      {{label: "Distance + angle",              action: "distanceAngle"}},
      {{label: "STOP",                          action: "stop"}},
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

    // Catches the single most common manual-console mistake (2026-10-07):
    // typing a NAV/RTE point in plain decimal degrees -- e.g. copy-pasted
    // straight from this very page's own "current"/"target" status display
    // a few lines below (which IS decimal degrees), or from a map -- where
    // this protocol's ddmm.mmmm wire format is actually expected (see the
    // "NAV,4723.492,N,..." button label above). Before this existed, that
    // silently sent the wrong point: nmeaToDecimal() can't tell "a valid-
    // looking but wrong value" from a genuine one, so e.g. "47.391534,N"
    // typed in place of "4723.492,N" for the SAME intended latitude got
    // sent as-is and decoded by the robot as roughly 0.79 degrees North --
    // ~45x off -- with no error anywhere, only a suspiciously-wrong
    // "target" reading afterward to notice by.
    //
    // Deliberately NOT a "this must be somewhere near France" geography
    // check -- the button above's own example and this protocol's docs use
    // points from all over (see protocole_controle.html), and a geography
    // box would wrongly "fix" a genuine faraway point. Instead this uses a
    // fact that's true everywhere: a GENUINE ddmm.mmmm latitude's raw
    // numeric value is at least "degree * 100", so for any site more than
    // roughly half a degree from the equator the raw value is comfortably
    // >= 50 -- a raw magnitude below RAW_LAT_MAGNITUDE_FLOOR can only be a
    // plain decimal-degrees latitude (whose maximum possible magnitude is
    // 90). Longitude can't carry this same check on its own (near the
    // Greenwich meridian this protocol's own ddmm.mmmm longitude is
    // numerically small too, indistinguishable from decimal degrees by
    // magnitude alone), so the decision for a whole point is made from
    // latitude only and applied to both of its fields together. Mirrored
    // server-side in link/robot_state.py's own _point_is_plausible() (same
    // threshold, same reasoning) as a safety net for anything that skips
    // this website and talks to the robot's TCP port directly.
    const RAW_LAT_MAGNITUDE_FLOOR = 90.0;

    function normalizeGpsPoint(latRaw, latDir, lonRaw, lonDir) {{
      const latMagnitude = Math.abs(parseFloat(latRaw));
      if (!Number.isFinite(latMagnitude) || latMagnitude >= RAW_LAT_MAGNITUDE_FLOOR) {{
        return [latRaw, lonRaw];  // already looks like genuine ddmm.mmmm -- leave both as typed
      }}
      const lat = (latDir === "S") ? -latMagnitude : latMagnitude;
      const lonMagnitude = Math.abs(parseFloat(lonRaw));
      const lon = (lonDir === "W") ? -lonMagnitude : lonMagnitude;
      const [latStr] = decimalToNmea(lat, false);
      const [lonStr] = decimalToNmea(lon, true);
      return [latStr, lonStr];
    }}

    // Rewrites a console-typed NAV/RTE command in place, auto-converting
    // any point that looks like plain decimal degrees (see
    // normalizeGpsPoint() above) before it's ever sent to the robot.
    // Anything else (STP, DRV, CAM, PID, STA...) is returned unchanged.
    // Called from sendToRobot() below, so this covers every command this
    // console actually sends -- manually typed, or built by a button.
    function normalizeGpsCommand(command) {{
      const parts = command.split(",").map((p) => p.trim());
      const type = parts[0];

      if (type === "NAV" && parts.length === 5) {{
        const [latStr, lonStr] = normalizeGpsPoint(parts[1], parts[2].toUpperCase(), parts[3], parts[4].toUpperCase());
        return ["NAV", latStr, parts[2].toUpperCase(), lonStr, parts[4].toUpperCase()].join(",");
      }}

      if (type === "RTE" && parts.length > 1) {{
        const count = parseInt(parts[1], 10);
        if (Number.isFinite(count) && parts.length === 2 + count * 4) {{
          const out = ["RTE", parts[1]];
          for (let i = 0; i < count; i++) {{
            const base = 2 + i * 4;
            const latDir = parts[base + 1].toUpperCase();
            const lonDir = parts[base + 3].toUpperCase();
            const [latStr, lonStr] = normalizeGpsPoint(parts[base], latDir, parts[base + 2], lonDir);
            out.push(latStr, latDir, lonStr, lonDir);
          }}
          return out.join(",");
        }}
      }}

      return command;
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

    // Builds the small inline form that drops down under the "Distance +
    // angle" button when it's clicked: a distance (m) and a rotation
    // angle (°, relative to the robot's current heading -- the user's
    // explicit choice on 2026-09-12, over an absolute compass bearing)
    // input, plus a send button. Returns the panel <div> (still hidden,
    // see the "open" CSS class); the caller wires the toggle and appends
    // both the button and this panel to the DOM.
    function buildDistanceAnglePanel() {{
      const panel = document.createElement("div");
      panel.className = "cmd-distance-panel";

      const distLabel = document.createElement("label");
      distLabel.textContent = "Distance (m)";
      const distInput = document.createElement("input");
      distInput.type = "number";
      distInput.step = "0.1";
      distInput.min = "0";
      distInput.placeholder = "10";
      distLabel.appendChild(distInput);

      const angleLabel = document.createElement("label");
      angleLabel.textContent = "Angle (°, / cap actuel)";
      const angleInput = document.createElement("input");
      angleInput.type = "number";
      angleInput.step = "1";
      angleInput.placeholder = "0";
      angleLabel.appendChild(angleInput);

      const sendBtn = document.createElement("button");
      sendBtn.type = "button";
      sendBtn.textContent = "Envoyer";

      sendBtn.addEventListener("click", async () => {{
        const distance = parseFloat(distInput.value);
        const angle = parseFloat(angleInput.value);
        if (!Number.isFinite(distance) || distance <= 0) {{
          logLine("Distance + angle: entrer une distance en mètres (&gt; 0)", "err");
          return;
        }}
        if (!Number.isFinite(angle)) {{
          logLine("Distance + angle: entrer un angle de rotation en degrés", "err");
          return;
        }}

        // Always ask the robot for a fresh fix rather than reusing the
        // status bar's last poll (up to 3s stale) -- this command computes
        // a real target to drive to, so it deserves the robot's actual
        // position and heading right now, not a moment ago.
        const data = await sendToRobot("STA");
        if (!data.ok || !data.fields || data.fields.length < 14) {{
          logLine("Distance + angle: impossible de lire la position actuelle du robot (STA a échoué)", "err");
          return;
        }}
        const [lat, latDir, lon, lonDir, cap, speed] = data.fields;
        const curLat = nmeaToDecimal(lat, latDir);
        const curLon = nmeaToDecimal(lon, lonDir);
        if (curLat === null || (curLat === 0 && curLon === 0)) {{
          logLine("Distance + angle: pas de position GPS sur le robot actuellement", "err");
          return;
        }}

        const heading = parseFloat(cap) || 0;
        if (parseFloat(speed) === 0) {{
          // Documented limitation (protocole_controle.html): there's no
          // compass/IMU on this robot, so "cap" (course over ground) is
          // only meaningful while it's already moving -- at a standstill
          // it's stale/noisy. The angle is still applied on top of it (the
          // chosen design), but the user is warned so a target computed
          // from a meaningless heading isn't a silent surprise.
          logLine(
            "Distance + angle: le robot est à l'arrêt, son cap (" +
              heading.toFixed(0) +
              "°) peut ne pas être fiable — la cible est calculée à partir de lui quand même",
            "sys"
          );
        }}

        const targetBearing = ((heading + angle) % 360 + 360) % 360;
        const [destLat, destLon] = destinationPoint(curLat, curLon, distance, targetBearing);
        const [latStr, latDirOut] = decimalToNmea(destLat, false);
        const [lonStr, lonDirOut] = decimalToNmea(destLon, true);
        sendCommand("NAV," + latStr + "," + latDirOut + "," + lonStr + "," + lonDirOut);
        panel.classList.remove("open");
      }});

      panel.appendChild(distLabel);
      panel.appendChild(angleLabel);
      panel.appendChild(sendBtn);
      return panel;
    }}

    // controlsButtons doesn't exist for a read-only (viewer) session --
    // that panel shows a plain message instead, see _control_page().
    const controlsButtons = document.getElementById("controlsButtons");
    if (controlsButtons) {{
      for (const buttonDef of commandButtons) {{
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = buttonDef.label;
        if (buttonDef.action === "gpsRoute") {{
          btn.className = "cmd-button";
          btn.title = "Pick a text file with one 'lat,lon' GPS point per line";
          btn.addEventListener("click", () => gpsRouteInput.click());
        }} else if (buttonDef.action === "distanceAngle") {{
          btn.className = "cmd-button";
          btn.title = "Enter a distance and a rotation angle (relative to the robot's current heading) and send the computed GPS point as NAV";
          const panel = buildDistanceAnglePanel();
          btn.addEventListener("click", () => panel.classList.toggle("open"));
          controlsButtons.appendChild(btn);
          controlsButtons.appendChild(panel);
          continue;
        }} else if (buttonDef.action === "stop") {{
          // Emergency stop: sends STP the moment this is clicked, same as
          // pressing Enter on a typed "STP" would -- no console step, no
          // second click, nothing to type. Logged to the visible Console
          // tab (logToConsole: true) same as any command a person sends.
          btn.className = "cmd-button-danger";
          btn.title = "Immediately sends STP -- stops both motors right now";
          btn.addEventListener("click", () => sendCommand("STP"));
        }} else {{
          btn.className = "cmd-button";
          btn.addEventListener("click", () => {{
            input.value = buttonDef.prefix;
            input.focus();
            input.setSelectionRange(buttonDef.prefix.length, buttonDef.prefix.length);
          }});
        }}
        controlsButtons.appendChild(btn);
      }}
    }}

  </script>

</body>
</html>"""


def _media_page(videos=None, images=None, role="admin"):
    """Media page: video playlist (loops through media/videos/) + live
    camera feed (falls back to the playlist when the feed errors out) +
    images slideshow (media/images/, swapped every 3s). Split out of
    /control on 2026-10-05 (explicit user request -- the Media page/link
    had gone missing when /control's inline Video feed/Images panels were
    restored from a stale local copy, so it's rebuilt here as its own
    page rather than nested back inside /control) so /control stays
    focused on driving the robot.

    videos / images: filenames in media/videos/ and media/images/ (see
    _list_media above). role: "admin" or "viewer" -- same meaning as in
    _control_page, used only to show the read-only badge here since this
    page has no controls to disable.

    The live camera feed is proxied from Pi #1 through /media/camera (see
    that route below) into a hidden <img>. As soon as it loads, it's shown
    in place of the recorded video playlist/placeholder; if it errors out
    (robot's camera script not running, wrong IP...) the recorded
    playlist/placeholder is shown instead, and the camera is retried every
    few seconds in the background -- so the panel switches over
    automatically whenever the live feed becomes available, no reload
    needed."""
    videos = videos or []
    images = images or []
    is_viewer = role == "viewer"

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
  <title>Robot Media</title>
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
    }}

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

    .main-area {{
      flex: 1;
      display: flex;
      flex-direction: row;
      margin-top: 72px;
      min-height: 0;
      padding-bottom: 24px;
    }}

    .panel {{
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 20px;
      min-height: 0;
    }}
    .panel:first-child {{ border-right: 1px solid #30363d; }}

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
      min-height: 200px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
      color: #484f58;
      text-align: center;
      padding: 12px;
      line-height: 1.6;
    }}
    /* Bugfix (2026-10-06): the rule above sets its own `display: flex`,
       which beats the browser's built-in "[hidden] {{ display: none }}"
       default on specificity (two classes vs. one attribute) -- so toggling
       a placeholder's `.hidden` property from JS (see robotMediaPlaceholder
       below) silently did nothing, leaving it visible side-by-side with
       whatever was supposed to replace it (e.g. the Robot camera carousel's
       actual image), squeezing that element off-centre instead of letting
       it fill/centre in the box alone. Same fix as .camera-stream[hidden]
       right below already uses for the live feed image -- every other
       `.placeholder` toggled via JS needs this same explicit override. */
    .panel .placeholder[hidden] {{ display: none; }}

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
      min-height: 200px;
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

    /* Right column split in half, top/bottom (2026-10-05 explicit user
       request): Images (Pi #2's own slideshow, unchanged) keeps rotating
       in the top half; a new "Robot camera" carousel -- the robot's OWN
       5 photos + 5 videos (camera/stream_server.py on Pi #1) -- cycles in
       the bottom half. */
    .panel-split {{
      flex-direction: column;
      gap: 0;
    }}
    .sub-panel {{
      flex: 1 1 50%;
      min-height: 0;
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 10px 0;
    }}
    .sub-panel:first-child {{ border-bottom: 1px solid #30363d; }}
    .sub-panel h2 {{
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #8b949e;
      margin: 0 0 10px;
    }}

    .robot-media-area {{
      width: 100%;
      height: 100%;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .robot-media-img, .robot-media-video {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      object-fit: contain;
      background: #000;
    }}
  </style>
</head>
<body>

  <div class="nav">
    <a href="{url_for('control')}">Control</a>
    <a href="{url_for('power')}">Power</a>
    <a href="{url_for('pages_index')}">Pages</a>
    <a href="{url_for('logout')}">Log out</a>
    {'<span class="viewer-badge">Read-only access</span>' if is_viewer else ''}
  </div>
  <div class="clock" id="clock">--:--:--</div>

  <div class="main-area">
    <div class="panel">
      <h2>Video feed</h2>
      <div class="video-feed-area">
        <img class="camera-stream" id="cameraStream" alt="Live camera feed" hidden>
        <div id="recordedVideoArea">{video_html}</div>
      </div>
    </div>
    <div class="panel panel-split">
      <div class="sub-panel">
        <h2>Images</h2>
        {images_html}
      </div>
      <div class="sub-panel">
        <h2>Robot camera</h2>
        <div class="robot-media-area">
          <div class="placeholder" id="robotMediaPlaceholder">No photos/videos on the robot yet</div>
          <img class="robot-media-img" id="robotMediaImg" alt="Robot photo" hidden>
          <video class="robot-media-video" id="robotMediaVideo" muted playsinline hidden></video>
        </div>
      </div>
    </div>
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

    // Robot camera carousel (2026-10-05 explicit user request): cycles
    // through whatever's currently in the robot's own capped camera
    // buffers (up to 5 photos + up to 5 videos, see /media/robot_feed) --
    // photos first (3s each), then videos (autoplayed to completion),
    // looping back to the first photo once the last video ends. Entirely
    // separate from the Images slideshow above (Pi #2's own photos,
    // unchanged) -- that one keeps rotating independently in its own
    // half of this column; this is the robot's OWN photos/videos, in the
    // new bottom half.
    const robotMediaImg = document.getElementById("robotMediaImg");
    const robotMediaVideo = document.getElementById("robotMediaVideo");
    const robotMediaPlaceholder = document.getElementById("robotMediaPlaceholder");
    const ROBOT_MEDIA_PHOTO_MS = 3000;
    const ROBOT_MEDIA_REFRESH_MS = 30000; // how often the playlist itself is re-fetched from the robot
    let robotMediaItems = [];
    let robotMediaIndex = 0;
    let robotMediaTimer = null;

    function showRobotMediaItem(i) {{
      if (robotMediaItems.length === 0) return;
      clearTimeout(robotMediaTimer);
      const item = robotMediaItems[i];
      const url = `/media/thumb/${{item.kind}}/${{encodeURIComponent(item.filename)}}`;
      if (item.kind === "video") {{
        robotMediaImg.hidden = true;
        robotMediaVideo.hidden = false;
        robotMediaVideo.src = url;
        robotMediaVideo.currentTime = 0;
        robotMediaVideo.play().catch(() => {{
          // Autoplay blocked, or the file is no longer reachable -- don't
          // get stuck on it, just move on after the same duration a
          // photo would get.
          robotMediaTimer = setTimeout(advanceRobotMedia, ROBOT_MEDIA_PHOTO_MS);
        }});
      }} else {{
        robotMediaVideo.hidden = true;
        robotMediaVideo.pause();
        robotMediaImg.hidden = false;
        robotMediaImg.src = url;
        robotMediaTimer = setTimeout(advanceRobotMedia, ROBOT_MEDIA_PHOTO_MS);
      }}
    }}

    function advanceRobotMedia() {{
      if (robotMediaItems.length === 0) return;
      robotMediaIndex = (robotMediaIndex + 1) % robotMediaItems.length;
      showRobotMediaItem(robotMediaIndex);
    }}

    robotMediaVideo.addEventListener("ended", advanceRobotMedia);
    robotMediaVideo.addEventListener("error", advanceRobotMedia);
    robotMediaImg.addEventListener("error", advanceRobotMedia);

    async function refreshRobotMediaPlaylist() {{
      let data;
      try {{
        const res = await fetch("{url_for('media_robot_feed')}");
        data = await res.json();
      }} catch (err) {{
        return; // robot unreachable right now -- keep showing whatever's already playing
      }}
      if (!data.ok || !data.items || data.items.length === 0) {{
        if (robotMediaItems.length === 0) {{
          robotMediaPlaceholder.hidden = false;
          robotMediaImg.hidden = true;
          robotMediaVideo.hidden = true;
        }}
        return;
      }}
      const wasEmpty = robotMediaItems.length === 0;
      robotMediaItems = data.items;
      robotMediaPlaceholder.hidden = true;
      if (wasEmpty) {{
        robotMediaIndex = 0;
        showRobotMediaItem(0);
      }} else {{
        robotMediaIndex = Math.min(robotMediaIndex, robotMediaItems.length - 1);
      }}
    }}

    refreshRobotMediaPlaylist();
    setInterval(refreshRobotMediaPlaylist, ROBOT_MEDIA_REFRESH_MS);
  </script>

</body>
</html>"""


@app.route("/control")
@login_required
def control():
    _clear_media_thumb_cache()
    return _control_page(role=session.get("role", "admin"))


@app.route("/media")
@login_required
def media():
    videos = _list_media(VIDEOS_DIR, VIDEO_EXTENSIONS)
    images = _list_media(IMAGES_DIR, IMAGE_EXTENSIONS)
    return _media_page(videos, images, role=session.get("role", "admin"))


@app.route("/power")
@login_required
def power():
    """Dedicated solar/battery/load monitoring page (pages/power.html):
    polls the robot's PWR sentence through /api/send every few seconds,
    same read-only spirit as /control's status bar (see PWR in
    VIEWER_ALLOWED_COMMANDS above -- a viewer account can load this page
    too, it just can't reach anything that drives the robot). Served as a
    static file straight from pages/ (no f-string page like _control_page
    needed) since all of its state comes from client-side polling, same
    reasoning as serve_page() below for the other pages/ files."""
    return send_from_directory(PAGES_DIR, "power.html")


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


@app.route("/api/power_history")
@login_required
def api_power_history():
    """Read-only, like PWR/STA via /api/send above -- no role check needed,
    a viewer can see historical charts same as the live gauges. `period`
    is "day" or "month" (case-insensitive); fetch_power_history() loops
    over Pi #1's paginated HIS response (see power_history_client.py and
    link/power_history.py) and returns the assembled rows as JSON.

    2026-10-05: /power's history section charts battery_soc only for now
    (see pages/power.html) -- the other indicators are already being
    logged on Pi #1 and available in this same response, just not drawn
    yet; this endpoint intentionally returns every field rather than
    only battery_soc so a later chart can be added here without any
    backend change."""
    period = (request.args.get("period") or "day").strip().upper()
    try:
        rows = fetch_power_history(period, ROBOT_HOST, ROBOT_PORT)
    except PowerHistoryError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "period": period, "rows": rows})


@app.route("/api/solar_map")
@login_required
def api_solar_map():
    """Read-only, like /api/power_history above -- no role check needed,
    a viewer can see the solar-exposure overlay same as the live gauges.
    fetch_solar_map() loops over Pi #1's paginated SMP response (see
    power_history_client.py and link/power_history.py/link/solar_map.py)
    and returns the assembled grid cells as JSON for /control's map
    checkbox (2026-10-07, explicit user request) to draw."""
    try:
        cells = fetch_solar_map(ROBOT_HOST, ROBOT_PORT)
    except PowerHistoryError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "cells": cells})


@app.route("/media/camera")
@login_required
def media_camera():
    """Proxies the robot's live MJPEG camera stream (Pi #1,
    camera/stream_server.py in the robot repo) so the browser only ever
    talks to this server -- the feed stays behind the login instead of
    being reachable directly on the LAN. Shared by two different <img>
    error handlers: /control's own live-camera column (2026-10-06, just
    a "Camera unavailable" placeholder and a retry) and /media's "Video
    feed" panel (falls back to the recorded video playlist instead).
    Returns 502 quickly (instead of hanging) if the robot's camera script
    isn't running or unreachable, so whichever page is asking gets its
    fallback right away rather than a long hang."""
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


@app.route("/media/thumb/<kind>/<path:filename>")
@login_required
def media_thumb(kind, filename):
    """Proxies+caches one geotagged photo/video for /control's GPS map
    (the violet markers' hover thumbnail) -- these files live on Pi #1's
    camera process (camera/stream_server.py), not in MEDIA_DIR above, and
    are reachable at GET /snapshots/<filename> or /recordings/<filename>
    there (see that module and link/server.py's MED sentence, which is
    how this page learns these filenames/positions exist in the first
    place).

    2026-10-05 (explicit user request): the file is downloaded to Pi #2
    ONLY on demand -- the two Pis must actually be in contact -- and
    cached in MEDIA_THUMB_CACHE_DIR rather than re-fetched on every hover;
    that cache is wiped every time /control is (re)loaded (see
    _clear_media_thumb_cache()/control() above), so hovering the same
    point twice within one page view reuses the cached copy, but nothing
    here is kept across page loads.

    `kind` is "photo" or "video" (matches MED's SNAP/VID field, decoded by
    the page's own JS) and picks which of the camera process's two capped
    file stores to fetch from -- a filename is only unique within its own
    store, not across both, so the kind must come along with it."""
    if kind == "photo":
        remote_path, content_type = "/snapshots/", "image/jpeg"
    elif kind == "video":
        remote_path, content_type = "/recordings/", "video/mp4"
    else:
        abort(404)

    # filename only ever becomes a local cache-file name and one path
    # segment of a trusted upstream URL below -- reject anything that
    # isn't the plain "one segment, no traversal" shape camera/
    # snapshots.py's/recordings.py's own filenames always have (same
    # defense-in-depth as their own _handle_file(), which additionally
    # validates against their live listing -- this proxy doesn't need to
    # duplicate that check, a wrong/stale name just 404s via the upstream
    # request below).
    if not filename or "/" in filename or filename in (".", ".."):
        abort(404)

    os.makedirs(MEDIA_THUMB_CACHE_DIR, exist_ok=True)
    cache_name = f"{kind}_{filename}"
    cache_path = os.path.join(MEDIA_THUMB_CACHE_DIR, cache_name)

    if not os.path.isfile(cache_path):
        url = f"http://{ROBOT_HOST}:{CAMERA_PORT}{remote_path}{filename}"
        try:
            upstream = requests.get(url, timeout=CAMERA_TIMEOUT)
        except requests.exceptions.RequestException:
            abort(502)
        if upstream.status_code != 200:
            abort(404)
        with open(cache_path, "wb") as f:
            f.write(upstream.content)

    return send_from_directory(MEDIA_THUMB_CACHE_DIR, cache_name, mimetype=content_type)


@app.route("/media/robot_feed")
@login_required
def media_robot_feed():
    """JSON listing of what's currently in the robot's own capped camera
    buffers (Pi #1, camera/stream_server.py -- the same up-to-5-photos/
    up-to-5-videos rolling stores already exposed for /control's violet
    map markers via the MED sentence) -- backs the "Robot camera" panel
    on /media (2026-10-05 explicit user request): a carousel that cycles
    through these, photos first (a few seconds each) then videos
    (autoplayed to completion), looping back to the first photo once the
    last video ends -- see _media_page()'s script.

    `kind` on each item is "photo" or "video" (same vocabulary as
    link.robot_state.RobotState.media_positions()), so the client can
    build the right /media/thumb/<kind>/<filename> URL for it (that route
    already does the actual fetch-and-cache from Pi #1). Returns
    {"ok": false} rather than erroring if either store can't be listed
    right now (Pi #1 unreachable, camera script not running) -- same
    "camera is optional" spirit as everywhere else this project talks to
    camera/stream_server.py -- so the carousel can show an empty state
    instead of breaking the page."""
    items = []
    for label, kind in (("snapshots", "photo"), ("recordings", "video")):
        url = f"http://{ROBOT_HOST}:{CAMERA_PORT}/{label}"
        try:
            resp = requests.get(url, timeout=CAMERA_TIMEOUT)
            resp.raise_for_status()
            names = resp.json().get(label, [])
        except (requests.exceptions.RequestException, ValueError):
            continue
        for name in names:
            items.append({"filename": name, "kind": kind})
    return jsonify({"ok": True, "items": items})


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

    # power.html excluded (2026-10-05, explicit user request): it's a
    # dashboard, not a doc, and is already reachable from /control's own
    # nav link plus its own "Pages"/"Explained" cross-links -- listing it
    # here too was redundant.
    files = sorted(
        f for f in os.listdir(PAGES_DIR)
        if f.lower().endswith(".html") and f != "power.html"
    )
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

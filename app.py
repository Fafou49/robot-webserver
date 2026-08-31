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

This server only serves static pages for now. Sending commands to the robot
(Raspberry Pi #1) isn't wired in here yet -- see the "A suivre" section of
the report for the protocol still to be defined.
"""
import functools
import os
import secrets

from dotenv import load_dotenv
from flask import Flask, abort, redirect, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

load_dotenv()

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")

VIDEO_EXTENSIONS = (".mp4", ".webm", ".ogg", ".mov")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# Credentials expected in .env (see .env.example): WEBSERVER_USERNAME and
# WEBSERVER_PASSWORD_HASH (hash generated with generate_password.py, never
# the plaintext password). FLASK_SECRET_KEY signs the session cookie; if
# missing, a random key is generated at startup -- fine for quick testing,
# but every server restart then logs everyone out.
WEBSERVER_USERNAME = os.environ.get("WEBSERVER_USERNAME", "")
WEBSERVER_PASSWORD_HASH = os.environ.get("WEBSERVER_PASSWORD_HASH", "")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

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


def _control_page(videos=None, images=None):
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
    slideshow (client-side JS)."""
    videos = videos or []
    images = images or []

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

    /* Images panel: single <img>, source swapped every 3s by JS below. */
    .slideshow-img {{
      max-width: 100%;
      max-height: 100%;
      border-radius: 8px;
      object-fit: contain;
    }}

    /* Console: bottom third of the window. */
    .console {{
      flex: 1;
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
    }}

    .console-log {{
      flex: 1;
      overflow-y: auto;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 3px;
    }}

    .console-log .line {{ white-space: pre-wrap; }}
    .console-log .ts  {{ color: #6e7681; margin-right: 8px; }}
    .console-log .sys {{ color: #58a6ff; }}
    .console-log .ok  {{ color: #3fb950; }}
    .console-log .err {{ color: #ff7b72; }}
    .console-log .cmd {{ color: #e6edf3; }}

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
    <a href="{url_for('index')}">Pages</a>
    <a href="{url_for('logout')}">Log out</a>
  </div>
  <div class="clock" id="clock">--:--:--</div>

  <div class="main-area">
    <div class="panel">
      <h2>Controls</h2>
      <div class="placeholder">Buttons coming soon</div>
    </div>
    <div class="panel">
      <h2>Video feed</h2>
      {video_html}
    </div>
    <div class="panel panel-right">
      <h2>Images</h2>
      {images_html}
    </div>
  </div>

  <div class="console">
    <div class="console-header">Robot console</div>
    <div class="console-log" id="consoleLog">
      <div class="line"><span class="ts">00:00:00</span><span class="sys">system: waiting for connection...</span></div>
    </div>
    <div class="console-input">
      <span class="prompt">&gt;</span>
      <input id="consoleInput" type="text" placeholder="Type a command and press Enter" autocomplete="off">
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

    function timestamp() {{
      const now = new Date();
      return `${{pad(now.getHours())}}:${{pad(now.getMinutes())}}:${{pad(now.getSeconds())}}`;
    }}

    function logLine(text, cls) {{
      const log = document.getElementById("consoleLog");
      const div = document.createElement("div");
      div.className = "line";
      div.innerHTML = `<span class="ts">${{timestamp()}}</span><span class="${{cls}}">${{text}}</span>`;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }}

    const input = document.getElementById("consoleInput");
    input.addEventListener("keydown", (e) => {{
      if (e.key === "Enter" && input.value.trim() !== "") {{
        const cmd = input.value.trim();
        logLine("&gt; " + cmd, "cmd");
        input.value = "";
        // Placeholder only: no backend wired in yet, protocol still to be defined.
        logLine("no connection to robot (protocol not defined yet)", "err");
      }}
    }});

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
    return _control_page(videos, images)


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
    after login)."""
    error = None
    next_url = request.values.get("next") or url_for("control")

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        credentials_configured = bool(WEBSERVER_USERNAME and WEBSERVER_PASSWORD_HASH)
        valid = (
            credentials_configured
            and username == WEBSERVER_USERNAME
            and check_password_hash(WEBSERVER_PASSWORD_HASH, password)
        )

        if valid:
            session.clear()
            session["logged_in"] = True
            session["username"] = username
            return redirect(next_url)

        if not credentials_configured:
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
    """Home page: lists the HTML pages available in pages/."""
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
        print("ATTENTION: WEBSERVER_USERNAME / WEBSERVER_PASSWORD_HASH non definis dans .env "
              "-- personne ne pourra se connecter. Voir README.md.")
    # host="0.0.0.0": reachable from other devices on the WiFi, not just
    # from the Pi itself (127.0.0.1).
    app.run(host="0.0.0.0", port=5000, debug=False)

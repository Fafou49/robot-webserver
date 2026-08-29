"""
Serveur web minimal (Flask) pour la Raspberry Pi n2 : affiche les pages HTML
du projet (rapport reseau/code, et toute autre page ajoutee plus tard dans
pages/) sur le reseau local, sans etape de compilation.

Lancement :
    pip install -r requirements.txt
    python3 app.py

Puis, depuis n'importe quel appareil connecte au meme WiFi :
    http://<IP_DE_LA_PI_N2>:5000/

Ce serveur ne fait QUE afficher des pages statiques pour l'instant. L'envoi
des ordres vers le robot (Raspberry Pi n1) n'est pas encore branche ici --
voir la section "A suivre" du rapport pour le protocole a definir.
"""
import os

from flask import Flask, abort, send_from_directory

PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")

app = Flask(__name__)


@app.route("/")
def index():
    """Page d'accueil : liste les pages HTML disponibles dans pages/."""
    if not os.path.isdir(PAGES_DIR):
        return "<p>Dossier pages/ introuvable.</p>", 500

    files = sorted(f for f in os.listdir(PAGES_DIR) if f.lower().endswith(".html"))
    if not files:
        return "<p>Aucune page HTML dans pages/ pour le moment.</p>"

    items = "".join(f'<li><a href="/pages/{f}">{f}</a></li>' for f in files)
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Serveur web du robot</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 60px auto; padding: 0 20px; }}
    h1 {{ font-size: 20px; }}
    li {{ margin: 8px 0; }}
    a {{ font-size: 16px; }}
  </style>
</head>
<body>
  <h1>Pages disponibles</h1>
  <ul>{items}</ul>
</body>
</html>"""


@app.route("/pages/<path:filename>")
def serve_page(filename):
    """Sert une page HTML de pages/ telle quelle (pas de templating Jinja,
    pour ne pas risquer de casser le CSS/HTML des pages generees ailleurs)."""
    if not filename.lower().endswith(".html"):
        abort(404)
    return send_from_directory(PAGES_DIR, filename)


if __name__ == "__main__":
    # host="0.0.0.0" : accessible depuis les autres appareils du WiFi,
    # pas seulement depuis la Pi elle-meme (127.0.0.1).
    app.run(host="0.0.0.0", port=5000, debug=False)

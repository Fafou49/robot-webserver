# Serveur web du robot (Raspberry Pi n°2)

Petit serveur Flask, sans interface graphique, qui affiche les pages HTML du
projet sur le réseau local — pour l'instant uniquement le rapport
réseau/code (`pages/rapport_rover_dgps.html`). L'accès est protégé par un
login (identifiant + mot de passe), voir "Configurer le login" ci-dessous.
L'envoi des ordres vers le robot (Raspberry Pi n°1) n'est pas encore
branché ici, voir "À suivre" dans le rapport pour le protocole à définir
(HTTP, WebSocket...).

## Structure

```
robot-webserver/
├── app.py                serveur Flask
├── generate_password.py  genere le hash de mot de passe pour .env
├── requirements.txt
├── .env.example           modele de configuration (identifiants, cle de session)
├── pages/                 pages HTML statiques a afficher
│   └── rapport_rover_dgps.html
├── media/
│   ├── videos/            videos a afficher dans l'espace "Video feed" de /control
│   └── images/            images du diaporama dans l'espace "Images" de /control
└── README.md
```

## Installation sur la Raspberry Pi n°2 (Pi OS Lite)

```bash
# copier ce dossier sur la Pi, par exemple via scp depuis votre PC :
scp -r robot-webserver pi@<IP_DE_LA_PI_N2>:~/

# puis, connecte en SSH sur la Pi :
cd ~/robot-webserver
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configurer le login (avant le premier lancement)

Le site est protégé par un identifiant/mot de passe. Le mot de passe n'est
jamais stocké en clair, seul son hash l'est.

```bash
# 1. copie le modele de configuration
cp .env.example .env

# 2. genere le hash de ton mot de passe (il te sera demande, sans s'afficher)
python3 generate_password.py
```

Ça affiche une ligne du type `WEBSERVER_PASSWORD_HASH=pbkdf2:sha256:...` —
colle-la dans `.env`, à la place de la ligne `WEBSERVER_PASSWORD_HASH=` vide.
Choisis aussi ton identifiant dans `WEBSERVER_USERNAME` (par défaut
`fabrice`).

Optionnel mais recommandé (sinon tout le monde est déconnecté à chaque
redémarrage du serveur) : génère une clé de session fixe et colle-la dans
`FLASK_SECRET_KEY` :

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Une fois `.env` rempli, lance le serveur :

```bash
python3 app.py
```

Le serveur écoute sur toutes les interfaces (`0.0.0.0`), port `5000` — donc
accessible depuis n'importe quel appareil du même WiFi à l'adresse :

```
http://<IP_DE_LA_PI_N2>:5000/
```

- Chez tes parents (réseau Orange), la Pi n°2 est en `192.168.1.23` →
  `http://192.168.1.23:5000/`
- Chez toi (Bbox), la Pi n°2 est en `192.168.1.179` →
  `http://192.168.1.179:5000/`

Les deux IP sont réservées de façon fixe sur chaque routeur (réservation
DHCP par adresse MAC), donc elles ne devraient plus changer.

## Ajouter une nouvelle page

Dépose n'importe quel fichier `.html` autonome (CSS/JS/images intégrés,
sans dépendance externe) dans `pages/` — il apparaîtra automatiquement dans
la liste sur `/` et sera servi tel quel sur `/pages/<nom-du-fichier>.html`.

## Ajouter des vidéos et des images (page /control)

Dépose tes fichiers dans `media/videos/` (`.mp4`, `.webm`, `.ogg`, `.mov`)
et `media/images/` (`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`) — ils
apparaissent automatiquement sur `/control` au prochain chargement de la
page (pas besoin de redémarrer le serveur) :

- **Video feed** : les vidéos sont lues une par une (pas toutes en même
  temps), dans l'ordre alphabétique des noms de fichiers ; à la fin d'une
  vidéo, la suivante démarre automatiquement, et ça reboucle sur la
  première après la dernière. Contrôles natifs du navigateur, son coupé
  par défaut — clique sur l'icône du son dans le lecteur pour l'activer.
- **Images** : diaporama automatique, une image affichée toutes les 3
  secondes, dans l'ordre alphabétique des noms de fichiers.

Ces dossiers sont vides par défaut (juste un `.gitkeep` pour que Git les
garde) — tant qu'ils sont vides, `/control` affiche les emplacements en
pointillés "coming soon".

## Lancer le serveur au démarrage de la Pi (optionnel, à faire plus tard)

Pour que le serveur redémarre automatiquement avec la Raspberry Pi (utile en
usage réel, pas indispensable pour tester), la méthode standard est un
service `systemd` — à mettre en place une fois l'application plus aboutie
(notamment une fois l'envoi d'ordres au robot ajouté).

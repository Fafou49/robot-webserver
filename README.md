# Serveur web du robot (Raspberry Pi n°2)

Petit serveur Flask, sans interface graphique, qui affiche les pages HTML du
projet sur le réseau local (rapport réseau/code, protocole de contrôle...)
et relaie les commandes tapées dans la console de `/control` vers le robot
(Raspberry Pi n°1). L'accès est protégé par un login (identifiant + mot de
passe), voir "Configurer le login" ci-dessous. Ce README est en français
pour toi, mais le site lui-même (login, `/control`, et toutes les pages de
`pages/`) est entièrement en anglais.

## Structure

```
robot-webserver/
├── app.py                serveur Flask
├── nmea.py               construction/analyse des trames NMEA + checksum
├── robot_link.py         client TCP vers le serveur de commandes du robot
├── generate_password.py  genere le hash de mot de passe pour .env
├── requirements.txt
├── .env.example           modele de configuration (identifiants, cle de session, IP du robot)
├── pages/                 pages HTML statiques a afficher
│   ├── rapport_rover_dgps.html
│   └── protocole_controle.html
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

### Compte "invité" (accès en lecture seule, optionnel)

En plus de ton compte principal, tu peux configurer un second identifiant
donnant un accès restreint à `/control` — pratique pour partager le suivi
du robot (position, vitesse...) avec quelqu'un sans lui laisser la
possibilité de le piloter à distance (utile notamment si le site est un
jour exposé sur internet, pas seulement sur ton WiFi local).

```bash
# genere le hash du mot de passe invité (meme outil que ci-dessus)
python3 generate_password.py
```

Colle le hash obtenu dans `WEBSERVER_VIEWER_PASSWORD_HASH`, et choisis un
identifiant dans `WEBSERVER_VIEWER_USERNAME` (les deux sont vides par
défaut, ce qui désactive complètement ce compte). Une fois connecté avec
ces identifiants, l'invité voit le bandeau d'état (position, vitesse...)
se mettre à jour normalement, mais :

- le panneau **Controls** n'affiche aucun bouton de commande ;
- le champ de saisie de la console est désactivé (grisé, non cliquable) ;
- même en contournant l'interface (appel direct à `/api/send`), le serveur
  refuse toute commande autre que `STA` (celle qui alimente le bandeau
  d'état) pour ce compte — ce n'est donc pas qu'un masquage côté
  affichage, c'est une vraie restriction côté serveur.

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

## Envoyer des commandes au robot (console de /control)

La console de `/control` envoie chaque commande tapée au serveur de
contrôle du robot (`link/server.py`, dépôt `robot`) via une connexion TCP —
voir `pages/protocole_controle.html` pour le format complet des trames.
Tape juste le type de trame et ses champs, séparés par des virgules, sans
le préfixe `$PROV,` ni le checksum (ajoutés automatiquement) :

```
STP
DRV,120,120
MOD,MANUAL
```

Configure l'adresse du robot dans `.env` :

```
ROBOT_HOST=192.168.1.180   # IP de la Raspberry Pi n°1 (Bbox par defaut)
ROBOT_PORT=5050
```

Si le robot est injoignable (éteint, mauvaise IP, port fermé), la console
affiche l'erreur en rouge au lieu de planter — la commande `STA` (sans
argument) renvoie l'état courant du robot, utile pour vérifier la liaison.

Le panneau **Controls** (à gauche de `/control`) propose aussi un bouton
par type de trame de commande (STP, DRV, NAV, MOD, PID, CAM, STA), pleine
largeur, avec un exemple complet en titre — cliquer dessus écrit juste le
début de la trame (ex. `MOD,`) dans la ligne de commande, à compléter et
valider avec Entrée.

## Bandeau d'état (haut de /control)

Sous les liens de retour à l'accueil, un bandeau interroge automatiquement
le robot (trame `STA`, toutes les 3 secondes) et affiche :

- **à gauche** la position GPS courante du robot ;
- **au centre** la distance et le cap vers la cible, la vitesse (km/h) et
  les valeurs moteur Gauche/Droite ;
- **à droite** la position GPS cible (dernière trame `NAV` reçue par le
  robot).

Distance et cap (vers la cible) sont calculés côté site (formule de
haversine, en JavaScript) à partir des deux positions — volontairement
indépendant de `gps/gps_delta.py` dans le dépôt robot, qui a un bug connu
non corrigé. La vitesse, elle, vient directement du champ `speed` de la
trame `STA` (mesurée par le GPS du robot, voir `link/gps_reader.py` dans le
dépôt `robot`) — aucun calcul côté site. Tant que le robot n'a pas de vraie
source GPS branchée, la position courante affiche "GPS non disponible" et
distance/cap/vitesse affichent "--" (la position cible et les valeurs
moteur, elles, sont déjà réelles dès aujourd'hui).

Le bandeau ne pollue jamais l'onglet **Console** de la boîte de dialogue en
dessous — ses requêtes automatiques n'y apparaissent pas, seules les
commandes tapées à la main y sont journalisées. Elles restent toutefois
visibles dans le second onglet, **TCP (all)**, qui journalise absolument
tout ce que le site échange avec le robot (commandes tapées comme requêtes
automatiques du bandeau) — utile pour déboguer la liaison sans attendre
qu'une commande manuelle échoue. Cet onglet garde une taille fixe : il
n'affiche jamais plus des 10 dernières trames (requête + réponse), les
plus anciennes étant automatiquement retirées au fur et à mesure —
l'onglet Console, lui, reste illimité puisqu'il ne contient que ce que
l'utilisateur a tapé. Dans les deux cas, la boîte de dialogue elle-même
(le rectangle visible en bas de `/control`) garde toujours la même
taille : au-delà de ce qui tient à l'écran, le contenu défile à
l'intérieur au lieu d'agrandir la boîte (`min-height: 0` sur les
conteneurs flex concernés — sans ça, un onglet qui se remplit pousse la
boîte à grandir hors de l'écran plutôt que de rester en place).

## Caméra en direct (panneau "Video feed" de /control)

Si une webcam est branchée sur le robot (Raspberry Pi n°1) et que son
script de diffusion tourne (`python3 -m camera` dans le dépôt `robot`,
voir son README), le panneau "Video feed" de `/control` affiche
automatiquement ce flux en direct, à la place de la playlist vidéo
enregistrée — sans avoir besoin de recharger la page. Techniquement, le
navigateur ne parle qu'à ce serveur (route `/media/camera`), qui relaie le
flux MJPEG du robot ; le flux reste donc protégé par le login du site.

Si le flux du robot devient injoignable (script arrêté, robot éteint,
mauvaise IP), le panneau retombe automatiquement sur la playlist vidéo
enregistrée (ou l'emplacement "coming soon" si elle est vide), et retente
la connexion toutes les 5 secondes en arrière-plan.

Configure dans `.env` :

```
CAMERA_PORT=8000            # doit correspondre au port d'écoute de camera/stream_server.py
CAMERA_STREAM_PATH=/stream.mjpg
```

(`ROBOT_HOST`, déjà configuré ci-dessus pour les commandes, est réutilisé
pour joindre la caméra -- même Pi, port différent.)

## Lancer le serveur au démarrage de la Pi (optionnel, à faire plus tard)

Pour que le serveur redémarre automatiquement avec la Raspberry Pi (utile en
usage réel, pas indispensable pour tester), la méthode standard est un
service `systemd` — à mettre en place une fois l'application plus aboutie
(notamment une fois l'envoi d'ordres au robot ajouté).

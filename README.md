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
│   ├── protocole_controle.html
│   └── architecture_webserver.html
├── static/                servi automatiquement par Flask sur /static/<fichier>
│   └── tech_stack.png     image de fond de /pages (technologies du projet en cercle autour de Claude)
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

La page `/pages` elle-même (la liste, pas les fichiers qu'elle liste) a son
propre habillage dans `app.py` (`_page()`) : fond d'écran fixe
(`static/tech_stack.png`, les technologies du projet en cercle autour de
Claude), liens en bleu `#58a6ff` (même couleur que le cercle autour de
Claude sur l'image, et que l'accent déjà utilisé sur `/control`), contenu
dans un panneau semi-transparent pour rester lisible quel que soit
l'endroit de l'image qui se trouve derrière. Remplacer `static/tech_stack.png`
par une autre image (même nom, ou changer l'URL dans `_page()`) suffit pour
en changer.

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

## Carte GPS (/control)

À droite du panneau Controls, `/control` affiche une carte GPS simple (une
grille, pas un fond de carte réel — ce robot roule en extérieur sans
connexion internet fiable) qui place automatiquement tous les points GPS
actuellement connus, avec un zoom qui s'ajuste à leur étendue. Elle
interroge le robot toutes les 5 secondes (trames `STA`, `WPT`, `GRT`,
`MED` — voir `pages/protocole_controle.html`) :

- **Vert** : position actuelle du robot (`STA`).
- **Bleu** : points sauvegardés sur la Raspberry Pi n°1 via le bouton `X`
  de la manette (`WPT`), reliés par un trait fin dans l'ordre de
  sauvegarde.
- **Rouge** : la dernière cible envoyée manuellement ("NAV sent", suivie
  uniquement côté site) ET la route "GPS Driving" actuellement active
  (`GRT`) — volontairement regroupées sous une seule couleur, sans
  distinction de forme.
- **Violet** : position GPS des photos/vidéos prises par le robot
  (`MED`) — survoler un point violet télécharge et affiche une miniature
  (image statique pour une vidéo, jamais de lecture automatique) ; le
  fichier n'est récupéré sur la Pi n°1 qu'au survol, et mis en cache dans
  un dossier temporaire sur la Pi n°2 qui se vide à chaque rechargement
  de `/control`.

Au survol de tout autre point (2026-10-05, sauf les violets, qui
affichent déjà une miniature), une vignette indique sa distance en
mètres jusqu'au robot (calcul haversine, même formule que le bandeau
d'état) — "unavailable" si le robot n'a pas de fix GPS pour le moment ; le
point du robot lui-même n'affiche évidemment pas de distance.

**Zoom à la molette** (2026-10-05) : zoome en ciblant le point SOUS LE
CURSEUR, pas la position du robot — le point visé reste exactement sous
la souris pendant qu'on zoome. Un double-clic réinitialise le zoom sur
l'ajustement automatique (qui, sinon, ne bouge plus la vue une fois
qu'on a zoomé manuellement, pour ne pas la faire sauter sous la souris au
rafraîchissement suivant). Une échelle de distance (bas-gauche de la
carte, en mètres ou en km) se recalcule à chaque zoom/dézoom.

**Suppression d'un point au clic droit** (2026-10-05) :

- Sur un point **bleu**, supprime immédiatement ce waypoint du fichier
  `waypoints.txt` de la Pi n°1 (nouvelle trame `WPD`, par index plutôt
  que par coordonnées pour éviter tout écart d'arrondi par rapport à ce
  qui est réellement écrit dans le fichier). S'il était au milieu de la
  liste, le segment se reforme automatiquement entre ses deux voisins dès
  le prochain rafraîchissement — la carte reconnecte simplement les
  points restants dans leur nouvel ordre, rien de spécifique à gérer.
- Sur un point **violet**, demande confirmation puis supprime la
  photo/vidéo à la fois du buffer caméra de la Pi n°1 (`camera/
  snapshots.py`/`camera/recordings.py`) ET de sa ligne dans la base de
  données de géolocalisation (nouvelle trame `MDD`) — irréversible.
- Les points **rouge** (NAV envoyé / GPS Driving) et **vert** (robot) ne
  sont volontairement pas supprimables par ce menu : rien ne les
  sauvegarde individuellement côté Pi n°1 (une route "GPS Driving" vit en
  mémoire tant qu'elle est active, "NAV sent" n'est même suivi que côté
  site), donc il n'y a rien de propre à "retirer" point par point — le
  clic droit y laisse simplement apparaître le menu contextuel normal du
  navigateur.

## Vignette caméra du robot (panneau droit de /media)

Le panneau de droite de `/media` est désormais partagé en deux moitiés
(2026-10-05) : en haut, le diaporama "Images" existant (photos stockées
sur la Pi n°2, comportement inchangé) ; en bas, un nouveau carrousel
"Robot camera" qui fait défiler les photos ET vidéos prises PAR LE ROBOT
— les mêmes buffers tournants (max 5 photos + 5 vidéos) que
`camera/stream_server.py` expose déjà pour les marqueurs violets de
`/control`. Chaque photo s'affiche 3 secondes, puis les vidéos démarrent
automatiquement les unes après les autres jusqu'à la dernière, avant de
reboucler sur la première photo. La liste elle-même est rafraîchie depuis
le robot toutes les 30 secondes (nouvelle route `/media/robot_feed`),
sans interrompre ce qui est en cours de lecture ; chaque fichier est
affiché via la route déjà existante `/media/thumb/<kind>/<filename>`
(même cache à la demande que les miniatures de la carte GPS ci-dessus).

## Page Power (/power) — suivi solaire/batterie/charge

Page autonome dédiée (lien "Power" dans le bandeau de navigation de
`/control`), en lecture seule : elle interroge le robot toutes les 3
secondes avec la trame `PWR` (sans champ, voir le dépôt `robot`,
`link/tracer_reader.py` et la section "Liaison RS485 / Tracer" de son
README) et affiche la tension/courant/puissance du panneau solaire, l'état
de la batterie (tension, courant et puissance de charge, température,
état de charge avec une jauge), la tension/courant/puissance de la charge
en sortie, et la température interne du contrôleur EPever Tracer. Tant que
le contrôleur n'a pas répondu avec succès au moins une fois (câble RS485
débranché, module `ch343` non chargé...), les tuiles affichent "—" et le
badge en haut à droite indique "Tracer unavailable" — la page continue
d'interroger le robot en arrière-plan et se met à jour automatiquement dès
que la liaison redevient disponible, sans recharger la page.

Comme `/control`, cette page est servie derrière le login — le compte
"invité" en lecture seule (voir plus haut) peut aussi l'ouvrir : `PWR`
étant une trame de lecture seule sans aucun effet sur le robot, elle a été
ajoutée à la liste des commandes autorisées pour ce rôle dans `app.py`
(`VIEWER_ALLOWED_COMMANDS`), au même titre que `STA`.

Sa navigation (2026-10-05) ne contient plus que "Control page" et
"Pages" — les liens directs vers `protocole_controle.html` et
`power_explained.html` ont été retirés (explicite demande utilisateur) :
ces deux pages restent accessibles, mais uniquement via le sommaire
`/pages`, comme le reste des pages de référence.

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

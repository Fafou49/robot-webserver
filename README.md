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
├── systemd/                unite systemd pour le demarrage automatique au boot (voir plus bas)
│   └── robot-webserver.service
└── README.md
```

## Installation sur la Raspberry Pi n°2 (Pi OS Lite)

```bash
# copier ce dossier sur la Pi, par exemple via scp depuis votre PC :
scp -r robot-webserver robot@<IP_DE_LA_PI_N2>:~/

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

## Ajouter des vidéos et des images (page /media)

Dépose tes fichiers dans `media/videos/` (`.mp4`, `.webm`, `.ogg`, `.mov`)
et `media/images/` (`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`) — ils
apparaissent automatiquement sur `/media` au prochain chargement de la
page (pas besoin de redémarrer le serveur). Ces deux panneaux vivaient à
l'origine dans `/control` ; ils ont déménagé sur leur propre page `/media`
le 2026-10-05 pour que `/control` reste concentré sur le pilotage :

- **Video feed** : les vidéos sont lues une par une (pas toutes en même
  temps), dans l'ordre alphabétique des noms de fichiers ; à la fin d'une
  vidéo, la suivante démarre automatiquement, et ça reboucle sur la
  première après la dernière. Contrôles natifs du navigateur, son coupé
  par défaut — clique sur l'icône du son dans le lecteur pour l'activer.
- **Images** : diaporama automatique, une image affichée toutes les 3
  secondes, dans l'ordre alphabétique des noms de fichiers.

Ces dossiers sont vides par défaut (juste un `.gitkeep` pour que Git les
garde) — tant qu'ils sont vides, `/media` affiche les emplacements en
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

**Format des points GPS dans la console (2026-10-07) :** `NAV`/`RTE` tapés
à la main attendent le format `ddmm.mmmm` + lettre de direction du
protocole (ex. `NAV,4723.492,N,00044.340,W`) — **pas** les degrés décimaux
affichés juste en dessous dans le bandeau d'état ("actuel"/"cible", ex.
`47.39153°, -0.73900°`) ni ceux attendus par le fichier "GPS Driving"
(`lat,lon` décimal, voir plus bas). Avant cette date, copier/coller un
point décimal affiché ailleurs sur la page dans la console était accepté
sans erreur et envoyait un point complètement faux (~45x d'écart) — la
console détecte maintenant ce cas automatiquement et convertit avant
l'envoi (`normalizeGpsCommand` dans `app.py`), et le robot lui-même
renvoie une erreur claire (`ERR,22`/`ERR,13` selon la trame, voir
`pages/protocole_controle.html`) si le point ne ressemble toujours pas à
un `ddmm.mmmm` valide — utile si quelque chose d'autre que ce site parle
directement au port TCP du robot.

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

## Barre de progression waypoint-return / GPS Driving (/control, 2026-10-07)

Juste sous le bandeau d'état ci-dessus, et seulement pendant qu'une route
est effectivement en cours, une barre de progression affiche à quel point
le robot est avancé dans le retour-waypoints (`BTN_A`) ou la route GPS
Driving actuellement suivie. Même composant pour les deux cas — seule la
couleur et le titre changent, lus depuis le champ `RETURN`/`DRIVE` que
`GRT` renvoie désormais en fin de trame (voir `pages/protocole_controle.html`
et le README du dépôt `robot`) :

- **Bleu**, titre « Waypoint return (BTN_A) » : le robot retrace ses
  propres waypoints sauvegardés (`waypoints.txt`).
- **Rouge**, titre « GPS driving route » : une route `RTE` envoyée/
  uploadée depuis `/control` — même couleur que les points rouges de la
  carte GPS ci-dessous.

Entièrement masquée dès qu'aucune route n'est active ou qu'elle vient de
se terminer (`GRT` renvoie alors une liste vide après `route_index`).

Les points intermédiaires sont espacés sur la barre par **distance GPS
cumulée réelle** depuis la position courante du robot (haversine, même
formule que le bandeau d'état), et non par simple comptage — un espacement
naïf par comptage donnerait une fausse impression quand deux points
consécutifs sont à 15 m l'un de l'autre et les deux suivants à 80 m.
Chaque point reprend la numérotation déjà utilisée par la carte GPS
ci-dessous (« Waypoint 3 », « GPS Driving 5 », calculée à partir de
`route_index` + la position du point dans la liste complète que `GRT`
renvoie, pas renumérotée à partir de 1 sur le sous-ensemble restant) pour
pouvoir croiser les deux vues facilement ; le tout dernier point de la
route complète est toujours affiché comme « Target », quel que soit le
mode. Au survol d'un point, une infobulle affiche sa distance depuis la
position actuelle et une ETA calculée à partir de la vitesse courante du
robot (champ `speed` de `STA`, même source que le bandeau d'état) —
« ETA unavailable (stopped) » tant que le robot est quasiment à l'arrêt
(vitesse < 0.05 km/h), pour éviter une division par (quasi) zéro
trompeuse.

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
  uniquement côté site) ET la route "GPS Driving"/retour-waypoints
  actuellement active (`GRT`) — volontairement regroupées sous une seule
  couleur, sans distinction de forme.
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

**Suppression d'un point au clic droit** (2026-10-05, étendue aux points
rouges le 2026-10-06) — toutes les couleurs sauf le robot lui-même sont
désormais supprimables :

- Sur un point **bleu**, supprime immédiatement ce waypoint du fichier
  `waypoints.txt` de la Pi n°1 (trame `WPD`, par index plutôt que par
  coordonnées pour éviter tout écart d'arrondi par rapport à ce qui est
  réellement écrit dans le fichier). S'il était au milieu de la liste, le
  segment se reforme automatiquement entre ses deux voisins dès le
  prochain rafraîchissement — la carte reconnecte simplement les points
  restants dans leur nouvel ordre, rien de spécifique à gérer.
- Sur un point **violet**, demande confirmation puis supprime la
  photo/vidéo à la fois du buffer caméra de la Pi n°1 (`camera/
  snapshots.py`/`camera/recordings.py`) ET de sa ligne dans la base de
  données de géolocalisation (trame `MDD`) — irréversible.
- Sur un point **rouge "GPS Driving"**, supprime ce point de la route
  actuellement suivie, en mémoire côté Pi n°1 (nouvelle trame `RTD`, par
  index dans la liste que renvoie `GRT`) — sans confirmation, comme pour
  le bleu, puisque rien n'est perdu de façon irréversible sur disque. Si
  le point supprimé est celui actuellement visé, le robot passe
  automatiquement au suivant dans la liste ; si c'était le dernier, la
  route se termine proprement (voir `RobotState.delete_route_point()`
  dans le dépôt `robot` pour le détail). N'écrit jamais dans
  `waypoints.txt`, même pour un point issu d'un retour-waypoints (`BTN_A`)
  — seul `WPD` touche ce fichier.
- Sur le point rouge **"NAV sent"**, il n'y a rien à envoyer au robot : ce
  point n'est suivi que par cet onglet du navigateur (perdu de toute
  façon au rechargement de la page), donc "supprimer" l'oublie simplement
  côté site et redessine la carte sans lui.
- Le point **vert** (position actuelle du robot) n'est volontairement pas
  supprimable par ce menu — le clic droit y laisse apparaître le menu
  contextuel normal du navigateur.

## Carte d'ensoleillement (/control, 2026-10-07)

Nouvelle case à cocher "Solar exposure map" sous la légende de la carte
GPS (voir section précédente) : une fois cochée, superpose une grille de
carrés semi-transparents (environ 5 m de côté) derrière tous les points et
traits existants, chaque carré représentant la puissance PV moyenne
mesurée à cet endroit par le robot (voir `link/solar_map.py` et
l'extension de `link/power_history.py` dans le dépôt `robot` pour la façon
dont cette grille est construite côté Pi n°1). Dégradé séquentiel à quatre
teintes — bleu-nuit foncé (faible puissance) → violet → orange → jaune vif
(forte puissance) — normalisé sur le min/max des cellules reçues à cet
instant (le wattage réel du panneau n'est pas connu côté site). Une petite
légende (dégradé + nombre de cellules/points) apparaît automatiquement dès
que des données sont disponibles.

Données récupérées via la nouvelle route `GET /api/solar_map` (lecture
seule, accessible aussi aux comptes "viewer" comme `/api/power_history`),
qui boucle côté serveur Flask sur la trame paginée `SMP` du Pi n°1
(`power_history_client.py`, fonction `fetch_solar_map()` — même principe
que `fetch_power_history()`). Interrogée toutes les 30 secondes tant que la
case est cochée (la grille elle-même ne change qu'au rythme du recalcul
sur le Pi n°1, toutes les 5 minutes et seulement quand le robot est
inactif — un intervalle de 5 s comme pour la carte elle-même serait inutile)
; aucune requête n'est envoyée tant que la case est décochée.

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

### Heure de bord + alignement des deux colonnes (2026-10-10)

Demande explicite de l'utilisateur. Deux changements sur `pages/power.html` :

- Une petite case **"Onboard time (Pi #1)"**, de faible hauteur (une seule
  ligne), ajoutée juste au-dessus du panneau Battery (colonne de droite).
  Elle affiche l'horloge système du Pi #1 — nouveau 15ᵉ champ `onboard_time`
  de la trame `PWR` (voir le dépôt `robot`, section "Heure de bord" de son
  README) — ainsi qu'un petit texte d'écart ("Ns ahead of"/"behind this
  device") par rapport à l'horloge du navigateur, pour repérer en un coup
  d'œil une dérive de l'horloge du Pi (pas de pile RTC, NTP injoignable...).
  Cet écart est purement indicatif : l'horloge du navigateur elle-même
  n'est pas garantie exacte, ce n'est qu'une comparaison relative rapide.
- Les deux colonnes du tableau de bord sont maintenant **alignées en bas** :
  "Load output" (dernière section de la colonne de gauche) et
  "Temperatures" (dernière section de la colonne de droite) se terminent
  désormais exactement au même niveau, quelle que soit la colonne la plus
  haute. Réalisé en CSS pur (`.dashboard { align-items: stretch }` + un
  `.col-spacer` flexible inséré juste avant la dernière section de chaque
  colonne, qui absorbe l'espace vertical excédentaire de la colonne la plus
  courte) — aucune hauteur n'est codée en dur, donc la mise en page reste
  correcte si le contenu d'une section change. Vérifié par une capture
  Playwright confirmant un écart de 0px entre les bas des deux sections.

### Lifetime (durée d'éveil) + bascule sur l'heure GPS (2026-10-10)

Demande explicite de l'utilisateur : *"ajoutes le life time qui
chronomètre la durée d'éveil de la pi (en petit a côté de l'heure de
bord). si le onboard time n'est pas réglé par le Wifi, prends celui du
GPS"*. Deux nouveaux champs en fin de trame `PWR` (16ᵉ `onboard_time_source`,
17ᵉ `uptime_s`, voir le dépôt `robot`, section "Heure de bord" de son
README, et `pages/protocole_controle.html`) :

- Un petit texte **"up Xh.."/"up Xd Xh"/"up X min"** apparaît maintenant
  directement à côté de l'heure de bord, dans la même case que
  "Onboard time (Pi #1)" — c'est le temps écoulé depuis le dernier
  démarrage du Pi #1 (`uptime_s`, lu depuis `/proc/uptime` côté robot,
  donc totalement indépendant du problème d'horloge ci-dessous : c'est le
  compteur de démarrage du noyau Linux, pas l'heure système).
- Quand l'horloge système du Pi #1 n'a pas encore été corrigée par le
  WiFi/NTP (pas de pile RTC — voir plus haut), `onboard_time` bascule
  automatiquement sur l'heure UTC du récepteur GPS dès qu'un premier fix
  est arrivé (correcte dès l'acquisition du fix, indépendamment du
  WiFi/NTP) — voir `link/power_history.py` et `link/gps_reader.py` côté
  robot. Dans ce cas (rare, normalement limité aux premières minutes
  après un démarrage), une petite étiquette **"GPS"** apparaît à côté de
  l'heure de bord pour signaler que la valeur affichée ne vient pas de
  l'horloge système ; le cas normal (`SYS`) n'affiche rien de plus.

## Caméra en direct (panneau "Video feed" de /media)

Si une webcam est branchée sur le robot (Raspberry Pi n°1) et que son
script de diffusion tourne (`python3 -m camera` dans le dépôt `robot`,
voir son README), le panneau "Video feed" de `/media` affiche
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

## Colonne caméra de /control (2026-10-06)

Piloter le robot sans voir où il pointe n'a pas vraiment de sens, donc
`/control` a maintenant lui aussi une colonne caméra — au milieu, entre
Controls et Map (les trois colonnes se partagent la largeur à parts
égales). Même flux, même route (`/media/camera`), mais volontairement plus
simple que le panneau "Video feed" de `/media` ci-dessus : pas de
secours sur une playlist vidéo enregistrée ici — une vieille vidéo
pendant qu'on est en train de piloter serait plus trompeuse qu'utile. Tant
que le flux n'est pas joignable, la colonne affiche juste "Camera
unavailable — retrying..." et retente toutes les 5 secondes, exactement
comme `/media`.

Au passage, un bug a été corrigé sur `/media` en même temps : le message
"No photos/videos on the robot yet" du carrousel "Robot camera" restait
affiché EN MÊME TEMPS que la photo/vidéo une fois qu'elle arrivait (une
règle CSS plus spécifique empêchait l'attribut `hidden` de vraiment
masquer l'élément), ce qui poussait l'image hors du centre de son cadre.
Les photos/vidéos du robot s'affichent maintenant correctement centrées,
seules, dans leur boîte.

## Lancer le serveur au démarrage de la Pi (`systemd/robot-webserver.service`, 2026-10-06)

Pour que `app.py` démarre tout seul à chaque allumage de la Pi n°2 (sans
avoir besoin d'ouvrir un terminal), la même solution que sur la Pi n°1
(robot repo, `systemd/robot.service` — voir son README, section
"Démarrage automatique au boot") : un service `systemd`. Il démarre avant
toute session graphique/SSH, et redémarre automatiquement le site si le
processus plante.

**Différence avec la Pi n°1** : là-bas, `run_robot.sh` lance DEUX
processus (le serveur de commandes et la caméra), donc un script
intermédiaire (`start_robot.sh`) est nécessaire pour activer
l'environnement virtuel puis les lancer tous les deux. Ici, `app.py` est
un unique processus Flask, donc pas besoin d'un script équivalent : le
service pointe directement sur l'exécutable `python3` DU venv
(`venv/bin/python3`), ce qui revient au même que `source
venv/bin/activate` (l'activation ne fait que modifier le `PATH` d'un
shell interactif ; lancer directement le `python3` du venv obtient le
même résultat, sans avoir besoin d'un shell pour `source`).

**Chemins à vérifier avant d'installer**, dans
`systemd/robot-webserver.service` :
```ini
User=robot
Group=robot
WorkingDirectory=/home/robot/robot-webserver
ExecStart=/home/robot/robot-webserver/venv/bin/python3 /home/robot/robot-webserver/app.py
```
Adapter ces chemins si ce projet ne vit pas exactement dans
`/home/robot/robot-webserver` sur cette Pi, ou si le compte utilisateur
n'est pas `robot` (vérifier aussi que le venv s'appelle bien `venv/` et
pas `.venv/`, voir "Installation" plus haut).

**Installation (à faire une fois, sur la Pi, avec `sudo`)** :
```bash
sudo cp systemd/robot-webserver.service /etc/systemd/system/robot-webserver.service
sudo systemctl daemon-reload
sudo systemctl enable robot-webserver.service   # démarre automatiquement à chaque boot
sudo systemctl start robot-webserver.service    # démarre tout de suite, sans attendre un reboot
```

**Vérifier que ça tourne** :
```bash
sudo systemctl status robot-webserver.service   # actif ou non, dernières lignes de log
journalctl -u robot-webserver.service -f        # logs en direct (Ctrl+C pour arrêter de suivre)
```

**Arrêter/redémarrer/désactiver** :
```bash
sudo systemctl stop robot-webserver.service       # arrête maintenant (jusqu'au prochain boot/start)
sudo systemctl restart robot-webserver.service    # redémarre tout de suite (utile après un `git pull`)
sudo systemctl disable robot-webserver.service    # ne redémarre plus tout seul au boot
```

**Points d'attention** :
- Le service tourne sous l'utilisateur `robot` (`User=robot` dans
  `robot-webserver.service`, même convention que le service de la Pi
  n°1 — à adapter si ce projet tourne sous un autre nom d'utilisateur
  sur cette Pi).
- `app.py` charge `.env` (identifiants de login, `ROBOT_HOST`,
  `FLASK_SECRET_KEY`...) via `load_dotenv()`, qui lit le fichier `.env`
  du répertoire courant — `WorkingDirectory=` dans le service DOIT donc
  rester exactement le dossier du projet, sinon `.env` ne se charge pas
  et le site démarre avec des identifiants/réglages vides.
- Si le service démarre (`systemctl status` actif) mais que le site ne
  répond pas, vérifier d'abord `journalctl -u robot-webserver.service
  -f` — une cause fréquente est un `.env` manquant ou incomplet (voir
  "Configurer le login" plus haut), puisque Flask démarre quand même
  mais rejette alors toute tentative de connexion.
- `Restart=on-failure` ne redémarre qu'en cas de plantage réel du
  processus Python (crash, exception non gérée) — un `sudo systemctl
  stop` reste un arrêt normal, pas un crash, donc pas de redémarrage
  intempestif juste après.
- Penser à `sudo systemctl restart robot-webserver.service` après chaque
  mise à jour du code sur cette Pi (`git pull`, ou copie manuelle d'un
  nouveau `app.py`) — le service ne relit pas les fichiers tout seul, il
  faut le redémarrer pour que le nouveau code soit pris en compte.
- **Non testé sur une vraie Raspberry Pi** (même honnêteté que pour le
  service équivalent sur la Pi n°1) : `robot-webserver.service` a été
  relu et vérifié (syntaxe `.ini`, chemins cohérents avec la section
  "Installation" ci-dessus), mais l'installation `systemd` elle-même —
  permissions, comportement réel au boot — n'a pas pu être vérifiée dans
  cet environnement (pas de vraie Pi ni de `systemd` actif ici). À
  tester avec un vrai redémarrage avant de s'y fier pour un déploiement
  sur le terrain.

# Serveur web du robot (Raspberry Pi n°2)

Petit serveur Flask, sans interface graphique, qui affiche les pages HTML du
projet sur le réseau local — pour l'instant uniquement le rapport
réseau/code (`pages/rapport_rover_dgps.html`). L'envoi des ordres vers le
robot (Raspberry Pi n°1) n'est pas encore branché ici, voir "À suivre" dans
le rapport pour le protocole à définir (HTTP, WebSocket...).

## Structure

```
robot-webserver/
├── app.py              serveur Flask
├── requirements.txt
├── pages/              pages HTML statiques a afficher
│   └── rapport_rover_dgps.html
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
python3 app.py
```

Le serveur écoute sur toutes les interfaces (`0.0.0.0`), port `5000` — donc
accessible depuis n'importe quel appareil du même WiFi à l'adresse :

```
http://<IP_DE_LA_PI_N2>:5000/
```

- Chez tes parents (réseau Orange), la Pi n°2 est en `192.168.1.23` →
  `http://192.168.1.23:5000/`
- Chez toi (Bbox), l'IP de la Pi n°2 n'est pas encore connue.

## Ajouter une nouvelle page

Dépose n'importe quel fichier `.html` autonome (CSS/JS/images intégrés,
sans dépendance externe) dans `pages/` — il apparaîtra automatiquement dans
la liste sur `/` et sera servi tel quel sur `/pages/<nom-du-fichier>.html`.

## Lancer le serveur au démarrage de la Pi (optionnel, à faire plus tard)

Pour que le serveur redémarre automatiquement avec la Raspberry Pi (utile en
usage réel, pas indispensable pour tester), la méthode standard est un
service `systemd` — à mettre en place une fois l'application plus aboutie
(notamment une fois l'envoi d'ordres au robot ajouté).

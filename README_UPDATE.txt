Pi #2 (robot-webserver) -- augmentation du debit air LoRa (2026-10-24)
=========================================================================

Dezippez a la racine du depot (ajoute tools/lora_configure.py, met a
jour .env.example et pages/protocole_controle.html -- ne touche aucun
autre fichier de code).

Meme procedure que sur Pi #1 (voir le README du zip Pi #1) -- a faire
sur le dongle de Pi #2 avec les MEMES valeurs (--air-rate 9.6
--uart-rate 19200).

Apres reconfiguration et verification du dongle :
  - .env ici : LORA_BAUD_RATE=19200
  - (pas de LORA_AIR_RATE_BYTES_PER_S ici -- cette variable ne vit que
    sur Pi #1)
  - Redemarrez le service (systemctl restart robot-webserver, ou
    relancer python3 app.py).

pages/protocole_controle.html a ete mis a jour pour refleter tout ca :
debit UART vs debit air, le constat terrain du 2026-10-24, le nouveau
camembert a 1200 B/s, et la reference a tools/lora_configure.py.

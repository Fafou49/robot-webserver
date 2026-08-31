"""
Generates the hash to paste into WEBSERVER_PASSWORD_HASH (.env) -- the
plaintext password is never stored, only its hash.

Usage:
    python3 generate_password.py
    (type your password when prompted, it won't be echoed to the screen)
"""
import getpass

from werkzeug.security import generate_password_hash

password = getpass.getpass("Mot de passe a hasher : ")
confirm = getpass.getpass("Confirme le mot de passe : ")

if password != confirm:
    print("Les deux saisies ne correspondent pas, rien n'a ete genere.")
else:
    print("\nAjoute cette ligne dans ton fichier .env :\n")
    print(f"WEBSERVER_PASSWORD_HASH={generate_password_hash(password)}")

"""
Generates a password hash to paste into .env (WEBSERVER_PASSWORD_HASH for
the admin account, or WEBSERVER_VIEWER_PASSWORD_HASH for the read-only
guest account) -- the plaintext password is never stored, only its hash.

Usage:
    python3 generate_password.py
    (type your password when prompted, it won't be echoed to the screen)
"""
import getpass

from werkzeug.security import generate_password_hash

password = getpass.getpass("Password to hash: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    print("The two entries don't match, nothing was generated.")
else:
    print("\nPaste this line into your .env file (use the *_VIEWER_* variant")
    print("instead if this is for the read-only guest account):\n")
    print(f"WEBSERVER_PASSWORD_HASH={generate_password_hash(password)}")

"""TCP client used by the Flask app to send NMEA-style commands to the
robot's control server (link.server in the robot repository, Pi #1) and
read back its ACK/ERR/STA response.

One command per connection: open, send, read one line, close. Simple and
plenty fast enough for human-typed console commands on a local WiFi link
-- see pages/protocole_controle.html for the sentence format and
pages/protocole_controle.html "Trames definies" for the transport
decision (plain TCP socket, chosen over MQTT/HTTP for simplicity).
"""

import socket

from nmea import SentenceError, build_sentence, parse_sentence

MAX_COMMAND_LENGTH = 200


def send_command(command_body: str, host: str, port: int, timeout: float = 2.0) -> dict:
    """`command_body` is what the user types in the console, e.g. "STP" or
    "DRV,120,120" -- the sentence type followed by its comma-separated
    fields, WITHOUT the "$PROV," prefix or the checksum (both are added
    here). Returns a dict always containing "ok" and "sent"; on success
    also "type"/"fields"/"raw_response", on failure "error"."""
    command_body = (command_body or "").strip()[:MAX_COMMAND_LENGTH]
    parts = [p.strip() for p in command_body.split(",")]
    sentence_type, fields = parts[0], parts[1:]
    sentence = build_sentence(sentence_type, *fields)

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((sentence + "\r\n").encode("ascii"))
            response_line = sock.makefile("r").readline().strip()
    except OSError as exc:
        return {"ok": False, "sent": sentence, "error": f"CONNECTION_ERROR:{exc}"}

    if not response_line:
        return {"ok": False, "sent": sentence, "error": "EMPTY_RESPONSE"}

    try:
        resp_type, resp_fields = parse_sentence(response_line)
    except SentenceError as exc:
        return {
            "ok": False,
            "sent": sentence,
            "error": f"BAD_RESPONSE:{exc}",
            "raw_response": response_line,
        }

    return {
        "ok": resp_type != "ERR",
        "sent": sentence,
        "type": resp_type,
        "fields": resp_fields,
        "raw_response": response_line,
    }

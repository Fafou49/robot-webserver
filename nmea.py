"""Build and parse the project's NMEA-0183-inspired proprietary sentences.

This is an exact copy of link/nmea.py from the robot repository (Pi #1) --
both projects are deployed independently, so the small shared piece is
duplicated rather than pulled in as a package dependency. Keep the two in
sync if the sentence format changes; see pages/protocole_controle.html for
the format this implements.
"""

PREFIX = "PROV"


class SentenceError(ValueError):
    """Raised when a raw line isn't a well-formed / valid sentence."""


def compute_checksum(body: str) -> str:
    """XOR of every character in `body` (the part between "$" and "*",
    exclusive), formatted as 2 uppercase hex digits."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"{checksum:02X}"


def build_sentence(sentence_type: str, *fields) -> str:
    """Builds a full sentence, e.g. build_sentence("DRV", 120, 120) ->
    "$PROV,DRV,120,120*77" (no trailing \\r\\n)."""
    parts = [PREFIX, sentence_type, *[str(f) for f in fields]]
    body = ",".join(parts)
    return f"${body}*{compute_checksum(body)}"


def parse_sentence(raw: str):
    """Parses a raw line into (sentence_type, fields). Raises
    SentenceError with a human-readable reason if malformed."""
    raw = raw.strip()
    if not raw.startswith("$"):
        raise SentenceError("missing leading '$'")
    if "*" not in raw:
        raise SentenceError("missing '*checksum'")

    body, _, checksum = raw[1:].partition("*")
    checksum = checksum.strip()
    if len(checksum) != 2:
        raise SentenceError("checksum must be exactly 2 hex characters")

    expected = compute_checksum(body)
    if checksum.upper() != expected:
        raise SentenceError(f"checksum mismatch (got {checksum.upper()}, expected {expected})")

    parts = body.split(",")
    if len(parts) < 2 or parts[0] != PREFIX:
        raise SentenceError(f"expected prefix '{PREFIX}', got '{parts[0]}'")

    sentence_type = parts[1]
    fields = parts[2:]
    return sentence_type, fields

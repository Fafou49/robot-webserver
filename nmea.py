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


def decimal_to_nmea(value: float, is_longitude: bool):
    """Converts a signed decimal-degrees coordinate into this protocol's
    on-the-wire (ddmm.mmmm string, direction letter) pair, e.g. 47.391534
    -> ("4723.492", "N"), -0.739006 -> ("00044.340", "W"). Longitude gets
    3-digit degrees (000-179), latitude 2 (00-90), matching standard NMEA
    GGA/RMC fields. Ported from link/nmea.py (robot_repo) on 2026-09-19 --
    until now this side only ever needed the reverse direction in Python
    (see nmea_to_decimal below); NAV/RTE encoding happened entirely in
    this page's own JS (decimalToNmea in app.py). Kept in sync with the
    robot repo's copy per this module's own docstring."""
    direction = ("W" if value < 0 else "E") if is_longitude else ("S" if value < 0 else "N")
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60
    deg_digits = 3 if is_longitude else 2
    return f"{degrees:0{deg_digits}d}{minutes:06.3f}", direction


def nmea_to_decimal(raw: str, direction: str):
    """The inverse of decimal_to_nmea: parses a ddmm.mmmm string plus its
    direction letter back into signed decimal degrees. Returns None if
    `raw` isn't a usable number. Ported from link/nmea.py (robot_repo) on
    2026-09-19 for app.py's /api/map_data (decoding STA/WPT/GRT responses
    server-side) and for logging a NAV command's fields to the local
    history file (see app.py's _log_nav_command()) -- this project's own
    JS already had an equivalent (nmeaToDecimal), this just gives the
    Python side the same capability, kept in sync with the robot repo's
    copy per this module's own docstring."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    degrees = int(value // 100)
    minutes = value - degrees * 100
    decimal = degrees + minutes / 60
    if direction in ("S", "W"):
        decimal = -decimal
    return decimal

"""Binary-safe chunk framing shared by Pi #1 (robot_repo, sender of image
chunks -- see link/lora_image_streamer.py) and Pi #2 (robot-webserver,
receiver + chunk-retransmission requester -- see lora_link.py there)
over the E22-900T22U LoRa transparent-transmission link.

Why this exists alongside link/nmea.py's $PROV,...*checksum\\r\\n text
sentences rather than reusing them: that framing is ASCII/line-based (a
sentence ends at the first \\r\\n, and relies on no NUL/CR/LF/'$'/'*'/','
byte ever appearing INSIDE a field) -- perfectly fine for short text
fields (numbers, enum-like strings) but unsafe for genuinely arbitrary
binary payloads like a JPEG-encoded image chunk, which WILL contain every
one of those byte values somewhere sooner or later, silently corrupting/
desyncing the line reader. Rather than base64-encoding image chunks into
a text sentence field (a real option, but a permanent +33% size tax on
top of an already tiny 2.4 kbps / ~300 byte-per-second channel -- see the
2026-10-24 LoRa image-feasibility discussion), this module defines a
second, parallel framing that is genuinely binary-safe: every frame is
announced by one magic byte that can never collide with '$' (0x24, the
first byte of EVERY $PROV sentence), then a small FIXED-SIZE header that
says exactly how many payload bytes follow -- so the reader never has to
guess where a binary frame ends by scanning for a terminator the way the
text framing does.

Both sides must stay byte-for-byte identical -- same duplication
convention as link/nmea.py already being copied verbatim into
robot-webserver (that module's own docstring explains why: dependency-
free, stdlib-only, so there's nothing to "share" except by copying).
"""

import struct
import zlib

# First byte of every binary frame. Deliberately >= 0x80 (never a valid
# printable ASCII character, and in particular never '$' 0x24) so a
# reader peeking the very first incoming byte can always tell a binary
# frame apart from a $PROV,...*checksum\r\n text sentence before
# committing to either parser -- see lora_link.py's read loop.
MAGIC = 0xA5

FRAME_TYPE_IMAGE_CHUNK = 1

# magic(1) + frame_type(1) + image_id(1) + seq(2) + total(2) + length(2) + crc32(4) = 13 bytes
_HEADER_FORMAT = ">BBBHHHI"
HEADER_LENGTH = struct.calcsize(_HEADER_FORMAT)

# Keeps every frame comfortably under the E22 module's documented
# ~1000-byte transparent-transmission input buffer -- link/lora_server.
# py's LORA_MAX_SENTENCE_LENGTH already uses the same 900-byte ceiling
# for the text transport; this is the payload budget alone, the header
# above is extra (so a full frame tops out at HEADER_LENGTH +
# MAX_PAYLOAD_LENGTH = 873 bytes).
MAX_PAYLOAD_LENGTH = 860


class FrameError(ValueError):
    """Raised by decode_header() on a malformed frame header."""


def encode_image_chunk(image_id: int, seq: int, total: int, payload: bytes) -> bytes:
    """Builds one wire frame for image chunk `seq` of `total` (0-based,
    so the last chunk has seq == total - 1), belonging to image
    `image_id` (a 1-byte rolling counter -- see
    link.lora_image_streamer.LoraImageStreamer). `total` is repeated in
    EVERY chunk's header (not just the first) so the receiver still
    learns the true chunk count even if chunk 0 itself is the one that
    gets lost or garbled on the air."""
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise ValueError(f"payload too long for one frame: {len(payload)} > {MAX_PAYLOAD_LENGTH}")
    header = struct.pack(
        _HEADER_FORMAT, MAGIC, FRAME_TYPE_IMAGE_CHUNK, image_id & 0xFF,
        seq & 0xFFFF, total & 0xFFFF, len(payload), zlib.crc32(payload) & 0xFFFFFFFF,
    )
    return header + payload


def decode_header(header: bytes):
    """Parses just the HEADER_LENGTH-byte header (the first byte, the
    magic, has ALREADY been read and checked by the caller before it
    even decided to read the rest of a binary frame -- see
    read_one_frame() below). Returns (frame_type, image_id, seq, total,
    length, crc32). Raises FrameError if `header` isn't exactly
    HEADER_LENGTH bytes or its magic byte doesn't match (defensive --
    shouldn't happen given the caller's own pre-check, but never trust a
    byte stream blindly)."""
    if len(header) != HEADER_LENGTH:
        raise FrameError(f"header must be exactly {HEADER_LENGTH} bytes, got {len(header)}")
    magic, frame_type, image_id, seq, total, length, crc = struct.unpack(_HEADER_FORMAT, header)
    if magic != MAGIC:
        raise FrameError(f"bad magic byte: {magic:#x}")
    return frame_type, image_id, seq, total, length, crc


def read_one_frame(read_exact):
    """Reads exactly one binary frame using `read_exact(n)` -- a callable
    that blocks until it returns exactly n bytes, or returns None/fewer
    than n bytes on a timeout/EOF (see lora_link.py's/lora_server.py's
    serial-port adapters). The caller has ALREADY consumed and matched
    the single leading MAGIC byte before calling this (that's how it
    decided "binary frame" over "text sentence" in the first place), so
    this only reads the REMAINING HEADER_LENGTH - 1 header bytes, then
    `length` payload bytes.

    Returns a dict {frame_type, image_id, seq, total, payload, ok} on a
    structurally complete read (right number of header + payload bytes),
    with `ok` True only if the payload's CRC32 also matches -- a chunk
    the air garbled still comes back with a reliable seq/total (useful:
    the caller can mark exactly that chunk missing rather than losing
    track of the whole image), just with ok=False and payload=None.
    Returns None if the header or payload didn't arrive in full within
    read_exact's own timeout -- same "silent on no/partial data"
    contract as link/lora_server.py's own _serial_readline()."""
    rest_of_header = read_exact(HEADER_LENGTH - 1)
    if rest_of_header is None or len(rest_of_header) != HEADER_LENGTH - 1:
        return None
    header = bytes([MAGIC]) + bytes(rest_of_header)
    try:
        frame_type, image_id, seq, total, length, crc = decode_header(header)
    except FrameError:
        return None

    payload = read_exact(length) if length else b""
    if payload is None or len(payload) != length:
        return None
    payload = bytes(payload)
    ok = (zlib.crc32(payload) & 0xFFFFFFFF) == crc
    return {
        "frame_type": frame_type,
        "image_id": image_id,
        "seq": seq,
        "total": total,
        "payload": payload if ok else None,
        "ok": ok,
    }


def chunk_payload(data: bytes, max_payload=MAX_PAYLOAD_LENGTH):
    """Splits `data` into a list of byte chunks of at most `max_payload`
    bytes each -- the image-id/seq/total bookkeeping happens in the
    caller (link.lora_image_streamer), this is just the plain splitting
    arithmetic, kept here so both that module and any test for it share
    one implementation."""
    if not data:
        return [b""]
    return [data[i:i + max_payload] for i in range(0, len(data), max_payload)]

"""Serial-port client used by the Flask app to send NMEA-style commands
to the robot's control server over an E22-900T22U LoRa radio, as an
alternative to the WiFi/TCP path in robot_link.py -- see
link/lora_server.py (Pi #1, robot repo) for the matching listener. Both
sides speak the exact same $PROV,...*checksum \\r\\n sentences as the TCP
transport, so every sentence type in pages/protocole_controle.html works
unchanged over either one.

2026-10-24 (Fabrice): telemetry (everything except photos/video, which
stay on WiFi for now -- see the camera/media routes in app.py) is moving
onto this radio link to extend range past WiFi. app.py's
_send_command_with_fallback() tries this transport FIRST and only falls
back to robot_link.send_command() (WiFi/TCP) when no real response came
back over the radio -- a deliberate choice to keep WiFi as a safety net
while the radio link is brought up and tuned, not a hard cutover.

2026-10-24, SAME DAY (bigger update): this module now owns a single
background reader thread rather than reading the serial port directly
inside send_command(). Why that changed: Pi #1 now ALSO pushes
unsolicited binary image-chunk frames onto this exact same wire (the
automatic LoRa-fallback snapshot loop, see link/lora_image_streamer.py
in the robot repo), interleaved with the normal text replies -- a plain
synchronous "write a command, read the next line" call can no longer
assume the very next bytes on the wire are necessarily ITS reply; they
might be a binary frame instead (and critically, unlike a text sentence,
a binary frame does NOT reliably end at a \\r\\n -- its own bytes can
contain \\r/\\n by pure chance, which would desync a naive line reader
for everything that follows). So now ONE thread (_reader_loop) owns
every `ser.read()` call: it peeks the first byte of each unit and
dispatches to either the binary frame parser (link/binary_frame.py,
shared verbatim with the robot repo) or the existing text-sentence line
reader, routing binary frames into the image-reassembly state
(_handle_incoming_image_frame) and text lines into a small queue that
send_command() below waits on instead of reading the port itself.
Writing is still serialized through `_lock` (now covering writes only,
not reads) exactly like before, so only one command is ever "in
flight" at a time -- same request/reply assumption this protocol has
always made, just no longer coupled to also being the one doing the
reading.

Not yet wired to real hardware (module received, not installed) -- see
LORA_ENABLED below, which makes every call here a fast, honest
"LORA_DISABLED" result rather than trying (and failing slowly) to open a
port that was never plugged in.
"""

import logging
import os
import queue
import threading
import time

import binary_frame
from link_bytes import record_bytes_sent
from nmea import SentenceError, build_sentence, parse_sentence

log = logging.getLogger("lora_link")

LORA_ENABLED = os.environ.get("LORA_ENABLED", "0") == "1"
LORA_SERIAL_PORT = os.environ.get("LORA_SERIAL_PORT", "/dev/ttyUSB0")
LORA_BAUD_RATE = int(os.environ.get("LORA_BAUD_RATE", "9600"))

# Must match link/lora_server.py's own LORA_MAX_SENTENCE_LENGTH exactly
# (same hand-kept-in-sync convention as every *_FIELD_ORDER tuple in
# power_history_client.py). A sentence over this is rejected here,
# before ever touching the port -- never silently truncated (same
# "raise, don't truncate" call robot_link.py already makes for RTE, and
# for better reason here: the E22's ~1000-byte input buffer doesn't
# recover gracefully from an overrun).
LORA_MAX_SENTENCE_LENGTH = 900

# Short so the reader loop cycles often enough to service the stop event
# and the image-feedback timer below promptly -- the actual "did my
# command get a reply in time" wait is governed separately by
# send_command()'s own `timeout` against the reply queue, not by this.
_SERIAL_READ_TIMEOUT_S = 0.5

_lock = threading.Lock()  # serializes WRITES only, see module docstring
_serial_conn = None  # lazily opened pyserial Serial instance, or None

_reply_queue = queue.Queue()
_reader_thread = None
_reader_stop_event = None

# -- Image reassembly state (the LoRa-fallback snapshot loop's receiving
# end) -- one image "in flight" at a time, same as the sender's own
# one-at-a-time design (link/lora_image_streamer.py). Protected by its
# own lock, independent of `_lock` above (that one only ever guards
# writes; this one only ever guards this dict).
_image_lock = threading.Lock()
_image_state = {
    "image_id": None,
    "total": 0,
    "chunks": {},  # seq (int) -> payload bytes, only ever entries that passed CRC
    "last_chunk_at": 0.0,
    "last_feedback_at": 0.0,
    "last_feedback_kind": None,  # "ack" | "nak" | None
}

IMAGE_QUIET_PERIOD_S = 1.5        # how long with no new chunk before this image is "worth" feedback
IMAGE_FEEDBACK_RESEND_S = 2.0     # resend the same NAK/ACK this often, in case it got lost on the air
IMAGE_FEEDBACK_CHECK_INTERVAL_S = 0.5


def _get_connection():
    """Returns the shared serial connection, opening it on first use.
    Raises an exception (caught by callers) if pyserial isn't importable
    or the port can't be opened -- module not plugged in yet, wrong
    device path, permissions, etc. Caller must hold `_lock`."""
    global _serial_conn
    if _serial_conn is not None:
        return _serial_conn
    import serial  # deferred: only touched when LORA_ENABLED=1, so a
                    # webserver checkout without pyserial installed can
                    # still run with LoRa left off (the default).
    _serial_conn = serial.Serial(LORA_SERIAL_PORT, LORA_BAUD_RATE, timeout=_SERIAL_READ_TIMEOUT_S)
    _ensure_reader_started()
    return _serial_conn


def _close_connection():
    """Drops the shared connection so the next call reopens it from
    scratch. Caller must hold `_lock`."""
    global _serial_conn
    if _serial_conn is not None:
        try:
            _serial_conn.close()
        except Exception:
            pass
        _serial_conn = None


def _read_one_unit(ser):
    """Reads exactly ONE unit off `ser` -- either a text sentence line
    or a binary image-chunk frame -- by peeking the first byte: a match
    against link.binary_frame.MAGIC means a binary frame follows (read
    via binary_frame.read_one_frame, which knows its own exact length
    from the header, no terminator-scanning needed); anything else is
    the start of a $PROV,...*checksum\\r\\n text line, read the same way
    every other transport in this protocol already does.

    Returns ("binary", frame_dict), ("text", line_str), or None on a
    plain read timeout (nothing arrived) -- same "silent on no data"
    contract as link/lora_server.py's own _serial_readline()."""
    first = ser.read(1)
    if not first:
        return None

    if first[0] == binary_frame.MAGIC:
        def read_exact(n):
            buf = bytearray()
            while len(buf) < n:
                chunk = ser.read(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return bytes(buf)

        frame = binary_frame.read_one_frame(read_exact)
        return ("binary", frame) if frame is not None else None

    max_line_len = LORA_MAX_SENTENCE_LENGTH * 2
    buf = bytearray(first)
    terminator = b"\r\n"
    while len(buf) < max_line_len:
        b = ser.read(1)
        if not b:
            break
        buf += b
        if buf.endswith(terminator):
            return ("text", buf[: -len(terminator)].decode("ascii", errors="replace").strip())
    return ("text", buf.decode("ascii", errors="replace").strip())


def _handle_incoming_image_frame(frame: dict) -> None:
    """Folds one decoded binary_frame dict into _image_state -- a new
    image_id resets the reassembly buffer (newest image always wins,
    same "newest command wins" convention this project already uses for
    NAV/RTE/MOD elsewhere); a chunk that failed its CRC (frame["ok"] is
    False, payload None) is simply not recorded -- it stays "missing"
    exactly as if it had never arrived at all, which is exactly what
    makes it show up in the next IMGNAK."""
    with _image_lock:
        if frame["image_id"] != _image_state["image_id"]:
            _image_state["image_id"] = frame["image_id"]
            _image_state["total"] = frame["total"]
            _image_state["chunks"] = {}
            _image_state["last_feedback_at"] = 0.0
            _image_state["last_feedback_kind"] = None
            log.info("LoRa image %s: new capture started (%d chunk(s) expected)",
                     frame["image_id"], frame["total"])
        _image_state["total"] = frame["total"]  # may only become known once a later chunk arrives
        _image_state["last_chunk_at"] = time.time()
        if frame["ok"]:
            _image_state["chunks"][frame["seq"]] = frame["payload"]


def _missing_chunks_locked():
    """Caller must hold _image_lock. Returns the sorted list of 0-based
    chunk indices not yet received intact for the current image."""
    total = _image_state["total"]
    if not total:
        return []
    return [i for i in range(total) if i not in _image_state["chunks"]]


def _maybe_send_image_feedback():
    """Called periodically from the reader loop (never from a separate
    thread -- see its own comment) -- decides whether this is a good
    moment to tell Pi #1 either "I'm still missing these chunks"
    (IMGNAK) or "I have everything" (IMGACK) for whatever image is
    currently being reassembled, and sends at most one such sentence per
    call. Deliberately quiet (does nothing) while chunks are still
    actively arriving (IMAGE_QUIET_PERIOD_S) -- no point asking for a
    retransmission in the middle of the FIRST transmission pass -- and
    re-sends the same verdict at most every IMAGE_FEEDBACK_RESEND_S
    rather than on every single check, in case the feedback sentence
    itself gets lost on the air and Pi #1 is still waiting for it."""
    with _image_lock:
        image_id = _image_state["image_id"]
        total = _image_state["total"]
        if image_id is None or not total:
            return
        now = time.time()
        if now - _image_state["last_chunk_at"] < IMAGE_QUIET_PERIOD_S:
            return
        missing = _missing_chunks_locked()
        kind = "nak" if missing else "ack"
        if (
            _image_state["last_feedback_kind"] == kind
            and now - _image_state["last_feedback_at"] < IMAGE_FEEDBACK_RESEND_S
        ):
            return
        _image_state["last_feedback_at"] = now
        _image_state["last_feedback_kind"] = kind

    try:
        if kind == "nak":
            bitmask = 0
            for i in missing:
                bitmask |= 1 << i
            sentence = build_sentence("IMGNAK", image_id, total, format(bitmask, "x"))
        else:
            sentence = build_sentence("IMGACK", image_id)
        wire_bytes = (sentence + "\r\n").encode("ascii")
        with _lock:
            ser = _get_connection()
            ser.write(wire_bytes)
            record_bytes_sent(len(wire_bytes))
    except Exception as exc:
        log.warning("LoRa image feedback (%s) failed to send: %s", kind, exc)


def get_latest_lora_snapshot():
    """For app.py's display endpoint (/lora_snap.jpg, see that module):
    returns (image_id, jpeg_bytes, complete) for the currently/most
    recently reassembled image -- `jpeg_bytes` is the longest CONTIGUOUS
    prefix of chunks starting from seq 0 (a progressive JPEG stream only
    decodes meaningfully from the start forward: having chunk 5 without
    chunk 2 doesn't help a viewer render anything chunk 2 onward), and
    `complete` is True once every chunk up to `total` has been received.
    Returns (None, b"", False) if nothing has ever arrived yet."""
    with _image_lock:
        image_id = _image_state["image_id"]
        total = _image_state["total"]
        chunks = _image_state["chunks"]
        if image_id is None:
            return None, b"", False
        prefix = bytearray()
        seq = 0
        while seq in chunks:
            prefix += chunks[seq]
            seq += 1
        complete = total > 0 and seq >= total
        return image_id, bytes(prefix), complete


def _reader_loop(stop_event: threading.Event) -> None:
    """The ONE thread that ever calls ser.read() on this connection --
    see this module's docstring for why reads and writes were split
    apart. Also drives _maybe_send_image_feedback() on a simple timer,
    same cooperative single-thread-does-everything-on-its-own-turn
    pattern link/lora_server.py's serial loop uses on the Pi #1 side."""
    last_feedback_check = 0.0
    while not stop_event.is_set():
        with _lock:
            try:
                ser = _get_connection()
            except Exception as exc:
                ser = None
                log.warning("LoRa reader: port not available yet (%s)", exc)
        if ser is None:
            stop_event.wait(2.0)
            continue

        try:
            unit = _read_one_unit(ser)
        except Exception as exc:
            log.warning("LoRa reader: serial error (%s) -- reconnecting", exc)
            with _lock:
                _close_connection()
            stop_event.wait(1.0)
            continue

        if unit is not None:
            kind, payload = unit
            if kind == "binary":
                _handle_incoming_image_frame(payload)
            elif kind == "text" and payload:
                _reply_queue.put(payload)

        now = time.time()
        if now - last_feedback_check >= IMAGE_FEEDBACK_CHECK_INTERVAL_S:
            last_feedback_check = now
            _maybe_send_image_feedback()


def _ensure_reader_started():
    """Caller must hold `_lock`. Starts the single background reader
    thread the first time a connection is opened -- a no-op on every
    later call (LORA_ENABLED stays on, the connection just gets reused)."""
    global _reader_thread, _reader_stop_event
    if _reader_thread is not None and _reader_thread.is_alive():
        return
    _reader_stop_event = threading.Event()
    _reader_thread = threading.Thread(target=_reader_loop, args=(_reader_stop_event,), daemon=True)
    _reader_thread.start()


def send_command(command_body: str, timeout: float = 2.0) -> dict:
    """Same return contract as robot_link.send_command() (no
    `host`/`port` -- there's one radio, not a set of candidate IPs):
    always "ok" and "sent"; on a real round trip also
    "type"/"fields"/"raw_response"; otherwise "error". Never raises --
    app.py's _send_command_with_fallback() tells "Pi #1 actually
    answered over the radio" apart from "no, try WiFi" purely by whether
    "type" is present in the result, exactly like it already does for a
    reachable-but-ERR-answering WiFi host.

    2026-10-24: now waits on the shared reply queue (fed by the one
    background reader thread) instead of reading the serial port itself
    -- see this module's docstring. The queue is drained of any stale
    leftover entries before sending (best-effort: this protocol has
    never had per-command request IDs, so a reply that arrives late
    for a PREVIOUS timed-out command was always a latent risk of being
    misdelivered to whichever call reads next -- draining here doesn't
    eliminate that, just reduces the window, same as before this
    change)."""
    if not LORA_ENABLED:
        return {"ok": False, "sent": "", "error": "LORA_DISABLED"}

    command_body = (command_body or "").strip()
    parts = [p.strip() for p in command_body.split(",")]
    sentence_type, fields = parts[0], parts[1:]
    sentence = build_sentence(sentence_type, *fields)
    wire_bytes = (sentence + "\r\n").encode("ascii")

    if len(wire_bytes) > LORA_MAX_SENTENCE_LENGTH:
        return {
            "ok": False,
            "sent": sentence,
            "error": f"SENTENCE_TOO_LONG_FOR_LORA:{len(wire_bytes)}>{LORA_MAX_SENTENCE_LENGTH}",
        }

    while not _reply_queue.empty():
        try:
            _reply_queue.get_nowait()
        except queue.Empty:
            break

    with _lock:
        try:
            ser = _get_connection()
            ser.write(wire_bytes)
            # Counted the moment write() returns, same reasoning as
            # robot_link.py's own record_bytes_sent() call -- the bytes
            # are committed to the module either way, whatever happens
            # to the reply below. Shared counter (link_bytes.py) so
            # /control's "Link bytes (TX)" tile is one true total across
            # both transports.
            record_bytes_sent(len(wire_bytes))
        except Exception as exc:
            _close_connection()
            return {"ok": False, "sent": sentence, "error": f"SERIAL_ERROR:{exc}"}

    try:
        response_line = _reply_queue.get(timeout=timeout)
    except queue.Empty:
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


def send_raw_sentence(sentence: str) -> bool:
    """Writes one already-built $PROV,...*checksum sentence with no
    reply wait -- used by dgps_relay.py to push a COR correction batch
    once per second without blocking on (or risking stealing) the
    reply-queue slot send_command() above owns for real commands.
    Returns False (never raises) on any failure -- best-effort, same
    spirit as every other optional-hardware integration in this
    project; the correction stream resumes on its own next tick either
    way."""
    if not LORA_ENABLED:
        return False
    wire_bytes = (sentence + "\r\n").encode("ascii")
    if len(wire_bytes) > LORA_MAX_SENTENCE_LENGTH:
        log.warning("COR sentence too long for LoRa (%d > %d) -- dropped",
                    len(wire_bytes), LORA_MAX_SENTENCE_LENGTH)
        return False
    with _lock:
        try:
            ser = _get_connection()
            ser.write(wire_bytes)
            record_bytes_sent(len(wire_bytes))
        except Exception as exc:
            _close_connection()
            log.warning("COR send failed: %s", exc)
            return False
    return True

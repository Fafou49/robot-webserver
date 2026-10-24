"""Pi #2's NTRIP client + 1-second DGPS correction relay to Pi #1
(2026-10-24, explicit user request: "je crois que l'on oublie de
transmettre les corrections DGPS par lora... je veux que PI2 récupère
les correction et en envoie 1 par seconde à pi 1"). Replaces the retired
gps/dgps_transfer.py on Pi #1 (see that file's own deprecation docstring
in the robot repo): Pi #2 is the one with the reliable internet
connection, so it's the one that should hold the NTRIP client -- Pi #1
may have none at all once it's out at the test area ("zone d'essais"),
exactly the situation this whole LoRa effort exists for, and the old
script silently produced nothing whenever that happened.

Transport, one correction batch at a time: WiFi/TCP (robot_link.
send_command, same control port every other command uses) is tried
FIRST here -- the OPPOSITE of app.py's _send_command_with_fallback(),
which tries LoRa first for ordinary console commands. That's deliberate,
not an inconsistency: corrections need to reach Pi #1 every single
second regardless of which link is up, and WiFi is both faster and,
crucially, not the scarce shared resource the automatic camera-over-LoRa
fallback loop depends on (link/lora_image_streamer.py's "DGPS always
wins" token bucket already copes fine if corrections themselves end up
traveling over LoRa too -- but there's no reason to spend that budget on
them at all when WiFi can carry them for free). LoRa (lora_link.
send_raw_sentence -- fire-and-forget, no reply wait, so this thread is
never stuck for up to ~2s waiting on an ACK that doesn't matter: next
second's correction supersedes this one either way) is only used once
every WiFi candidate has failed to even connect -- precisely the "le
wifi n'est pas accessible par pi1" scenario the user described, and
exactly when the camera snap-over-LoRa loop also kicks in (see link/
server.py's _lora_snap_should_run on the Pi #1 side).

A dedicated RobotHostResolver instance, not app.py's shared one: this
module runs its own background threads, independent of any Flask
request, and robot_host.py's own docstring is explicit that resolvers
are never shared across unrelated callers (one path's connection failure
could otherwise wrongly rotate away from a host that's still fine for
the other). Cheap to just keep separate -- it converges to the right
candidate within one tick either way.

Correction batches are capped per COR sentence (COR_MAX_RAW_CHUNK_BYTES)
and split across several sentences when a given second's buffered RTCM
bytes exceed that -- real multi-constellation bursts can occasionally
run bigger than a single comfortable sentence (see the 2026-10-24
conversation's ~290-560 B/s measurement), and this keeps every COR
sentence safely under both transports' length limits (robot_link.
MAX_COMMAND_LENGTH=8000 on WiFi, lora_link.LORA_MAX_SENTENCE_LENGTH=900
on LoRa) with comfortable margin for base64's ~4/3 expansion plus
sentence framing overhead.
"""

import base64
import logging
import os
import threading
import time

from dotenv import load_dotenv

import lora_link
import robot_link
from robot_host import RobotHostResolver

log = logging.getLogger("dgps_relay")

# Same .env file / variable names the retired gps/dgps_transfer.py used
# on Pi #1 -- see that script's own deprecation docstring and this
# project's .env.example. Falls back to the public Centipede test
# account, same default as before, if no .env is provided.
load_dotenv()

NTRIP_CASTER = os.environ.get("NTRIP_CASTER", "crtk.net")
NTRIP_PORT = int(os.environ.get("NTRIP_PORT", "2101"))
NTRIP_MOUNTPOINT = os.environ.get("NTRIP_MOUNTPOINT", "NEAR")
NTRIP_USER = os.environ.get("NTRIP_USER", "centipede")
NTRIP_PASSWORD = os.environ.get("NTRIP_PASSWORD", "centipede")

# This relay is the only thing standing between "GPS corrections exist"
# and "nothing ever uses them" now that gps/dgps_transfer.py is retired,
# so it defaults ON (unlike LORA_ENABLED, which gates hardware that
# genuinely isn't wired in yet) -- set DGPS_RELAY_ENABLED=0 in .env to
# turn it off, e.g. while testing without NTRIP credentials at hand.
DGPS_RELAY_ENABLED = os.environ.get("DGPS_RELAY_ENABLED", "1") == "1"

# Mirrors app.py's own ROBOT_HOST_CANDIDATES/ROBOT_PORT reasoning exactly
# (see robot_host.py's module docstring) -- kept as this module's own
# copies, read straight from the same .env variables, rather than an
# import from app.py: app.py is the one that starts this module's
# background thread at startup (see start() below and its call site in
# app.py), so importing app.py from here would be circular.
ROBOT_HOST = os.environ.get("ROBOT_HOST", "192.168.1.180")
ROBOT_HOST_CANDIDATES = [h for h in os.environ.get("ROBOT_HOST_CANDIDATES", ROBOT_HOST).split(",")]
ROBOT_PORT = int(os.environ.get("ROBOT_PORT", "5050"))
_resolver = RobotHostResolver(ROBOT_HOST_CANDIDATES)

RELAY_INTERVAL_S = 1.0  # explicit user requirement: "en envoie 1 par seconde"
WIFI_SEND_TIMEOUT_S = 1.5  # generous for a local-LAN round trip, short enough to still leave slack before the next 1Hz tick

# See this module's docstring, "Correction batches are capped" paragraph.
COR_MAX_RAW_CHUNK_BYTES = 600

_buffer_lock = threading.Lock()
_buffer = bytearray()

_stats_lock = threading.Lock()
_stats = {
    "last_relay_at": 0.0,
    "last_transport": None,  # "wifi" | "lora" | "failed" | None (nothing sent yet)
    "total_bytes_relayed": 0,
    "total_chunks_sent": 0,
    "total_chunks_failed": 0,
}

_relay_thread = None
_relay_stop_event = None
_ntrip_thread = None


def _on_rtcm_data(raw: bytes) -> None:
    """GNSSNTRIPClient's callback, invoked from ITS OWN background
    thread (pygnssutils spins one up internally) every time a new RTCM
    block arrives from the caster -- that can happen several times a
    second, not synced to this module's own 1 Hz relay timer at all.
    Just appends to the shared buffer; _relay_loop below is the only
    other thing that ever touches it, so the two threads only ever need
    to coordinate through this one lock."""
    if not raw:
        return
    with _buffer_lock:
        _buffer.extend(raw)


def _drain_buffer() -> bytes:
    """Atomically takes everything buffered since the last call and
    empties the buffer -- called once per relay tick."""
    with _buffer_lock:
        if not _buffer:
            return b""
        data = bytes(_buffer)
        _buffer.clear()
        return data


def _chunk_raw_bytes(data: bytes, max_chunk=COR_MAX_RAW_CHUNK_BYTES):
    return [data[i:i + max_chunk] for i in range(0, len(data), max_chunk)] or []


def _send_one_correction_chunk(raw_chunk: bytes) -> str:
    """Sends one base64-encoded COR sentence for `raw_chunk` (already
    small enough for either transport, see COR_MAX_RAW_CHUNK_BYTES).
    Returns "wifi" or "lora" on a successful send, "failed" if neither
    transport could deliver it. WiFi tried first across every resolver
    candidate (same rotate-on-connection-failure logic as app.py's
    _send_command_with_fallback(), just against this module's own
    resolver instance, see module docstring) -- falls through to LoRa
    only once every WiFi candidate has failed to even connect."""
    b64 = base64.b64encode(raw_chunk).decode("ascii")
    command_body = f"COR,{b64}"

    for host in _resolver.ordered():
        result = robot_link.send_command(command_body, host, ROBOT_PORT, timeout=WIFI_SEND_TIMEOUT_S)
        if result.get("error", "").startswith("CONNECTION_ERROR"):
            _resolver.report_failure(host)
            continue
        _resolver.report_success(host)
        if result.get("type") != "ACK":
            log.warning("COR over WiFi (%s) got a non-ACK reply: %s", host, result.get("raw_response"))
        return "wifi"  # host answered either way -- a reachable Pi #1 isn't a transport failure

    from nmea import build_sentence  # deferred: only needed on this (rarer) LoRa fallback path
    sentence = build_sentence("COR", b64)
    if lora_link.send_raw_sentence(sentence):
        return "lora"
    return "failed"


def _relay_tick() -> None:
    """One full relay cycle: drain whatever RTCM bytes have piled up
    since the last tick, split into sentence-sized chunks, send each in
    turn. A quiet second (no corrections buffered -- e.g. the NTRIP
    connection is still coming up) is simply a no-op, not an error."""
    data = _drain_buffer()
    if not data:
        return
    chunks = _chunk_raw_bytes(data)
    for raw_chunk in chunks:
        transport = _send_one_correction_chunk(raw_chunk)
        with _stats_lock:
            _stats["last_relay_at"] = time.time()
            _stats["last_transport"] = transport
            if transport == "failed":
                _stats["total_chunks_failed"] += 1
                log.warning("DGPS correction chunk (%d bytes) could not be relayed over WiFi or LoRa", len(raw_chunk))
            else:
                _stats["total_bytes_relayed"] += len(raw_chunk)
                _stats["total_chunks_sent"] += 1


def _relay_loop(stop_event: threading.Event) -> None:
    """Runs _relay_tick() roughly once per RELAY_INTERVAL_S until
    `stop_event` is set -- accounts for the tick's own processing time
    so a slow WiFi-candidate-rotation tick doesn't compound into drift
    over a long session."""
    while not stop_event.is_set():
        tick_start = time.time()
        try:
            _relay_tick()
        except Exception:
            log.exception("DGPS relay tick failed")
        elapsed = time.time() - tick_start
        stop_event.wait(max(0.0, RELAY_INTERVAL_S - elapsed))


def _start_ntrip_client() -> None:
    """Starts pygnssutils' NTRIP client -- same call shape the retired
    gps/dgps_transfer.py used on Pi #1, just with the callback now
    feeding this module's buffer instead of writing straight to a GPS
    serial port (that write happens on Pi #1 now, via link/
    dgps_injector.py, once the COR sentence arrives -- see that module
    and link/server.py's COR handler). Runs in its own thread because
    GNSSNTRIPClient.run() is itself non-blocking internally (it spins up
    its own worker and returns), but importing pygnssutils / resolving
    the caster can briefly block, and this must never hold up Flask's
    own startup."""
    from pygnssutils import GNSSNTRIPClient  # deferred: optional dependency, see requirements.txt

    client = GNSSNTRIPClient(None)
    log.info("DGPS relay: connecting to NTRIP caster %s:%s (mountpoint %s)",
              NTRIP_CASTER, NTRIP_PORT, NTRIP_MOUNTPOINT)
    try:
        client.run(
            server=NTRIP_CASTER,
            port=NTRIP_PORT,
            mountpoint=NTRIP_MOUNTPOINT,
            user=NTRIP_USER,
            password=NTRIP_PASSWORD,
            callback=_on_rtcm_data,
        )
    except Exception:
        log.exception("DGPS relay: NTRIP client failed to start -- corrections will not flow "
                       "until the webserver is restarted (or DGPS_RELAY_ENABLED=0 set deliberately)")


def start() -> threading.Event:
    """Starts both background threads (NTRIP client, 1Hz relay loop)
    when DGPS_RELAY_ENABLED, else returns an already-set stop_event and
    does nothing else -- same on/off convention as link/lora_server.py's
    start_lora_listener() on the Pi #1 side. Call once, at app.py
    startup. Safe to call even without pygnssutils installed or without
    real NTRIP credentials configured: both failure modes are caught and
    logged (see _start_ntrip_client above), never raised up into Flask's
    own startup path."""
    global _relay_thread, _relay_stop_event, _ntrip_thread

    stop_event = threading.Event()
    if not DGPS_RELAY_ENABLED:
        log.info("DGPS relay disabled (DGPS_RELAY_ENABLED=0) -- no corrections will be sent to Pi #1")
        stop_event.set()
        return stop_event

    _relay_stop_event = stop_event
    _ntrip_thread = threading.Thread(target=_start_ntrip_client, daemon=True)
    _ntrip_thread.start()

    _relay_thread = threading.Thread(target=_relay_loop, args=(stop_event,), daemon=True)
    _relay_thread.start()
    log.info("DGPS relay started: NTRIP -> %s chunks/s over WiFi (LoRa fallback) to Pi #1", RELAY_INTERVAL_S)
    return stop_event


def get_relay_stats() -> dict:
    """A snapshot of the relay's own recent activity -- for a future
    status tile/log line, not currently wired to any route. Never
    raises, cheap to call often."""
    with _stats_lock:
        return dict(_stats)

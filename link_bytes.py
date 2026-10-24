"""Process-wide counter of bytes sent to the robot's control server,
whichever transport actually carried them -- WiFi/TCP via robot_link.py,
or the E22-900T22U LoRa radio via lora_link.py, once that's wired in.

Shared between the two transport modules (rather than each keeping its
own) so /control's status bar ("Link bytes (TX)", see app.py) shows one
true total regardless of which link is currently in use: the whole point
of this number is to have a real read on how much traffic this project
pushes, as a budget check for the much slower LoRa link -- splitting it
in two would just hide that.

Process-local and reset on every webserver restart -- fine for the
single `python3 app.py` process this project runs; a multi-worker WSGI
deployment would keep one counter per worker, not a shared total.
"""

import threading

_lock = threading.Lock()
_bytes_sent_since_boot = 0


def record_bytes_sent(n: int) -> None:
    global _bytes_sent_since_boot
    with _lock:
        _bytes_sent_since_boot += n


def get_bytes_sent_since_boot() -> int:
    with _lock:
        return _bytes_sent_since_boot

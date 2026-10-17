"""Picks which IP currently reaches Pi #1, when more than one is possible
-- e.g. its Bbox (home) fixed IP and its temporary Pixel-hotspot IP while
the robot is out testing on mobile data (2026-10-17, explicit user
request: avoir les deux jeux d'IP qui autorisent les flux, pour ne plus
avoir a toucher .env a chaque aller-retour entre la maison et la zone
d'essais).

Pi #1 itself still only ever has ONE real IP at a time -- whichever
network it's actually joined to right now. This module doesn't make both
addresses work simultaneously (that's not possible, a given interface is
on one network at a time); it just means app.py no longer needs to be
manually edited and restarted every time the robot switches between the
home network and a temporary hotspot: it tries the last address that
worked, and if that one's gone quiet, it rotates through the other
configured candidates until one answers.

Deliberately NOT a DNS/mDNS-based fix (e.g. "pi1.local") even though that
would sidestep the whole problem: every caller in app.py already reaches
Pi #1 server-side (see media_camera() -- the browser only ever talks to
this Flask server, never directly to Pi #1), so there's no browser-side
mDNS resolution involved here to begin with. A plain candidate list,
tried from this one server process, has no dependency on whether some
future client's OS/hotspot happens to support multicast DNS.

Strategy: sticky-with-fallback, not a background health-check poll.
The resolver remembers whichever candidate last worked for a given
service and keeps offering it first -- the normal case (nothing changed
since the last request) costs nothing extra. Only when a caller actually
tries a candidate and it fails does the resolver rotate that candidate
to the back of the list. This means the FIRST request after the robot
switches networks pays for trying the now-dead old IP once (bounded by
that call's own timeout) before reaching the right one; every request
after that is back to costing nothing, until the network changes again.

IMPORTANT -- one resolver per independent service/port, never shared:
Pi #1's control server (port 5050) and its camera stream (port 8000) are
two separate processes that can each be up or down independently of the
other (e.g. the camera script crashed but link/server.py is still fine).
If a single resolver's "this host failed" came from a camera request, it
would wrongly rotate away from a host that's still perfectly good for
control commands, and vice versa. app.py therefore keeps two separate
RobotHostResolver instances (one per port) built from the same candidate
list, each learning independently which address currently works for ITS
OWN service.
"""
import threading


class RobotHostResolver:
    """Tracks, for ONE specific service (one port), which of several
    candidate hosts currently answers. See this module's docstring for
    the overall reasoning."""

    def __init__(self, candidates):
        candidates = [c.strip() for c in candidates if c and c.strip()]
        if not candidates:
            raise ValueError("RobotHostResolver needs at least one candidate host")
        # De-duplicated, order preserved (first = current best guess).
        self._candidates = list(dict.fromkeys(candidates))
        self._lock = threading.Lock()

    def ordered(self):
        """Every candidate, current best guess first -- for a caller that
        wants to try them all in turn (report_success/report_failure as it
        goes) rather than calling this repeatedly."""
        with self._lock:
            return list(self._candidates)

    def report_success(self, host):
        """Moves `host` to the front, so it's offered first next time.
        No-op if it's already there, or if it's not a known candidate."""
        with self._lock:
            if host in self._candidates and self._candidates[0] != host:
                self._candidates.remove(host)
                self._candidates.insert(0, host)

    def report_failure(self, host):
        """Moves `host` to the back, so the next `ordered()` call offers a
        different candidate first instead of retrying the one that just
        failed. No-op if `host` isn't a known candidate, or isn't
        currently at the front (a late/duplicate report for a candidate
        that's already been superseded by a more recent success/failure)."""
        with self._lock:
            if self._candidates and self._candidates[0] == host and len(self._candidates) > 1:
                self._candidates.append(self._candidates.pop(0))

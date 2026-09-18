"""RPC verb classification for the daemon socket gate.

These sets used to live in ``brainview.py``, which imported them into the
socket handler. The brain-view dashboard has been removed, but the socket
handler still needs to know two things about a verb:

1. whether it is safe to serve off a lock-free read-only snapshot, and
2. whether it is OBSERVATION rather than user work.

Both are properties of the verb, not of any particular client, so they belong
here rather than in whichever surface happens to consume them next.
"""

from __future__ import annotations

#: Verbs that are read-only by construction, and therefore safe to serve off
#: the lock-free RO snapshot when the writable store is unavailable (relay busy
#: plus write-open fenced).
READ_ONLY_VERBS = frozenset({"overview", "graph", "economy", "events", "browse"})

#: OBSERVATION, not user work: these must NEVER reset the daemon's activity
#: clock, or a client polling them keeps the sleep pipeline from ever seeing an
#: idle window. Browsing one's own memory is watching, not working.
OBSERVATION_VERBS = READ_ONLY_VERBS | {"search", "surface"}

__all__ = ["READ_ONLY_VERBS", "OBSERVATION_VERBS"]

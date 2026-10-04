"""In-process per-user locks.

Production runs one gunicorn worker, so a thread lock (plus a DB row lock where
it matters) covers concurrency. Keyed by (name, user_id) so one user's work
never blocks — or gets skipped because of — another's.
"""

import threading

_user_locks: dict[tuple[str, int], threading.Lock] = {}
_user_locks_guard = threading.Lock()


def user_lock(name: str, user_id: int) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks.setdefault((name, user_id), threading.Lock())

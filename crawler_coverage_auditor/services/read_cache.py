"""Short, revision-checked memoization of local inventory reads only."""

from collections import OrderedDict
from copy import deepcopy
from functools import wraps
from pathlib import Path
from threading import RLock
from time import monotonic

_RESULTS = OrderedDict()
_LOCK = RLock()


def database_revision(database):
    path = Path(database).resolve()
    result = []
    for item in (path, Path(str(path) + "-wal")):
        try:
            stat = item.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def cached_read(function):
    @wraps(function)
    def wrapped(database, *args, **kwargs):
        # Keep data copies isolated: a route decorating a result cannot corrupt
        # a later response. WAL replacement/writes invalidate immediately.
        path = str(Path(database).resolve())
        key = (function.__name__, path, args, tuple(sorted(kwargs.items())))
        with _LOCK:
            revision, now = database_revision(database), monotonic()
            previous = _RESULTS.get(key)
            if previous and previous[0] == revision and now - previous[1] < 1.0:
                _RESULTS.move_to_end(key)
                return deepcopy(previous[2])
            value = function(database, *args, **kwargs)
            if revision == database_revision(database):
                _RESULTS[key] = (revision, now, deepcopy(value))
                while len(_RESULTS) > 64:
                    _RESULTS.popitem(last=False)
            return value

    return wrapped

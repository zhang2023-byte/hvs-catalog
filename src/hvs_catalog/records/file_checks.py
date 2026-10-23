"""Reuse file verification only within one synchronous validation operation."""

import hashlib
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_ACTIVE = ContextVar("record_file_verification", default=None)
_CONTENT_LIMIT = 16 * 1024 * 1024


@contextmanager
def verification_scope():
    if _ACTIVE.get() is not None:
        yield
        return
    token = _ACTIVE.set(
        {"digests": {}, "contents": OrderedDict(), "values": {}, "bytes": 0}
    )
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def scoped_verification(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with verification_scope():
            return function(*args, **kwargs)

    return wrapped


def _key(path):
    # Containment belongs to the caller. stat follows links and identifies the
    # current file, including replacements; do not repeat full path resolution.
    path = Path(path).absolute()
    stat = path.stat()
    return (
        path,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def file_digest(path):
    key = _key(path)
    cache = _ACTIVE.get()
    if cache is not None and key in cache["digests"]:
        return cache["digests"][key]
    digest = hashlib.sha256()
    with key[0].open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    if cache is not None:
        cache["digests"][key] = result
    return result


def file_content(path):
    key = _key(path)
    cache = _ACTIVE.get()
    if cache is not None and key in cache["contents"]:
        return cache["contents"][key]
    payload = key[0].read_bytes()
    result = (payload, hashlib.sha256(payload).hexdigest())
    if cache is not None:
        cache["digests"][key] = result[1]
        if len(payload) <= _CONTENT_LIMIT:
            while cache["bytes"] + len(payload) > _CONTENT_LIMIT:
                _, previous = cache["contents"].popitem(last=False)
                cache["bytes"] -= len(previous[0])
            cache["contents"][key] = result
            cache["bytes"] += len(payload)
    return result


def checked_value(kind, key, compute):
    cache = _ACTIVE.get()
    if cache is None:
        return compute()
    identity = (kind, key)
    if identity not in cache["values"]:
        cache["values"][identity] = compute()
    return cache["values"][identity]

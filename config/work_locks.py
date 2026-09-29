"""Nonblocking session locks shared by web, cron and healer workers.

Production uses PostgreSQL advisory locks, released even on process death.
SQLite's process-local fallback is only for development and unit tests.
"""
from contextlib import contextmanager
from hashlib import blake2b
from threading import Lock

from django.db import connection

_local = [Lock() for _ in range(128)]


@contextmanager
def work_lock(name):
    key = int.from_bytes(blake2b(name.encode(), digest_size=8).digest(),
                         "big", signed=True)
    if connection.vendor != "postgresql":
        lock = _local[key % len(_local)]
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = cursor.fetchone()[0]
    session = connection.connection
    try:
        yield acquired
    finally:
        if acquired and connection.connection is session and not session.closed:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
            except Exception:
                # Never leave a lock in a pooled/reused failed connection.
                connection.close()
                raise

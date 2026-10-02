"""Local account-wide RPM and in-flight leases, shared by models and processes.

Adapted from Stella HVS provider_rate_limit.py, MIT, Copyright (c) 2026
Zhang Jian. This implementation gates each physical request, not an outer retry.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import RuntimeConfig


class AccountQuota:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        config.quota_store.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quota_groups (
                    name TEXT PRIMARY KEY, rpm INTEGER, concurrency INTEGER,
                    cooldown REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS quota_calls (
                    group_name TEXT, started REAL);
                CREATE INDEX IF NOT EXISTS quota_calls_time
                    ON quota_calls(group_name, started);
                CREATE TABLE IF NOT EXISTS quota_leases (
                    id TEXT PRIMARY KEY, group_name TEXT, expires REAL);
                CREATE INDEX IF NOT EXISTS quota_leases_expiry
                    ON quota_leases(group_name, expires);
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO quota_groups(name,rpm,concurrency) VALUES(?,?,?)",
                (config.quota_group, config.rpm, config.concurrency),
            )
            row = db.execute(
                "SELECT rpm,concurrency FROM quota_groups WHERE name=?",
                (config.quota_group,),
            ).fetchone()
            if row != (config.rpm, config.concurrency):
                raise ValueError(
                    "shared account quota configuration conflicts with another process"
                )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.config.quota_store, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def acquire(self, *, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (timeout or self.config.total_timeout)
        cfg = self.config
        while True:
            now = time.time()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM quota_calls WHERE group_name=? AND started<=?",
                    (cfg.quota_group, now - 60),
                )
                db.execute(
                    "DELETE FROM quota_leases WHERE group_name=? AND expires<=?",
                    (cfg.quota_group, now),
                )
                cooldown = db.execute(
                    "SELECT cooldown FROM quota_groups WHERE name=?", (cfg.quota_group,)
                ).fetchone()[0]
                count = db.execute(
                    "SELECT COUNT(*) FROM quota_calls WHERE group_name=?",
                    (cfg.quota_group,),
                ).fetchone()[0]
                active = db.execute(
                    "SELECT COUNT(*) FROM quota_leases WHERE group_name=?",
                    (cfg.quota_group,),
                ).fetchone()[0]
                if cooldown <= now and count < cfg.rpm and active < cfg.concurrency:
                    lease = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO quota_leases VALUES(?,?,?)",
                        (lease, cfg.quota_group, now + cfg.total_timeout + 30),
                    )
                    db.execute(
                        "INSERT INTO quota_calls VALUES(?,?)", (cfg.quota_group, now)
                    )
                    return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("account quota wait expired before sending")
            time.sleep(min(0.05, remaining))

    def release(self, lease: str):
        with self._connect() as db:
            db.execute(
                "DELETE FROM quota_leases WHERE id=? AND group_name=?",
                (lease, self.config.quota_group),
            )

    def cooldown(self, seconds: float):
        with self._connect() as db:
            db.execute(
                "UPDATE quota_groups SET cooldown=MAX(cooldown,?) WHERE name=?",
                (time.time() + max(0, seconds), self.config.quota_group),
            )

    @contextmanager
    def permit(self):
        lease = self.acquire()
        try:
            yield lease
        finally:
            self.release(lease)

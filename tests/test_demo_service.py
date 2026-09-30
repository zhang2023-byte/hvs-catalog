"""Shared service facts persist independently of client backups and rollback."""

import json
import sqlite3

import pytest

from hvs_catalog.pipeline.mock import PAPER_B, PipelineMockService, material_spec


def test_external_call_survives_reopen_and_client_rollback(tmp_path):
    path = tmp_path / "calls.jsonl"
    client = sqlite3.connect(tmp_path / "client.db")
    client.execute("CREATE TABLE results (result TEXT)")
    client.execute("BEGIN")
    service = PipelineMockService(path)
    response = service.call(
        step="direct_roster",
        request={"step": "direct_roster"},
        material=material_spec(PAPER_B),
    )
    client.execute("INSERT INTO results VALUES ('not committed')")
    client.rollback()
    client.close()
    assert response["entries"][0]["identifier"] == "FIC-B-1"
    reopened = PipelineMockService(path)
    assert reopened.summary()["calls"] == 1
    reopened.call(
        step="direct_roster",
        request={"step": "direct_roster"},
        material=material_spec(PAPER_B),
    )
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]["request_digest"] == entries[1]["request_digest"]
    assert "cost" not in entries[0]


def test_unknown_tokens_remain_null(tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_text(
        json.dumps({"step": "unknown", "request_digest": "a" * 64, "tokens": None})
        + "\n"
    )
    assert PipelineMockService(path).summary()["tokens"] is None


def test_retired_sqlite_not_migrated_or_appended(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calls (cost REAL NOT NULL)")
    conn.execute("INSERT INTO calls VALUES (0.5)")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="另选新 JSONL"):
        PipelineMockService(path)
    assert path.read_bytes() == before

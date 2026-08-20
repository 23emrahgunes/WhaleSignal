from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import p26_baseline_freeze as freeze_mod
from p26_baseline_freeze import BaselineFreezer, verify_manifest
from p26_config import P26Settings


def _make_p25_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE markets(condition_id TEXT PRIMARY KEY, resolved INTEGER);
        CREATE TABLE snapshots(id INTEGER PRIMARY KEY, condition_id TEXT);
        CREATE TABLE forecasts(id INTEGER PRIMARY KEY, condition_id TEXT);
        CREATE TABLE paper_trades(
            id INTEGER PRIMARY KEY,
            condition_id TEXT,
            strategy_version TEXT,
            status TEXT,
            realized_pnl REAL
        );
        INSERT INTO markets VALUES ('c1',1),('c2',0);
        INSERT INTO snapshots VALUES (1,'c1'),(2,'c2');
        INSERT INTO forecasts VALUES (1,'c1');
        INSERT INTO paper_trades VALUES
          (1,'c1','RESEARCH_PAPER_V1','SETTLED',1.25),
          (2,'c2','RESEARCH_PAPER_V2','OPEN',NULL);
        """
    )
    conn.commit()
    conn.close()


def _git_repo(path: Path, *, dirty: bool = False) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
    )
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=path,
        check=True,
    )
    if dirty:
        (path / "tracked.txt").write_text("dirty\n", encoding="utf-8")


def _settings(tmp_path: Path, db_path: Path) -> P26Settings:
    model = tmp_path / "model.pkl"
    calibration = tmp_path / "calibration.pkl"
    model.write_bytes(b"model")
    calibration.write_bytes(b"calibration")
    return P26Settings(
        p25_db_path=str(db_path),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        backup_root=str(tmp_path / "backups"),
        p25_model_path=str(model),
        p25_calibration_path=str(calibration),
        p25_state_url="http://unit.test/state",
        p25_paper_summary_url="http://unit.test/paper",
        baseline_require_clean_git=True,
    )


def test_baseline_freeze_backup_exports_manifest_and_verifies(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "p25.sqlite"
    _make_p25_db(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    settings = _settings(tmp_path, db_path)

    def fake_fetch(url: str, timeout: float):
        return {
            "ok": True,
            "url": url,
            "timeout": timeout,
            "live_orders": 0,
        }

    monkeypatch.setattr(freeze_mod, "fetch_json", fake_fetch)
    manifest_path = BaselineFreezer(settings, repo).freeze(
        baseline_id="TEST_FREEZE"
    )
    result = verify_manifest(manifest_path)
    assert result["ok"] is True

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format_version"] == "P26_BASELINE_FREEZE_V2"
    assert manifest["db_integrity_check"] == "ok"
    assert manifest["git_dirty"] is False
    assert manifest["paper_v1_count"] == 1
    assert manifest["table_row_counts"]["paper_trades"] == 2
    assert manifest["source_snapshot_row_counts"] == manifest["table_row_counts"]
    assert manifest["safety"]["p25_database_mutated"] is False
    assert manifest["safety"]["p25_writes_allowed_during_snapshot"] is True
    root = manifest_path.parent
    exported = json.loads(
        (root / "paper_v1.json").read_text(encoding="utf-8")
    )
    assert [row["strategy_version"] for row in exported] == [
        "RESEARCH_PAPER_V1"
    ]
    source_count = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM paper_trades"
    ).fetchone()[0]
    assert source_count == 2


def test_live_write_after_snapshot_is_audited_not_treated_as_corruption(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "p25.sqlite"
    _make_p25_db(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    settings = _settings(tmp_path, db_path)
    monkeypatch.setattr(freeze_mod, "fetch_json", lambda *_: {"ok": True})

    original_backup = freeze_mod.sqlite_online_backup

    def backup_then_live_write(source_path, destination_path, **kwargs):
        proof = original_backup(source_path, destination_path, **kwargs)
        conn = sqlite3.connect(source_path)
        try:
            conn.execute(
                "INSERT INTO forecasts(id,condition_id) VALUES (?,?)",
                (2, "c2"),
            )
            conn.execute(
                "INSERT INTO snapshots(id,condition_id) VALUES (?,?)",
                (3, "c1"),
            )
            conn.commit()
        finally:
            conn.close()
        return proof

    monkeypatch.setattr(
        freeze_mod, "sqlite_online_backup", backup_then_live_write
    )
    manifest_path = BaselineFreezer(settings, repo).freeze(
        baseline_id="LIVE_WRITER"
    )
    result = verify_manifest(manifest_path)
    assert result["ok"] is True
    assert result["source_changed_after_snapshot"] is True

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["table_row_counts"] == manifest["source_snapshot_row_counts"]
    assert manifest["source_live_after_row_counts"]["forecasts"] == 2
    assert manifest["source_snapshot_row_counts"]["forecasts"] == 1
    assert manifest["source_live_after_row_counts"]["snapshots"] == 3
    assert manifest["source_snapshot_row_counts"]["snapshots"] == 2
    assert manifest["source_changed_after_snapshot"] is True


def test_baseline_freeze_rejects_dirty_worktree(tmp_path, monkeypatch):
    db_path = tmp_path / "p25.sqlite"
    _make_p25_db(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo, dirty=True)
    settings = _settings(tmp_path, db_path)
    monkeypatch.setattr(freeze_mod, "fetch_json", lambda *_: {"ok": True})
    with pytest.raises(RuntimeError, match="WORKTREE_DIRTY"):
        BaselineFreezer(settings, repo).freeze(baseline_id="DIRTY")


def test_manifest_detects_tampering(tmp_path, monkeypatch):
    db_path = tmp_path / "p25.sqlite"
    _make_p25_db(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    settings = _settings(tmp_path, db_path)
    monkeypatch.setattr(freeze_mod, "fetch_json", lambda *_: {"ok": True})
    manifest_path = BaselineFreezer(settings, repo).freeze(
        baseline_id="TAMPER"
    )
    (manifest_path.parent / "paper_v1.json").write_text(
        "[]\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="sha256"):
        verify_manifest(manifest_path)


def test_manifest_detects_schema_tampering(tmp_path, monkeypatch):
    db_path = tmp_path / "p25.sqlite"
    _make_p25_db(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    settings = _settings(tmp_path, db_path)
    monkeypatch.setattr(freeze_mod, "fetch_json", lambda *_: {"ok": True})
    manifest_path = BaselineFreezer(settings, repo).freeze(
        baseline_id="SCHEMA_TAMPER"
    )
    backup = manifest_path.parent / "direction_engine.sqlite"
    conn = sqlite3.connect(backup)
    conn.execute("CREATE TABLE injected_after_freeze(x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema_sha256|row_counts"):
        verify_manifest(manifest_path)


def test_p26_config_rejects_shared_database(tmp_path):
    settings = P26Settings(
        p25_db_path=str(tmp_path / "same.sqlite"),
        p26_db_path=str(tmp_path / "same.sqlite"),
    )
    with pytest.raises(ValueError, match="separate"):
        settings.validate_research_safety()

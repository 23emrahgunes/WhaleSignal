"""P2.6.0 baseline freeze and integrity verification.

The freeze operation is read-only with respect to the live P2.5 database. It
pins one SQLite read transaction, captures row counts and schema from that exact
snapshot, and runs the online backup from the same connection. Writes that land
in P2.5 after the snapshot are recorded as live growth, not misclassified as
backup corruption.

The module exports RESEARCH_PAPER_V1, snapshots runtime JSON, creates an atomic
SHA-256 manifest and never restores a database automatically.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from p26_config import P26Settings, get_p26_settings


BASELINE_FORMAT_VERSION = "P26_BASELINE_FREEZE_V2"
PAPER_V1_STRATEGY = "RESEARCH_PAPER_V1"


def utc_stamp(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)


def git_output(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_state(cwd: Path) -> dict[str, Any]:
    try:
        return {
            "commit": git_output(["rev-parse", "HEAD"], cwd),
            "branch": git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
            "dirty_lines": [
                line
                for line in git_output(["status", "--porcelain"], cwd).splitlines()
                if line
            ],
        }
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {
            "commit": "UNKNOWN",
            "branch": "UNKNOWN",
            "dirty_lines": ["GIT_UNAVAILABLE"],
        }


def _schema_sql_from_connection(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    return "\n".join(str(row[3]) for row in rows)


def _table_row_counts_from_connection(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        )
    return counts


@dataclass(frozen=True)
class SQLiteSnapshotProof:
    row_counts: dict[str, int]
    schema_sql: str
    snapshot_started_at_ms: int
    snapshot_completed_at_ms: int


def sqlite_online_backup(
    source_path: Path,
    destination_path: Path,
    *,
    pages: int = 256,
    sleep: float = 0.01,
) -> SQLiteSnapshotProof:
    """Back up one pinned read snapshot and return its audit proof.

    P2.5 is expected to keep writing while this runs. Therefore callers must
    compare the backup with ``row_counts`` returned here, not with a fresh count
    query executed after the backup has completed.
    """
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_path.with_suffix(destination_path.suffix + ".tmp")
    temp_path.unlink(missing_ok=True)

    source_uri = f"file:{source_path.resolve()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30.0)
    destination = sqlite3.connect(temp_path, timeout=30.0)
    started_ms = int(time.time() * 1000)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA busy_timeout=30000")
        source.execute("BEGIN")
        snapshot_counts = _table_row_counts_from_connection(source)
        snapshot_schema = _schema_sql_from_connection(source)
        source.backup(destination, pages=pages, sleep=sleep)
        destination.commit()
        completed_ms = int(time.time() * 1000)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        destination.close()
        if source.in_transaction:
            source.rollback()
        source.close()

    os.replace(temp_path, destination_path)
    return SQLiteSnapshotProof(
        row_counts=snapshot_counts,
        schema_sql=snapshot_schema,
        snapshot_started_at_ms=started_ms,
        snapshot_completed_at_ms=completed_ms,
    )


def sqlite_integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "NO_RESULT"
    finally:
        conn.close()


def sqlite_schema_sql(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return _schema_sql_from_connection(conn)
    finally:
        conn.close()


def table_row_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return _table_row_counts_from_connection(conn)
    finally:
        conn.close()


def fetch_json(url: str, timeout_sec: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "direction-engine-p26-freeze/2.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def export_paper_v1(db_path: Path, json_path: Path, csv_path: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='paper_trades'"
        ).fetchone()
        if not exists:
            rows: list[dict[str, Any]] = []
        else:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM paper_trades
                    WHERE strategy_version=?
                    ORDER BY id ASC
                    """,
                    (PAPER_V1_STRATEGY,),
                ).fetchall()
            ]
    finally:
        conn.close()

    atomic_write_json(json_path, rows)
    fieldnames = sorted({key for row in rows for key in row})
    buffer = tempfile.SpooledTemporaryFile(
        mode="w+", newline="", encoding="utf-8"
    )
    try:
        writer = csv.DictWriter(
            buffer, fieldnames=fieldnames, extrasaction="ignore"
        )
        if fieldnames:
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        buffer.seek(0)
        atomic_write_bytes(csv_path, buffer.read().encode("utf-8"))
    finally:
        buffer.close()
    return len(rows)


@dataclass(frozen=True)
class ArtifactEntry:
    relative_path: str
    size_bytes: int
    sha256: str
    required: bool


def artifact_entry(
    root: Path, path: Path, required: bool = True
) -> ArtifactEntry:
    return ArtifactEntry(
        relative_path=str(path.relative_to(root)),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        required=required,
    )


def copy_if_exists(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temp_path)
    os.replace(temp_path, destination)
    return True


class BaselineFreezer:
    def __init__(self, settings: P26Settings, repo_root: Path) -> None:
        self.settings = settings
        self.repo_root = repo_root.resolve()

    def freeze(
        self,
        *,
        baseline_id: Optional[str] = None,
        allow_dirty: bool = False,
        allow_api_unavailable: bool = False,
    ) -> Path:
        self.settings.ensure_directories()
        git = git_state(self.repo_root)
        dirty = bool(git["dirty_lines"])
        if self.settings.baseline_require_clean_git and dirty and not allow_dirty:
            raise RuntimeError(
                "BASELINE_FREEZE_FAIL: WORKTREE_DIRTY: "
                + ", ".join(git["dirty_lines"])
            )

        baseline_id = baseline_id or f"P25_FREEZE_{utc_stamp()}"
        target = Path(self.settings.backup_root) / baseline_id
        if target.exists():
            raise FileExistsError(target)
        target.mkdir(parents=True, exist_ok=False)

        source_db = Path(self.settings.p25_db_path)
        backup_db = target / "direction_engine.sqlite"
        snapshot = sqlite_online_backup(source_db, backup_db)
        integrity = sqlite_integrity(backup_db)
        if integrity.lower() != "ok":
            raise RuntimeError(f"backup integrity failed: {integrity}")

        backup_counts = table_row_counts(backup_db)
        if snapshot.row_counts != backup_counts:
            raise RuntimeError(
                "snapshot row-count mismatch "
                f"snapshot={snapshot.row_counts} backup={backup_counts}"
            )

        backup_schema = sqlite_schema_sql(backup_db)
        if snapshot.schema_sql != backup_schema:
            raise RuntimeError("snapshot schema mismatch between source and backup")

        source_live_after_counts = table_row_counts(source_db)
        source_changed_after_snapshot = (
            source_live_after_counts != snapshot.row_counts
        )

        paper_json = target / "paper_v1.json"
        paper_csv = target / "paper_v1.csv"
        paper_count = export_paper_v1(backup_db, paper_json, paper_csv)

        artifact_paths: list[tuple[Path, bool]] = [
            (backup_db, True),
            (paper_json, True),
            (paper_csv, True),
        ]
        for source_raw, name in (
            (self.settings.p25_model_path, "p25_model.pkl"),
            (self.settings.p25_calibration_path, "p25_calibration.pkl"),
        ):
            destination = target / name
            copied = copy_if_exists(Path(source_raw), destination)
            artifact_paths.append((destination, copied))

        api_exports: dict[str, dict[str, Any]] = {}
        for name, url in (
            ("state", self.settings.p25_state_url),
            ("paper_summary", self.settings.p25_paper_summary_url),
        ):
            destination = target / f"api_{name}.json"
            try:
                payload = fetch_json(
                    url, self.settings.baseline_http_timeout_sec
                )
                atomic_write_json(destination, payload)
                api_exports[name] = {
                    "status": "OK",
                    "url": url,
                    "relative_path": str(destination.relative_to(target)),
                }
                artifact_paths.append((destination, True))
            except (
                OSError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                if not allow_api_unavailable:
                    raise RuntimeError(
                        f"runtime API unavailable: {url}: {exc}"
                    ) from exc
                error_payload = {
                    "status": "UNAVAILABLE",
                    "url": url,
                    "error": repr(exc),
                }
                atomic_write_json(destination, error_payload)
                api_exports[name] = error_payload
                artifact_paths.append((destination, False))

        schema_path = target / "schema.sql"
        atomic_write_bytes(
            schema_path, (backup_schema + "\n").encode("utf-8")
        )
        artifact_paths.append((schema_path, True))

        artifacts = [
            artifact_entry(target, path, required=required)
            for path, required in artifact_paths
            if path.exists()
        ]
        manifest = {
            "format_version": BASELINE_FORMAT_VERSION,
            "baseline_id": baseline_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "hostname": platform.node(),
            "python_version": sys.version,
            "platform": platform.platform(),
            "git_commit": git["commit"],
            "git_branch": git["branch"],
            "git_dirty": dirty,
            "git_dirty_lines": git["dirty_lines"],
            "db_source_path": str(source_db.resolve()),
            "db_backup_path": str(backup_db.relative_to(target)),
            "db_integrity_check": integrity,
            "db_schema_sha256": sha256_bytes(backup_schema.encode("utf-8")),
            "source_snapshot_schema_sha256": sha256_bytes(
                snapshot.schema_sql.encode("utf-8")
            ),
            "table_row_counts": backup_counts,
            "source_snapshot_row_counts": snapshot.row_counts,
            "source_live_after_row_counts": source_live_after_counts,
            "source_changed_after_snapshot": source_changed_after_snapshot,
            "snapshot_started_at_ms": snapshot.snapshot_started_at_ms,
            "snapshot_completed_at_ms": snapshot.snapshot_completed_at_ms,
            "paper_v1_count": paper_count,
            "api_exports": api_exports,
            "artifacts": [asdict(entry) for entry in artifacts],
            "safety": {
                "restored_automatically": False,
                "p25_database_mutated": False,
                "p25_writes_allowed_during_snapshot": True,
                "fresh_source_growth_is_not_backup_corruption": True,
                "execution_enabled": False,
            },
        }
        manifest_path = target / "p26_freeze_manifest.json"
        atomic_write_json(manifest_path, manifest)
        verify_manifest(manifest_path)
        return manifest_path


def verify_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    failures: list[str] = []
    for entry in manifest.get("artifacts", []):
        path = root / str(entry["relative_path"])
        if not path.exists():
            if entry.get("required", True):
                failures.append(f"missing:{entry['relative_path']}")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size != int(entry["size_bytes"]):
            failures.append(f"size:{entry['relative_path']}")
        if actual_hash != entry["sha256"]:
            failures.append(f"sha256:{entry['relative_path']}")

    db_path = root / str(manifest["db_backup_path"])
    if db_path.exists():
        integrity = sqlite_integrity(db_path)
        if integrity.lower() != "ok":
            failures.append(f"integrity:{integrity}")
        actual_counts = table_row_counts(db_path)
        if actual_counts != manifest.get("table_row_counts", {}):
            failures.append("row_counts")
        actual_schema_hash = sha256_bytes(
            sqlite_schema_sql(db_path).encode("utf-8")
        )
        if actual_schema_hash != manifest.get("db_schema_sha256"):
            failures.append("schema_sha256")

    if failures:
        raise RuntimeError("BASELINE_VERIFY_FAIL: " + ",".join(failures))
    return {
        "ok": True,
        "baseline_id": manifest["baseline_id"],
        "artifacts": len(manifest.get("artifacts", [])),
        "source_changed_after_snapshot": bool(
            manifest.get("source_changed_after_snapshot", False)
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--baseline-id")
    freeze.add_argument("--allow-dirty", action="store_true")
    freeze.add_argument("--allow-api-unavailable", action="store_true")
    freeze.add_argument("--repo-root", default=".")
    verify = sub.add_parser("verify")
    verify.add_argument("manifest")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "verify":
        result = verify_manifest(Path(args.manifest))
        print(json.dumps(result, sort_keys=True))
        return 0

    settings = get_p26_settings()
    freezer = BaselineFreezer(settings, Path(args.repo_root))
    manifest = freezer.freeze(
        baseline_id=args.baseline_id,
        allow_dirty=args.allow_dirty,
        allow_api_unavailable=args.allow_api_unavailable,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

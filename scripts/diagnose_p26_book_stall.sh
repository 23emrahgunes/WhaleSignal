#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${1:-${ROOT}/data/p26_research.sqlite}"
SERVICE="${2:-direction-engine-p26-book.service}"
PY="${ROOT}/.venv/bin/python"

cd "$ROOT"

echo "=== SERVICE ==="
systemctl is-active "$SERVICE" || true
systemctl show "$SERVICE" -p ActiveState -p SubState -p MainPID -p ExecStart --no-pager || true

echo
echo "=== RECENT JOURNAL ==="
journalctl -u "$SERVICE" --since "30 min ago" --no-pager -n 120 || true

echo
echo "=== RECENT LOG ==="
tail -n 160 logs/p26-book.log 2>/dev/null || true

echo
echo "=== DB FILES ==="
ls -lh "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true

echo
echo "=== DB PROCESS HOLDERS ==="
if command -v lsof >/dev/null 2>&1; then
  lsof "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true
elif command -v fuser >/dev/null 2>&1; then
  fuser -v "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true
else
  echo "no lsof/fuser available"
fi

echo
echo "=== DB SNAPSHOT ==="
"$PY" - "$DB" <<'PY'
import json
import sqlite3
import sys
import time

db = sys.argv[1]
now = int(time.time() * 1000)
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)
conn.row_factory = sqlite3.Row
try:
    conn.execute("PRAGMA busy_timeout=3000")
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    out = {"now_ms": now, "tables": sorted(t for t in tables if t.startswith("p26_"))}
    if "p26_clob_books" in tables:
        row = conn.execute(
            """
            SELECT count(*) AS n,
                   min(recv_ts_ms) AS min_recv,
                   max(recv_ts_ms) AS max_recv,
                   count(DISTINCT combo_key) AS combos,
                   count(DISTINCT condition_id) AS conditions
            FROM p26_clob_books
            """
        ).fetchone()
        out["clob_books"] = {
            "rows": int(row["n"] or 0),
            "combos": int(row["combos"] or 0),
            "conditions": int(row["conditions"] or 0),
            "min_recv_ts_ms": row["min_recv"],
            "max_recv_ts_ms": row["max_recv"],
            "last_book_age_sec": (
                round((now - int(row["max_recv"])) / 1000, 3)
                if row["max_recv"] is not None else None
            ),
        }
    if "p26_meta" in tables:
        meta = conn.execute(
            "SELECT value,updated_at_ms FROM p26_meta WHERE key='book_collector_health_json'"
        ).fetchone()
        if meta:
            try:
                health = json.loads(meta["value"])
            except json.JSONDecodeError:
                health = {"raw": meta["value"]}
            out["book_health"] = health
            out["book_health_age_sec"] = round((now - int(meta["updated_at_ms"])) / 1000, 3)
    print(json.dumps(out, indent=2, sort_keys=True))
finally:
    conn.close()
PY

echo
echo "=== WRITE LOCK PROBE ==="
"$PY" - "$DB" <<'PY'
import sqlite3
import sys
import time

db = sys.argv[1]
started = time.perf_counter()
conn = sqlite3.connect(db, timeout=5.0)
try:
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        INSERT INTO p26_meta(key,value,updated_at_ms)
        VALUES('p26_lock_probe','ok',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
        """,
        (int(time.time() * 1000),),
    )
    conn.commit()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    print(f"write_probe=OK elapsed_ms={elapsed_ms}")
except Exception as exc:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    print(f"write_probe=FAIL elapsed_ms={elapsed_ms} error={exc!r}")
    raise
finally:
    conn.close()
PY

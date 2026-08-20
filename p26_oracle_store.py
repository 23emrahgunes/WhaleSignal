"""Persistent Chainlink RTDS tick store for the P2.6 sidecar."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from chainlink_feed import parse_chainlink_payload
from p26_schema import connect_p26, ensure_p26_schema


ORACLE_SOURCE = "POLYMARKET_RTDS_CHAINLINK"
ORACLE_SCHEMA_VERSION = "P26_ORACLE_TICK_V1"


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class OracleTick:
    asset: str
    source: str
    value_text: str
    value_real: float
    source_ts_ms: int
    recv_ts_ms: int
    payload_sha256: str
    schema_version: str = ORACLE_SCHEMA_VERSION
    id: Optional[int] = None

    def as_insert_tuple(self, inserted_at_ms: Optional[int] = None) -> tuple:
        return (
            self.asset,
            self.source,
            self.value_text,
            self.value_real,
            self.source_ts_ms,
            self.recv_ts_ms,
            self.payload_sha256,
            self.schema_version,
            int(time.time() * 1000) if inserted_at_ms is None else inserted_at_ms,
        )


def _payload_candidates(obj: object) -> Iterator[dict]:
    if not isinstance(obj, dict):
        return
    topic = str(obj.get("topic") or "")
    if topic and topic != "crypto_prices_chainlink":
        return
    payload = obj.get("payload", obj)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            shared_symbol = payload.get("symbol")
            for item in data:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                if shared_symbol is not None and "symbol" not in candidate:
                    candidate["symbol"] = shared_symbol
                yield candidate
        else:
            yield payload
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item


def iter_rtds_ticks(obj: object, recv_ts_ms: Optional[int] = None) -> Iterator[OracleTick]:
    recv_ts_ms = int(time.time() * 1000) if recv_ts_ms is None else int(recv_ts_ms)
    events = obj if isinstance(obj, list) else [obj]
    for event in events:
        for candidate in _payload_candidates(event):
            parsed = parse_chainlink_payload(candidate, recv_ts_ms / 1000.0)
            if parsed is None:
                continue
            asset, state = parsed
            raw_value = candidate.get("value")
            if raw_value is None:
                raw_value = candidate.get("price")
            if raw_value is None:
                raw_value = candidate.get("full_accuracy_value")
            value_text = str(raw_value if raw_value is not None else format(state.value, ".18g"))
            yield OracleTick(
                asset=asset,
                source=ORACLE_SOURCE,
                value_text=value_text,
                value_real=float(state.value),
                source_ts_ms=int(round(state.source_ts * 1000.0)),
                recv_ts_ms=recv_ts_ms,
                payload_sha256=hashlib.sha256(_canonical_json(candidate)).hexdigest(),
            )


class OracleTickStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn = connect_p26(db_path)
        ensure_p26_schema(self.conn)

    def insert_many(self, ticks: Iterable[OracleTick]) -> int:
        rows = list(ticks)
        if not rows:
            return 0
        before = self.conn.total_changes
        now_ms = int(time.time() * 1000)
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO p26_oracle_ticks(
                asset,source,value_text,value_real,source_ts_ms,recv_ts_ms,
                payload_sha256,schema_version,inserted_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [tick.as_insert_tuple(now_ms) for tick in rows],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def insert(self, tick: OracleTick) -> bool:
        return self.insert_many([tick]) == 1

    @staticmethod
    def _row_to_tick(row: sqlite3.Row) -> OracleTick:
        return OracleTick(
            id=int(row["id"]),
            asset=str(row["asset"]),
            source=str(row["source"]),
            value_text=str(row["value_text"]),
            value_real=float(row["value_real"]),
            source_ts_ms=int(row["source_ts_ms"]),
            recv_ts_ms=int(row["recv_ts_ms"]),
            payload_sha256=str(row["payload_sha256"]),
            schema_version=str(row["schema_version"]),
        )

    def latest(self, asset: str) -> Optional[OracleTick]:
        row = self.conn.execute(
            """
            SELECT * FROM p26_oracle_ticks
            WHERE asset=? ORDER BY source_ts_ms DESC,id DESC LIMIT 1
            """,
            (asset.upper(),),
        ).fetchone()
        return self._row_to_tick(row) if row else None

    def at_or_before(
        self,
        asset: str,
        target_ts_ms: int,
        *,
        max_age_ms: Optional[int] = None,
    ) -> Optional[OracleTick]:
        params: list[object] = [asset.upper(), int(target_ts_ms)]
        extra = ""
        if max_age_ms is not None:
            extra = " AND source_ts_ms>=?"
            params.append(int(target_ts_ms) - int(max_age_ms))
        row = self.conn.execute(
            f"""
            SELECT * FROM p26_oracle_ticks
            WHERE asset=? AND source_ts_ms<=? {extra}
            ORDER BY source_ts_ms DESC,id DESC LIMIT 1
            """,
            params,
        ).fetchone()
        return self._row_to_tick(row) if row else None

    def nearest(
        self,
        asset: str,
        target_ts_ms: int,
        *,
        max_distance_ms: int,
        allow_future: bool = False,
    ) -> Optional[OracleTick]:
        if not allow_future:
            return self.at_or_before(asset, target_ts_ms, max_age_ms=max_distance_ms)
        row = self.conn.execute(
            """
            SELECT * FROM p26_oracle_ticks
            WHERE asset=? AND source_ts_ms BETWEEN ? AND ?
            ORDER BY ABS(source_ts_ms-?) ASC, source_ts_ms ASC, id ASC LIMIT 1
            """,
            (
                asset.upper(),
                int(target_ts_ms) - int(max_distance_ms),
                int(target_ts_ms) + int(max_distance_ms),
                int(target_ts_ms),
            ),
        ).fetchone()
        return self._row_to_tick(row) if row else None

    def rehydrate(self, *, since_ts_ms: int, assets: Iterable[str] = ("BTC", "ETH", "SOL", "XRP")) -> dict[str, list[OracleTick]]:
        result: dict[str, list[OracleTick]] = {}
        for asset in assets:
            rows = self.conn.execute(
                """
                SELECT * FROM p26_oracle_ticks
                WHERE asset=? AND source_ts_ms>=?
                ORDER BY source_ts_ms ASC,id ASC
                """,
                (asset.upper(), int(since_ts_ms)),
            ).fetchall()
            result[asset.upper()] = [self._row_to_tick(row) for row in rows]
        return result

    def prune(self, *, before_ts_ms: int, batch_size: int = 10_000) -> int:
        before = self.conn.total_changes
        self.conn.execute(
            """
            DELETE FROM p26_oracle_ticks
            WHERE id IN (
                SELECT t.id
                FROM p26_oracle_ticks t
                LEFT JOIN p26_canonical_rows c ON c.chainlink_tick_id=t.id
                WHERE t.source_ts_ms<? AND c.id IS NULL
                ORDER BY t.source_ts_ms ASC
                LIMIT ?
            )
            """,
            (int(before_ts_ms), int(batch_size)),
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(source_ts_ms) AS first_ts, MAX(source_ts_ms) AS last_ts
            FROM p26_oracle_ticks
            """
        ).fetchone()
        per_asset = {
            str(r[0]): int(r[1])
            for r in self.conn.execute(
                "SELECT asset,COUNT(*) FROM p26_oracle_ticks GROUP BY asset"
            ).fetchall()
        }
        return {
            "ticks": int(row["n"] or 0),
            "first_ts_ms": row["first_ts"],
            "last_ts_ms": row["last_ts"],
            "per_asset": per_asset,
        }

    def close(self) -> None:
        self.conn.close()

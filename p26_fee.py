"""Read-only Polymarket CLOB V2 fee schedules and Paper V2 fee math.

No credentials, signatures or order submission exist here.  Public CLOB market
metadata is persisted so every paper fill has an auditable fee lineage.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import aiohttp

from p26_schema import connect_p26, ensure_p26_schema


FEE_FORMULA_VERSION = "POLYMARKET_CLOB_V2_FD_V1"
FEE_QUANTUM = Decimal("0.00001")


class FeeScheduleUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class FeeSchedule:
    condition_id: str
    token_id: str
    enabled: bool
    rate: float
    exponent: float
    taker_only: bool
    source: str
    source_ts_ms: int
    formula_version: str = FEE_FORMULA_VERSION

    def __post_init__(self) -> None:
        if not self.condition_id or not self.token_id:
            raise ValueError("condition_id and token_id are required")
        if self.rate < 0 or self.exponent < 0:
            raise ValueError("fee rate/exponent cannot be negative")

    def fee_usdc(self, *, shares: float, price: float) -> float:
        if shares <= 0 or not self.enabled:
            return 0.0
        if not 0.0 < price < 1.0:
            raise ValueError("fee price must be in (0,1)")
        curve = (float(price) * (1.0 - float(price))) ** float(self.exponent)
        raw = Decimal(str(float(shares) * float(self.rate) * curve))
        return float(raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_fee_schema(conn: sqlite3.Connection) -> None:
    ensure_p26_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS p26_market_tokens (
            condition_id        TEXT NOT NULL,
            combo_key           TEXT NOT NULL,
            side                TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
            token_id            TEXT NOT NULL,
            market_end_ts_ms    INTEGER,
            active              INTEGER NOT NULL DEFAULT 1,
            updated_at_ms       INTEGER NOT NULL,
            PRIMARY KEY(condition_id,side),
            UNIQUE(token_id)
        );
        CREATE INDEX IF NOT EXISTS idx_p26_market_tokens_active
        ON p26_market_tokens(active,market_end_ts_ms);

        CREATE TABLE IF NOT EXISTS p26_fee_schedules (
            condition_id        TEXT NOT NULL,
            token_id            TEXT NOT NULL,
            enabled             INTEGER NOT NULL,
            rate                REAL NOT NULL,
            exponent            REAL NOT NULL,
            taker_only          INTEGER NOT NULL,
            source              TEXT NOT NULL,
            source_ts_ms        INTEGER NOT NULL,
            formula_version     TEXT NOT NULL,
            raw_json            TEXT,
            updated_at_ms       INTEGER NOT NULL,
            PRIMARY KEY(condition_id,token_id)
        );
        CREATE INDEX IF NOT EXISTS idx_p26_fee_token
        ON p26_fee_schedules(token_id,source_ts_ms);
        """
    )
    conn.commit()


class FeeScheduleStore:
    def __init__(self, db_path: str) -> None:
        self.conn = connect_p26(db_path)
        ensure_fee_schema(self.conn)

    def upsert_market_info(
        self,
        *,
        condition_id: str,
        combo_key: str,
        market_end_ts_ms: Optional[int],
        payload: dict[str, Any],
        source_ts_ms: Optional[int] = None,
        source: str = "CLOB_V2_GET_CLOB_MARKET_INFO",
    ) -> dict[str, FeeSchedule]:
        now_ms = int(time.time() * 1000) if source_ts_ms is None else int(source_ts_ms)
        raw_tokens = payload.get("t") or payload.get("tokens") or []
        fee_details = payload.get("fd") or payload.get("fee_details")
        enabled = isinstance(fee_details, dict)
        rate = float((fee_details or {}).get("r", (fee_details or {}).get("rate", 0.0)) or 0.0)
        exponent = float((fee_details or {}).get("e", (fee_details or {}).get("exponent", 1.0)) or 1.0)
        taker_only = bool((fee_details or {}).get("to", (fee_details or {}).get("taker_only", True)))
        schedules: dict[str, FeeSchedule] = {}
        seen_sides: set[str] = set()
        for item in raw_tokens:
            if not isinstance(item, dict):
                continue
            token_id = item.get("t") or item.get("token_id")
            outcome = str(item.get("o") or item.get("outcome") or "").strip().upper()
            if outcome in {"UP", "YES"}:
                side = "UP"
            elif outcome in {"DOWN", "NO"}:
                side = "DOWN"
            else:
                continue
            if not token_id or side in seen_sides:
                continue
            seen_sides.add(side)
            token_id = str(token_id)
            schedule = FeeSchedule(
                condition_id=str(condition_id), token_id=token_id,
                enabled=enabled, rate=rate, exponent=exponent,
                taker_only=taker_only, source=source, source_ts_ms=now_ms,
            )
            schedules[side] = schedule
            self.conn.execute(
                """
                INSERT INTO p26_market_tokens(
                    condition_id,combo_key,side,token_id,market_end_ts_ms,active,updated_at_ms
                ) VALUES(?,?,?,?,?,1,?)
                ON CONFLICT(condition_id,side) DO UPDATE SET
                    combo_key=excluded.combo_key,
                    token_id=excluded.token_id,
                    market_end_ts_ms=excluded.market_end_ts_ms,
                    active=1,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (condition_id, combo_key, side, token_id, market_end_ts_ms, now_ms),
            )
            self.conn.execute(
                """
                INSERT INTO p26_fee_schedules(
                    condition_id,token_id,enabled,rate,exponent,taker_only,
                    source,source_ts_ms,formula_version,raw_json,updated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(condition_id,token_id) DO UPDATE SET
                    enabled=excluded.enabled,rate=excluded.rate,
                    exponent=excluded.exponent,taker_only=excluded.taker_only,
                    source=excluded.source,source_ts_ms=excluded.source_ts_ms,
                    formula_version=excluded.formula_version,
                    raw_json=excluded.raw_json,updated_at_ms=excluded.updated_at_ms
                """,
                (
                    condition_id, token_id, int(enabled), rate, exponent,
                    int(taker_only), source, now_ms, FEE_FORMULA_VERSION,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    int(time.time() * 1000),
                ),
            )
        self.conn.commit()
        return schedules

    def get(self, condition_id: str, token_id: str) -> Optional[FeeSchedule]:
        row = self.conn.execute(
            "SELECT * FROM p26_fee_schedules WHERE condition_id=? AND token_id=?",
            (condition_id, token_id),
        ).fetchone()
        if row is None:
            return None
        return FeeSchedule(
            condition_id=str(row["condition_id"]), token_id=str(row["token_id"]),
            enabled=bool(row["enabled"]), rate=float(row["rate"]),
            exponent=float(row["exponent"]), taker_only=bool(row["taker_only"]),
            source=str(row["source"]), source_ts_ms=int(row["source_ts_ms"]),
            formula_version=str(row["formula_version"]),
        )

    def tokens(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        where = "WHERE active=1" if active_only else ""
        return self.conn.execute(
            f"SELECT * FROM p26_market_tokens {where} ORDER BY condition_id,side"
        ).fetchall()

    def mark_active_conditions(self, condition_ids: set[str]) -> None:
        now_ms = int(time.time() * 1000)
        if condition_ids:
            placeholders = ",".join("?" for _ in condition_ids)
            self.conn.execute(
                f"UPDATE p26_market_tokens SET active=0,updated_at_ms=? "
                f"WHERE condition_id NOT IN ({placeholders})",
                (now_ms, *sorted(condition_ids)),
            )
            self.conn.execute(
                f"UPDATE p26_market_tokens SET active=1,updated_at_ms=? "
                f"WHERE condition_id IN ({placeholders})",
                (now_ms, *sorted(condition_ids)),
            )
        else:
            self.conn.execute(
                "UPDATE p26_market_tokens SET active=0,updated_at_ms=?",
                (now_ms,),
            )
        self.conn.commit()

    def mapping(self, condition_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT side,token_id FROM p26_market_tokens WHERE condition_id=?",
            (condition_id,),
        ).fetchall()
        return {str(row["side"]): str(row["token_id"]) for row in rows}

    def close(self) -> None:
        self.conn.close()


async def fetch_clob_market_info(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    condition_id: str,
    timeout_sec: float = 10.0,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/clob-markets/{condition_id}"
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with session.get(url, timeout=timeout) as response:
        if response.status != 200:
            body = await response.text()
            raise FeeScheduleUnavailable(
                f"CLOB market info HTTP {response.status}: {body[:200]}"
            )
        payload = await response.json()
    if not isinstance(payload, dict):
        raise FeeScheduleUnavailable("CLOB market info is not an object")
    return payload

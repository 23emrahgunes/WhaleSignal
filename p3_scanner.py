"""P3 structural complete-set scanner and opportunity lifetime tracker.

The scanner is model-free.  It consumes already-persisted public P2.6 CLOB books
and fee schedules, computes BUY+MERGE and SPLIT+SELL parity, persists only
fee/depth/latency-valid positive opportunities, and maintains contiguous lifetime
windows.  No order submission exists.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from p26_execution import OrderBookSnapshot
from p26_fee import FeeSchedule
from p3_complete_set import BookPair, best_buy_merge, best_split_sell
from p3_config import P3Settings
from p3_models import StructuralOpportunity
from p3_recorder import P3Recorder
from p3_schema import open_p26_read_only


@dataclass(frozen=True)
class ScanStats:
    conditions: int = 0
    valid_pairs: int = 0
    missing_book: int = 0
    stale_book: int = 0
    source_skew: int = 0
    missing_fee: int = 0
    positive_buy_merge: int = 0
    positive_split_sell: int = 0
    inserted: int = 0
    windows_closed: int = 0


class StructuralArbScanner:
    def __init__(self, settings: P3Settings) -> None:
        self.settings = settings
        self.settings.validate_research_safety()
        self.p26 = open_p26_read_only(settings.p26_db_path)
        self.recorder = P3Recorder(settings.p3_db_path)

    @staticmethod
    def _decode_book(row) -> OrderBookSnapshot:  # noqa: ANN001
        return OrderBookSnapshot.from_levels(
            token_id=str(row["token_id"]),
            ts_ms=int(row["source_ts_ms"]),
            bids=[tuple(value) for value in json.loads(str(row["bids_json"]))],
            asks=[tuple(value) for value in json.loads(str(row["asks_json"]))],
            sequence=(int(row["sequence"]) if row["sequence"] is not None else None),
        )

    def _active_conditions(self) -> list[dict]:
        rows = self.p26.execute(
            """
            SELECT condition_id,combo_key,
                   MAX(CASE WHEN side='UP' THEN token_id END) AS up_token,
                   MAX(CASE WHEN side='DOWN' THEN token_id END) AS down_token,
                   MAX(market_end_ts_ms) AS market_end_ts_ms
            FROM p26_market_tokens
            WHERE active=1
            GROUP BY condition_id,combo_key
            HAVING up_token IS NOT NULL AND down_token IS NOT NULL
            ORDER BY combo_key,condition_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def _latest_book(self, condition_id: str, side: str):  # noqa: ANN001
        return self.p26.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=?
            ORDER BY source_ts_ms DESC,id DESC LIMIT 1
            """,
            (condition_id, side),
        ).fetchone()

    def _fee(self, condition_id: str, token_id: str) -> Optional[FeeSchedule]:
        row = self.p26.execute(
            """
            SELECT * FROM p26_fee_schedules
            WHERE condition_id=? AND token_id=?
            """,
            (condition_id, token_id),
        ).fetchone()
        if row is None:
            return None
        return FeeSchedule(
            condition_id=str(row["condition_id"]),
            token_id=str(row["token_id"]),
            enabled=bool(row["enabled"]),
            rate=float(row["rate"]),
            exponent=float(row["exponent"]),
            taker_only=bool(row["taker_only"]),
            source=str(row["source"]),
            source_ts_ms=int(row["source_ts_ms"]),
            formula_version=str(row["formula_version"]),
        )

    def _pair(self, item: dict, now_ms: int) -> tuple[Optional[BookPair], str]:
        condition_id = str(item["condition_id"])
        up_row = self._latest_book(condition_id, "UP")
        down_row = self._latest_book(condition_id, "DOWN")
        if up_row is None or down_row is None:
            return None, "MISSING_BOOK"
        up_ts = int(up_row["source_ts_ms"])
        down_ts = int(down_row["source_ts_ms"])
        if now_ms - up_ts > self.settings.max_book_age_ms or now_ms - down_ts > self.settings.max_book_age_ms:
            return None, "STALE_BOOK"
        if abs(up_ts - down_ts) > self.settings.max_source_skew_ms:
            return None, "SOURCE_SKEW"
        pair = BookPair(
            condition_id=condition_id,
            combo_key=str(item["combo_key"]),
            up_book_id=int(up_row["id"]),
            down_book_id=int(down_row["id"]),
            up=self._decode_book(up_row),
            down=self._decode_book(down_row),
        )
        return pair, "OK"

    def _accepted(self, opp: Optional[StructuralOpportunity]) -> bool:
        if opp is None:
            return False
        return (
            opp.net_profit_usdc > self.settings.min_net_profit_usdc
            and opp.net_roi > self.settings.min_net_roi
        )

    def scan_once(self, *, now_ms: Optional[int] = None) -> ScanStats:
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        counters = {
            "conditions": 0,
            "valid_pairs": 0,
            "missing_book": 0,
            "stale_book": 0,
            "source_skew": 0,
            "missing_fee": 0,
            "positive_buy_merge": 0,
            "positive_split_sell": 0,
            "inserted": 0,
            "windows_closed": 0,
        }
        active_keys: set[tuple[str, str]] = set()
        for item in self._active_conditions():
            counters["conditions"] += 1
            pair, reason = self._pair(item, now)
            if pair is None:
                counters[reason.lower()] += 1
                continue
            counters["valid_pairs"] += 1
            up_fee = self._fee(pair.condition_id, pair.up.token_id)
            down_fee = self._fee(pair.condition_id, pair.down.token_id)
            if up_fee is None or down_fee is None:
                counters["missing_fee"] += 1
                continue

            opportunities = (
                best_buy_merge(
                    pair,
                    up_fee=up_fee,
                    down_fee=down_fee,
                    detected_ts_ms=now,
                    max_quantity_shares=self.settings.max_quantity_shares,
                    execution_buffer_per_share=self.settings.execution_buffer_per_share,
                ),
                best_split_sell(
                    pair,
                    up_fee=up_fee,
                    down_fee=down_fee,
                    detected_ts_ms=now,
                    max_quantity_shares=self.settings.max_quantity_shares,
                    execution_buffer_per_share=self.settings.execution_buffer_per_share,
                ),
            )
            for index, opp in enumerate(opportunities):
                if not self._accepted(opp):
                    continue
                assert opp is not None
                if index == 0:
                    counters["positive_buy_merge"] += 1
                else:
                    counters["positive_split_sell"] += 1
                opp_id, created = self.recorder.record_opportunity(opp)
                if created:
                    counters["inserted"] += 1
                self.recorder.touch_window(opp_id, opp)
                active_keys.add((opp.strategy, opp.condition_id))

        counters["windows_closed"] = self.recorder.close_stale_windows(
            active_keys,
            now_ms=now,
            grace_ms=self.settings.window_grace_ms,
        )
        return ScanStats(**counters)

    def close(self) -> None:
        self.p26.close()
        self.recorder.close()

"""Ex-post two-leg FOK/legging-risk replay for structural P3 opportunities.

Replay uses only P2.6 book snapshots that arrived after the historical detection
moment.  It never feeds future data back into detection and never submits orders.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from p26_execution import BookLevel, OrderBookSnapshot
from p26_fee import FeeSchedule
from p3_complete_set import simulate_buy_shares, simulate_sell_shares
from p3_config import P3Settings
from p3_models import ARB_BUY_MERGE, ARB_SPLIT_SELL, ReplayOutcome
from p3_schema import connect_p3, ensure_p3_schema, open_p26_read_only


def _book_from_row(row) -> OrderBookSnapshot:  # noqa: ANN001
    return OrderBookSnapshot.from_levels(
        token_id=str(row["token_id"]),
        ts_ms=int(row["source_ts_ms"]),
        bids=[tuple(value) for value in json.loads(str(row["bids_json"]))],
        asks=[tuple(value) for value in json.loads(str(row["asks_json"]))],
        sequence=(int(row["sequence"]) if row["sequence"] is not None else None),
    )


def _limited_book(
    snapshot: OrderBookSnapshot,
    *,
    buy: bool,
    limit_price: float,
) -> OrderBookSnapshot:
    if buy:
        asks = tuple(level for level in snapshot.asks if float(level.price) <= float(limit_price) + 1e-12)
        bids = snapshot.bids
    else:
        bids = tuple(level for level in snapshot.bids if float(level.price) >= float(limit_price) - 1e-12)
        asks = snapshot.asks
    return OrderBookSnapshot(
        token_id=snapshot.token_id,
        ts_ms=snapshot.ts_ms,
        bids=bids,
        asks=asks,
        sequence=snapshot.sequence,
        source=snapshot.source,
    )


class P3ReplayEngine:
    def __init__(self, settings: P3Settings) -> None:
        self.settings = settings
        self.p26 = open_p26_read_only(settings.p26_db_path)
        self.p3 = connect_p3(settings.p3_db_path)
        ensure_p3_schema(self.p3)

    def _future_book(self, condition_id: str, side: str, target_ms: int):  # noqa: ANN001
        return self.p26.execute(
            """
            SELECT * FROM p26_clob_books
            WHERE condition_id=? AND side=? AND source_ts_ms>=? AND source_ts_ms<=?
            ORDER BY source_ts_ms,id LIMIT 1
            """,
            (
                condition_id,
                side,
                int(target_ms),
                int(target_ms + self.settings.replay_snapshot_tolerance_ms),
            ),
        ).fetchone()

    def _fee(self, condition_id: str, token_id: str) -> Optional[FeeSchedule]:
        row = self.p26.execute(
            "SELECT * FROM p26_fee_schedules WHERE condition_id=? AND token_id=?",
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

    def _record(self, result: ReplayOutcome) -> None:
        self.p3.execute(
            """
            INSERT OR IGNORE INTO p3_replays(
                opportunity_id,delay_ms,target_ts_ms,observed_ts_ms,strategy,
                quantity_shares,up_fill,down_fill,both_fill,outcome,
                up_exec_price,down_exec_price,gross_profit_usdc,unwind_side,
                unwind_price,unwind_fee_usdc,unwind_loss_usdc,cycle_net_pnl_usdc,
                details_json,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                result.opportunity_id, result.delay_ms, result.target_ts_ms,
                result.observed_ts_ms, result.strategy, result.quantity_shares,
                int(result.up_fill), int(result.down_fill), int(result.both_fill),
                result.outcome, result.up_exec_price, result.down_exec_price,
                result.gross_profit_usdc, result.unwind_side, result.unwind_price,
                result.unwind_fee_usdc, result.unwind_loss_usdc,
                result.cycle_net_pnl_usdc,
                json.dumps(result.details, sort_keys=True, separators=(",", ":")),
                int(time.time() * 1000),
            ),
        )
        self.p3.commit()

    def replay_one(self, opportunity_id: int, delay_ms: int) -> ReplayOutcome:
        opp = self.p3.execute(
            "SELECT * FROM p3_opportunities WHERE id=?", (int(opportunity_id),)
        ).fetchone()
        if opp is None:
            raise KeyError(f"opportunity not found: {opportunity_id}")
        condition_id = str(opp["condition_id"])
        strategy = str(opp["strategy"])
        q = float(opp["quantity_shares"])
        target = int(opp["detected_ts_ms"]) + int(delay_ms)
        up_row = self._future_book(condition_id, "UP", target)
        down_row = self._future_book(condition_id, "DOWN", target)
        if up_row is None or down_row is None:
            result = ReplayOutcome(
                opportunity_id=int(opportunity_id), delay_ms=int(delay_ms),
                target_ts_ms=target, observed_ts_ms=None, strategy=strategy,
                quantity_shares=q, up_fill=False, down_fill=False, both_fill=False,
                outcome="NO_SYNCHRONOUS_BOOK", up_exec_price=None, down_exec_price=None,
                gross_profit_usdc=None, unwind_side=None, unwind_price=None,
                unwind_fee_usdc=None, unwind_loss_usdc=None, cycle_net_pnl_usdc=None,
                details={"up_book": bool(up_row), "down_book": bool(down_row)},
            )
            self._record(result)
            return result

        up_book = _book_from_row(up_row)
        down_book = _book_from_row(down_row)
        observed = max(up_book.ts_ms, down_book.ts_ms)
        up_fee = self._fee(condition_id, up_book.token_id)
        down_fee = self._fee(condition_id, down_book.token_id)
        if up_fee is None or down_fee is None:
            result = ReplayOutcome(
                opportunity_id=int(opportunity_id), delay_ms=int(delay_ms),
                target_ts_ms=target, observed_ts_ms=observed, strategy=strategy,
                quantity_shares=q, up_fill=False, down_fill=False, both_fill=False,
                outcome="FEE_SCHEDULE_UNAVAILABLE", up_exec_price=None, down_exec_price=None,
                gross_profit_usdc=None, unwind_side=None, unwind_price=None,
                unwind_fee_usdc=None, unwind_loss_usdc=None, cycle_net_pnl_usdc=None,
                details={},
            )
            self._record(result)
            return result

        if strategy == ARB_BUY_MERGE:
            up_limited = _limited_book(up_book, buy=True, limit_price=float(opp["up_limit_price"]))
            down_limited = _limited_book(down_book, buy=True, limit_price=float(opp["down_limit_price"]))
            up_leg = simulate_buy_shares(up_limited, q, up_fee)
            down_leg = simulate_buy_shares(down_limited, q, down_fee)
            up_fill, down_fill = up_leg.complete, down_leg.complete
            if up_fill and down_fill:
                cost = up_leg.notional_usdc + down_leg.notional_usdc + up_leg.fee_usdc + down_leg.fee_usdc
                pnl = q - cost
                outcome = "BOTH_FILLED"
                unwind_side = None
                unwind_price = unwind_fee = unwind_loss = None
            elif up_fill or down_fill:
                filled_side = "UP" if up_fill else "DOWN"
                book = up_book if up_fill else down_book
                fee = up_fee if up_fill else down_fee
                buy_leg = up_leg if up_fill else down_leg
                unwind = simulate_sell_shares(book, q, fee)
                proceeds = unwind.notional_usdc - unwind.fee_usdc if unwind.complete else 0.0
                cost = buy_leg.notional_usdc + buy_leg.fee_usdc
                pnl = proceeds - cost
                outcome = "ONE_LEG_FILLED_UNWIND" if unwind.complete else "ONE_LEG_UNWIND_FAILED"
                unwind_side = filled_side
                unwind_price = unwind.vwap
                unwind_fee = unwind.fee_usdc
                unwind_loss = -pnl
            else:
                pnl = 0.0
                outcome = "NONE_FILLED"
                unwind_side = None
                unwind_price = unwind_fee = unwind_loss = None
        elif strategy == ARB_SPLIT_SELL:
            up_limited = _limited_book(up_book, buy=False, limit_price=float(opp["up_limit_price"]))
            down_limited = _limited_book(down_book, buy=False, limit_price=float(opp["down_limit_price"]))
            up_leg = simulate_sell_shares(up_limited, q, up_fee)
            down_leg = simulate_sell_shares(down_limited, q, down_fee)
            up_fill, down_fill = up_leg.complete, down_leg.complete
            if up_fill and down_fill:
                proceeds = up_leg.notional_usdc + down_leg.notional_usdc - up_leg.fee_usdc - down_leg.fee_usdc
                pnl = proceeds - q
                outcome = "BOTH_FILLED"
                unwind_side = None
                unwind_price = unwind_fee = unwind_loss = None
            elif up_fill or down_fill:
                missing_side = "DOWN" if up_fill else "UP"
                missing_book = down_book if up_fill else up_book
                missing_fee = down_fee if up_fill else up_fee
                first_leg = up_leg if up_fill else down_leg
                unwind = simulate_sell_shares(missing_book, q, missing_fee)
                proceeds = first_leg.notional_usdc - first_leg.fee_usdc
                if unwind.complete:
                    proceeds += unwind.notional_usdc - unwind.fee_usdc
                    pnl = proceeds - q
                    outcome = "ONE_LEG_FILLED_UNWIND"
                else:
                    pnl = None
                    outcome = "ONE_LEG_UNWIND_FAILED"
                unwind_side = missing_side
                unwind_price = unwind.vwap
                unwind_fee = unwind.fee_usdc
                unwind_loss = (-pnl if pnl is not None else None)
            else:
                pnl = 0.0
                outcome = "NONE_FILLED"
                unwind_side = None
                unwind_price = unwind_fee = unwind_loss = None
        else:
            raise ValueError(f"unsupported strategy: {strategy}")

        gross_profit = pnl if outcome == "BOTH_FILLED" else None
        result = ReplayOutcome(
            opportunity_id=int(opportunity_id), delay_ms=int(delay_ms),
            target_ts_ms=target, observed_ts_ms=observed, strategy=strategy,
            quantity_shares=q, up_fill=bool(up_fill), down_fill=bool(down_fill),
            both_fill=bool(up_fill and down_fill), outcome=outcome,
            up_exec_price=up_leg.vwap, down_exec_price=down_leg.vwap,
            gross_profit_usdc=gross_profit, unwind_side=unwind_side,
            unwind_price=unwind_price, unwind_fee_usdc=unwind_fee,
            unwind_loss_usdc=unwind_loss, cycle_net_pnl_usdc=pnl,
            details={
                "up_book_ts_ms": up_book.ts_ms,
                "down_book_ts_ms": down_book.ts_ms,
                "up_limit_price": float(opp["up_limit_price"]),
                "down_limit_price": float(opp["down_limit_price"]),
            },
        )
        self._record(result)
        return result

    def process_ready(self, *, now_ms: Optional[int] = None) -> dict:
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        delays = self.settings.replay_delays()
        max_delay = max(delays) + self.settings.replay_snapshot_tolerance_ms
        rows = self.p3.execute(
            """
            SELECT o.id FROM p3_opportunities o
            WHERE o.detected_ts_ms<=?
              AND EXISTS (
                SELECT 1 FROM (SELECT ? AS delay_ms)
              )
            ORDER BY o.id
            LIMIT ?
            """,
            (now - max_delay, delays[0], int(self.settings.replay_batch_size)),
        ).fetchall()
        created = 0
        for row in rows:
            opp_id = int(row["id"])
            existing = {
                int(r[0])
                for r in self.p3.execute(
                    "SELECT delay_ms FROM p3_replays WHERE opportunity_id=?", (opp_id,)
                ).fetchall()
            }
            for delay in delays:
                if delay in existing:
                    continue
                self.replay_one(opp_id, delay)
                created += 1
        return {"opportunities_scanned": len(rows), "replays_created": created}

    def close(self) -> None:
        self.p26.close()
        self.p3.close()

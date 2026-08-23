"""Confirmation-time DRY replay for P3 independent windows.

Generic p3_replays are keyed to a deduplicated opportunity's original detection time.
That is correct for observation-level research, but not for a later confirmed entry
when the same unchanged book state survives. This engine replays only the first strict
confirmed observation per independent window/policy at that observation's actual scan
time. It is SHADOW/PAPER only and never submits orders.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from p3_complete_set import simulate_buy_shares, simulate_sell_shares
from p3_confirmation import CONFIRMED, select_confirmed_observation
from p3_models import ARB_BUY_MERGE, ARB_SPLIT_SELL, ReplayOutcome
from p3_replay import _book_from_row, _limited_book
from p3_replay_clock import P3ReplayEngine as ClockReplayEngine


ENTRY_REPLAY_VERSION = "P3_STRICT_CONFIRM_ENTRY_V1"


class P3EntryReplayEngine(ClockReplayEngine):
    def _record_entry(
        self,
        *,
        window_id: int,
        confirm_ms: int,
        observation_id: int,
        entry_ts_ms: int,
        result: ReplayOutcome,
    ) -> None:
        details = dict(result.details)
        details["entry_replay_version"] = ENTRY_REPLAY_VERSION
        details["replay_clock_version"] = "P3_REPLAY_FIRST_RECV_ASOF_V3"
        details["time_axis"] = "inserted_at_ms_asof"
        details["entry_ts_ms"] = int(entry_ts_ms)
        details["confirm_ms"] = int(confirm_ms)
        self.p3.execute(
            """
            INSERT INTO p3_entry_replays(
                window_id,confirm_ms,observation_id,opportunity_id,entry_ts_ms,
                delay_ms,target_ts_ms,observed_ts_ms,strategy,quantity_shares,
                up_fill,down_fill,both_fill,outcome,up_exec_price,down_exec_price,
                gross_profit_usdc,unwind_side,unwind_price,unwind_fee_usdc,
                unwind_loss_usdc,cycle_net_pnl_usdc,details_json,created_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(window_id,confirm_ms,delay_ms) DO UPDATE SET
                observation_id=excluded.observation_id,
                opportunity_id=excluded.opportunity_id,
                entry_ts_ms=excluded.entry_ts_ms,
                target_ts_ms=excluded.target_ts_ms,
                observed_ts_ms=excluded.observed_ts_ms,
                strategy=excluded.strategy,
                quantity_shares=excluded.quantity_shares,
                up_fill=excluded.up_fill,
                down_fill=excluded.down_fill,
                both_fill=excluded.both_fill,
                outcome=excluded.outcome,
                up_exec_price=excluded.up_exec_price,
                down_exec_price=excluded.down_exec_price,
                gross_profit_usdc=excluded.gross_profit_usdc,
                unwind_side=excluded.unwind_side,
                unwind_price=excluded.unwind_price,
                unwind_fee_usdc=excluded.unwind_fee_usdc,
                unwind_loss_usdc=excluded.unwind_loss_usdc,
                cycle_net_pnl_usdc=excluded.cycle_net_pnl_usdc,
                details_json=excluded.details_json,
                created_at_ms=excluded.created_at_ms
            """,
            (
                int(window_id), int(confirm_ms), int(observation_id),
                int(result.opportunity_id), int(entry_ts_ms), int(result.delay_ms),
                int(result.target_ts_ms), result.observed_ts_ms, result.strategy,
                float(result.quantity_shares), int(result.up_fill), int(result.down_fill),
                int(result.both_fill), result.outcome, result.up_exec_price,
                result.down_exec_price, result.gross_profit_usdc, result.unwind_side,
                result.unwind_price, result.unwind_fee_usdc, result.unwind_loss_usdc,
                result.cycle_net_pnl_usdc,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                int(time.time() * 1000),
            ),
        )
        self.p3.commit()

    def replay_entry(
        self,
        *,
        window_id: int,
        confirm_ms: int,
        observation_id: int,
        delay_ms: int,
    ) -> ReplayOutcome:
        obs = self.p3.execute(
            "SELECT * FROM p3_window_observations WHERE id=? AND window_id=?",
            (int(observation_id), int(window_id)),
        ).fetchone()
        if obs is None:
            raise KeyError(f"window observation not found: {window_id}/{observation_id}")
        opp = self.p3.execute(
            "SELECT * FROM p3_opportunities WHERE id=?",
            (int(obs["opportunity_id"]),),
        ).fetchone()
        if opp is None:
            raise KeyError(f"opportunity not found: {obs['opportunity_id']}")

        condition_id = str(opp["condition_id"])
        strategy = str(opp["strategy"])
        q = float(opp["quantity_shares"])
        entry_ts = int(obs["observed_ts_ms"])
        target = entry_ts + int(delay_ms)
        up_row = self._future_book(condition_id, "UP", target)
        down_row = self._future_book(condition_id, "DOWN", target)
        if up_row is None or down_row is None:
            result = ReplayOutcome(
                opportunity_id=int(opp["id"]), delay_ms=int(delay_ms),
                target_ts_ms=target, observed_ts_ms=None, strategy=strategy,
                quantity_shares=q, up_fill=False, down_fill=False, both_fill=False,
                outcome="NO_SYNCHRONOUS_BOOK", up_exec_price=None, down_exec_price=None,
                gross_profit_usdc=None, unwind_side=None, unwind_price=None,
                unwind_fee_usdc=None, unwind_loss_usdc=None, cycle_net_pnl_usdc=None,
                details={"up_book": bool(up_row), "down_book": bool(down_row)},
            )
            self._record_entry(
                window_id=window_id, confirm_ms=confirm_ms,
                observation_id=observation_id, entry_ts_ms=entry_ts, result=result,
            )
            return result

        up_book = _book_from_row(up_row)
        down_book = _book_from_row(down_row)
        observed = max(int(up_row["inserted_at_ms"]), int(down_row["inserted_at_ms"]))
        up_fee = self._fee(condition_id, up_book.token_id)
        down_fee = self._fee(condition_id, down_book.token_id)
        if up_fee is None or down_fee is None:
            result = ReplayOutcome(
                opportunity_id=int(opp["id"]), delay_ms=int(delay_ms),
                target_ts_ms=target, observed_ts_ms=observed, strategy=strategy,
                quantity_shares=q, up_fill=False, down_fill=False, both_fill=False,
                outcome="FEE_SCHEDULE_UNAVAILABLE", up_exec_price=None,
                down_exec_price=None, gross_profit_usdc=None, unwind_side=None,
                unwind_price=None, unwind_fee_usdc=None, unwind_loss_usdc=None,
                cycle_net_pnl_usdc=None, details={},
            )
            self._record_entry(
                window_id=window_id, confirm_ms=confirm_ms,
                observation_id=observation_id, entry_ts_ms=entry_ts, result=result,
            )
            return result

        if strategy == ARB_BUY_MERGE:
            up_limited = _limited_book(
                up_book, buy=True, limit_price=float(opp["up_limit_price"])
            )
            down_limited = _limited_book(
                down_book, buy=True, limit_price=float(opp["down_limit_price"])
            )
            up_leg = simulate_buy_shares(up_limited, q, up_fee)
            down_leg = simulate_buy_shares(down_limited, q, down_fee)
            up_fill, down_fill = up_leg.complete, down_leg.complete
            if up_fill and down_fill:
                cost = (
                    up_leg.notional_usdc + down_leg.notional_usdc
                    + up_leg.fee_usdc + down_leg.fee_usdc
                )
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
            up_limited = _limited_book(
                up_book, buy=False, limit_price=float(opp["up_limit_price"])
            )
            down_limited = _limited_book(
                down_book, buy=False, limit_price=float(opp["down_limit_price"])
            )
            up_leg = simulate_sell_shares(up_limited, q, up_fee)
            down_leg = simulate_sell_shares(down_limited, q, down_fee)
            up_fill, down_fill = up_leg.complete, down_leg.complete
            if up_fill and down_fill:
                proceeds = (
                    up_leg.notional_usdc + down_leg.notional_usdc
                    - up_leg.fee_usdc - down_leg.fee_usdc
                )
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

        result = ReplayOutcome(
            opportunity_id=int(opp["id"]), delay_ms=int(delay_ms),
            target_ts_ms=target, observed_ts_ms=observed, strategy=strategy,
            quantity_shares=q, up_fill=bool(up_fill), down_fill=bool(down_fill),
            both_fill=bool(up_fill and down_fill), outcome=outcome,
            up_exec_price=up_leg.vwap, down_exec_price=down_leg.vwap,
            gross_profit_usdc=(pnl if outcome == "BOTH_FILLED" else None),
            unwind_side=unwind_side, unwind_price=unwind_price,
            unwind_fee_usdc=unwind_fee, unwind_loss_usdc=unwind_loss,
            cycle_net_pnl_usdc=pnl,
            details={
                "up_book_inserted_at_ms": int(up_row["inserted_at_ms"]),
                "down_book_inserted_at_ms": int(down_row["inserted_at_ms"]),
                "up_limit_price": float(opp["up_limit_price"]),
                "down_limit_price": float(opp["down_limit_price"]),
            },
        )
        self._record_entry(
            window_id=window_id, confirm_ms=confirm_ms,
            observation_id=observation_id, entry_ts_ms=entry_ts, result=result,
        )
        return result

    def process_ready(self, *, now_ms: Optional[int] = None) -> dict:
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        confirms = set(self.settings.dry_survival_delays())
        confirms.add(0)
        confirms.add(int(self.settings.dry_entry_confirm_ms))
        delay = int(self.settings.dry_latency_ms)
        windows = self.p3.execute(
            "SELECT id FROM p3_windows ORDER BY id"
        ).fetchall()
        created = 0
        eligible = 0
        strict_unproven = 0
        gaps = 0
        for window in windows:
            window_id = int(window["id"])
            for confirm_ms in sorted(confirms):
                selected = select_confirmed_observation(
                    self.p3, window_id=window_id, confirm_ms=int(confirm_ms),
                    max_gap_ms=int(self.settings.dry_confirm_max_gap_ms),
                )
                if selected.status != CONFIRMED:
                    if selected.status == "LEGACY_CONFIRMATION_UNPROVEN":
                        strict_unproven += 1
                    elif selected.status == "CONFIRMATION_GAP":
                        gaps += 1
                    continue
                eligible += 1
                assert selected.entry_ts_ms is not None
                assert selected.observation_id is not None
                if selected.entry_ts_ms + delay > now:
                    continue
                existing = self.p3.execute(
                    """
                    SELECT observation_id,details_json FROM p3_entry_replays
                    WHERE window_id=? AND confirm_ms=? AND delay_ms=?
                    """,
                    (window_id, int(confirm_ms), delay),
                ).fetchone()
                if existing is not None:
                    try:
                        details = json.loads(str(existing["details_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        details = {}
                    if (
                        int(existing["observation_id"]) == int(selected.observation_id)
                        and details.get("entry_replay_version") == ENTRY_REPLAY_VERSION
                    ):
                        continue
                self.replay_entry(
                    window_id=window_id, confirm_ms=int(confirm_ms),
                    observation_id=int(selected.observation_id), delay_ms=delay,
                )
                created += 1
        return {
            "windows_scanned": len(windows),
            "strict_candidates": eligible,
            "entry_replays_created": created,
            "legacy_unproven": strict_unproven,
            "confirmation_gaps": gaps,
        }

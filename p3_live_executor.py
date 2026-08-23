"""Guarded P3 LIVE BUY+MERGE executor.

The executor only considers STRICT-confirmed windows created after the current
process-local arm time. Every cycle is capped, fresh-book revalidated and inserted
into the audit DB before network submission to prevent duplicate execution.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from p3_config import P3Settings
from p3_confirmation import CONFIRMED, select_confirmed_observation
from p3_live_gateway import PolymarketLiveGateway
from p3_live_preflight import run_live_preflight
from p3_live_state import LiveState
from p3_models import ARB_BUY_MERGE
from p3_schema import connect_p3, ensure_p3_schema, open_p26_read_only


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class P3LiveExecutor:
    def __init__(
        self,
        settings: P3Settings,
        state: LiveState,
        *,
        gateway_factory: Callable[[P3Settings], Any] = PolymarketLiveGateway,
        preflight_fn: Callable[..., dict[str, Any]] = run_live_preflight,
    ) -> None:
        self.settings = settings
        self.state = state
        self.gateway_factory = gateway_factory
        self.preflight_fn = preflight_fn

    def _refresh_preflight_if_needed(self) -> bool:
        snap = self.state.snapshot()
        last = snap.last_preflight or {}
        checked = int(last.get("checked_at_ms") or 0)
        age_ms = int(time.time() * 1000) - checked
        if last.get("ok") and age_ms <= 60_000:
            return True
        result = self.preflight_fn(self.settings, for_arming=True)
        self.state.remember_preflight(result)
        if not result.get("ok"):
            self.state.halt("LIVE_PREFLIGHT_REFRESH_FAILED")
            return False
        return True

    @staticmethod
    def _cycle_exists(conn, *, session_id: str, window_id: int) -> bool:  # noqa: ANN001
        row = conn.execute(
            "SELECT 1 FROM p3_live_cycles WHERE session_id=? AND window_id=?",
            (session_id, int(window_id)),
        ).fetchone()
        return row is not None

    def _next_candidate(self, conn, *, session_id: str, armed_at_ms: int) -> dict[str, Any] | None:  # noqa: ANN001
        rows = conn.execute(
            """
            SELECT id,strategy,condition_id,combo_key,opened_ts_ms
            FROM p3_windows
            WHERE opened_ts_ms>=?
            ORDER BY opened_ts_ms,id
            LIMIT 200
            """,
            (int(armed_at_ms),),
        ).fetchall()
        for window in rows:
            window_id = int(window["id"])
            if self._cycle_exists(conn, session_id=session_id, window_id=window_id):
                continue
            selection = select_confirmed_observation(
                conn,
                window_id=window_id,
                confirm_ms=int(self.settings.dry_entry_confirm_ms),
                max_gap_ms=int(self.settings.dry_confirm_max_gap_ms),
            )
            if selection.status != CONFIRMED or selection.entry_ts_ms is None:
                continue
            if int(selection.entry_ts_ms) < int(armed_at_ms):
                continue
            opp = conn.execute(
                "SELECT * FROM p3_opportunities WHERE id=?",
                (int(selection.opportunity_id),),
            ).fetchone()
            if opp is None:
                continue
            return {
                "window": dict(window),
                "selection": selection,
                "opportunity": dict(opp),
            }
        return None

    @staticmethod
    def _tokens(p26, condition_id: str) -> tuple[str, str]:  # noqa: ANN001
        rows = p26.execute(
            """
            SELECT side,token_id FROM p26_market_tokens
            WHERE condition_id=? AND active=1 AND side IN ('UP','DOWN')
            """,
            (str(condition_id),),
        ).fetchall()
        values = {str(row["side"]): str(row["token_id"]) for row in rows}
        if not values.get("UP") or not values.get("DOWN"):
            raise RuntimeError("active UP/DOWN token mapping missing")
        return values["UP"], values["DOWN"]

    def _scaled_order(self, opp: dict[str, Any]) -> dict[str, float]:
        original_q = float(opp["quantity_shares"])
        original_capital = float(opp["capital_usdc"])
        if original_q <= 0 or original_capital <= 0:
            raise RuntimeError("invalid opportunity quantity/capital")
        cap_ratio = min(1.0, float(self.settings.live_max_capital_per_cycle_usdc) / original_capital)
        q = min(
            original_q * cap_ratio,
            float(self.settings.live_max_quantity_shares),
            original_q,
        )
        scale = q / original_q
        return {
            "quantity_shares": q,
            "scale": scale,
            "capital_usdc": original_capital * scale,
            "net_profit_usdc": float(opp["net_profit_usdc"]) * scale,
            "net_roi": float(opp["net_roi"]),
        }

    def _insert_cycle(
        self,
        conn,
        *,
        session_id: str,
        candidate: dict[str, Any],
        up_token: str,
        down_token: str,
        scaled: dict[str, float],
        status: str,
        details: dict[str, Any],
    ) -> int:  # noqa: ANN001
        opp = candidate["opportunity"]
        sel = candidate["selection"]
        now = int(time.time() * 1000)
        cur = conn.execute(
            """
            INSERT INTO p3_live_cycles(
                session_id,window_id,observation_id,opportunity_id,strategy,
                condition_id,combo_key,entry_ts_ms,quantity_shares,capital_usdc,
                up_token_id,down_token_id,up_limit_price,down_limit_price,status,
                details_json,created_at_ms,updated_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                int(candidate["window"]["id"]),
                int(sel.observation_id),
                int(sel.opportunity_id),
                str(opp["strategy"]),
                str(opp["condition_id"]),
                str(opp["combo_key"]),
                int(sel.entry_ts_ms),
                float(scaled["quantity_shares"]),
                float(scaled["capital_usdc"]),
                str(up_token),
                str(down_token),
                float(opp["up_limit_price"]),
                float(opp["down_limit_price"]),
                str(status),
                _json(details),
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _update_cycle(conn, cycle_id: int, *, status: str, **fields: Any) -> None:  # noqa: ANN001
        allowed = {
            "up_order_id", "down_order_id", "up_fill_verified", "down_fill_verified",
            "merge_tx_hash", "unwind_side", "unwind_order_id", "error_code", "details_json",
        }
        pairs = ["status=?", "updated_at_ms=?"]
        values: list[Any] = [str(status), int(time.time() * 1000)]
        for key, value in fields.items():
            if key not in allowed:
                continue
            pairs.append(f"{key}=?")
            values.append(value)
        values.append(int(cycle_id))
        conn.execute(f"UPDATE p3_live_cycles SET {','.join(pairs)} WHERE id=?", values)
        conn.commit()

    def process_once(self) -> dict[str, Any]:
        if not self.state.can_auto_execute():
            return {"status": "IDLE_NOT_AUTO_ARMED"}
        if not self._refresh_preflight_if_needed():
            return {"status": "HALTED_PREFLIGHT"}

        snap = self.state.snapshot()
        if not self.state.can_auto_execute() or snap.armed_at_ms is None:
            return {"status": "IDLE_NOT_ARMED"}

        conn = connect_p3(self.settings.p3_db_path)
        ensure_p3_schema(conn)
        p26 = open_p26_read_only(self.settings.p26_db_path)
        try:
            candidate = self._next_candidate(
                conn,
                session_id=snap.session_id,
                armed_at_ms=int(snap.armed_at_ms),
            )
            if candidate is None:
                return {"status": "NO_CONFIRMED_WINDOW"}

            opp = candidate["opportunity"]
            sel = candidate["selection"]
            window_id = int(candidate["window"]["id"])
            up_token, down_token = self._tokens(p26, str(opp["condition_id"]))
            scaled = self._scaled_order(opp)
            q = float(scaled["quantity_shares"])

            if str(opp["strategy"]) != ARB_BUY_MERGE:
                cycle_id = self._insert_cycle(
                    conn, session_id=snap.session_id, candidate=candidate,
                    up_token=up_token, down_token=down_token, scaled=scaled,
                    status="SKIPPED_UNSUPPORTED_STRATEGY", details={},
                )
                return {"status": "SKIPPED_UNSUPPORTED_STRATEGY", "cycle_id": cycle_id}

            if (
                scaled["net_profit_usdc"] < float(self.settings.live_min_net_profit_usdc)
                or scaled["net_roi"] < float(self.settings.live_min_net_roi)
            ):
                cycle_id = self._insert_cycle(
                    conn, session_id=snap.session_id, candidate=candidate,
                    up_token=up_token, down_token=down_token, scaled=scaled,
                    status="SKIPPED_LIVE_EDGE_GATE",
                    details={"scaled": scaled},
                )
                return {"status": "SKIPPED_LIVE_EDGE_GATE", "cycle_id": cycle_id}

            max_submit_age_ms = max(
                500,
                int(self.settings.scan_interval_ms) + 3 * int(self.settings.live_poll_interval_ms),
            )
            entry_age_ms = int(time.time() * 1000) - int(sel.entry_ts_ms)
            if entry_age_ms > max_submit_age_ms:
                cycle_id = self._insert_cycle(
                    conn, session_id=snap.session_id, candidate=candidate,
                    up_token=up_token, down_token=down_token, scaled=scaled,
                    status="SKIPPED_STALE_CONFIRMATION",
                    details={"entry_age_ms": entry_age_ms, "max_submit_age_ms": max_submit_age_ms},
                )
                return {"status": "SKIPPED_STALE_CONFIRMATION", "cycle_id": cycle_id}

            gateway = self.gateway_factory(self.settings)
            up_capacity = gateway.buy_limit_capacity(
                token_id=up_token, limit_price=float(opp["up_limit_price"])
            )
            down_capacity = gateway.buy_limit_capacity(
                token_id=down_token, limit_price=float(opp["down_limit_price"])
            )
            min_size = max(float(up_capacity.get("min_order_size") or 0), float(down_capacity.get("min_order_size") or 0))
            if q + 1e-9 < min_size or float(up_capacity["capacity_shares"]) + 1e-9 < q or float(down_capacity["capacity_shares"]) + 1e-9 < q:
                cycle_id = self._insert_cycle(
                    conn, session_id=snap.session_id, candidate=candidate,
                    up_token=up_token, down_token=down_token, scaled=scaled,
                    status="SKIPPED_FRESH_DEPTH", details={"up": up_capacity, "down": down_capacity, "q": q},
                )
                return {"status": "SKIPPED_FRESH_DEPTH", "cycle_id": cycle_id}

            collateral = float(gateway.collateral_balance_usdc())
            if collateral + 1e-9 < float(scaled["capital_usdc"]):
                cycle_id = self._insert_cycle(
                    conn, session_id=snap.session_id, candidate=candidate,
                    up_token=up_token, down_token=down_token, scaled=scaled,
                    status="SKIPPED_INSUFFICIENT_BALANCE", details={"collateral_usdc": collateral},
                )
                return {"status": "SKIPPED_INSUFFICIENT_BALANCE", "cycle_id": cycle_id}

            # Claim the window before any network order submission. A concurrent loop
            # cannot execute the same window because of UNIQUE(session_id,window_id).
            before_up = float(gateway.conditional_balance_shares(up_token, refresh=True))
            before_down = float(gateway.conditional_balance_shares(down_token, refresh=True))
            cycle_id = self._insert_cycle(
                conn, session_id=snap.session_id, candidate=candidate,
                up_token=up_token, down_token=down_token, scaled=scaled,
                status="PRE_SUBMIT_CLAIMED",
                details={"scaled": scaled, "before_up": before_up, "before_down": before_down},
            )

            if not self.state.can_auto_execute():
                self._update_cycle(conn, cycle_id, status="ABORTED_DISARMED_BEFORE_SUBMIT")
                return {"status": "ABORTED_DISARMED_BEFORE_SUBMIT", "cycle_id": cycle_id}

            posted = gateway.post_two_leg_fok(
                up_token_id=up_token,
                down_token_id=down_token,
                quantity_shares=q,
                up_limit_price=float(opp["up_limit_price"]),
                down_limit_price=float(opp["down_limit_price"]),
            )
            self._update_cycle(
                conn,
                cycle_id,
                status="ORDERS_POSTED",
                up_order_id=posted.get("up_order_id"),
                down_order_id=posted.get("down_order_id"),
                details_json=_json({"posted": posted, "scaled": scaled}),
            )

            settled = gateway.wait_for_leg_deltas(
                up_token_id=up_token,
                down_token_id=down_token,
                before_up=before_up,
                before_down=before_down,
                quantity_shares=q,
            )
            up_delta = float(settled.get("up_delta") or 0.0)
            down_delta = float(settled.get("down_delta") or 0.0)
            up_ok = bool(settled.get("up_verified"))
            down_ok = bool(settled.get("down_verified"))
            self._update_cycle(
                conn,
                cycle_id,
                status="SETTLEMENT_CHECKED",
                up_fill_verified=int(up_ok),
                down_fill_verified=int(down_ok),
                details_json=_json({"posted": posted, "settled": settled, "scaled": scaled}),
            )

            if up_ok and down_ok:
                merge = gateway.merge_positions(
                    condition_id=str(opp["condition_id"]), quantity_shares=q
                )
                merge_wait = gateway.wait_for_merge(
                    up_token_id=up_token,
                    down_token_id=down_token,
                    before_up=before_up,
                    before_down=before_down,
                )
                if not merge.get("verified") or not merge_wait.get("verified"):
                    self._update_cycle(
                        conn,
                        cycle_id,
                        status="HALTED_MERGE_NOT_VERIFIED",
                        merge_tx_hash=merge.get("transaction_hash"),
                        error_code="MERGE_NOT_VERIFIED",
                        details_json=_json({"merge": merge, "merge_wait": merge_wait, "settled": settled}),
                    )
                    self.state.halt("MERGE_NOT_VERIFIED")
                    return {"status": "HALTED_MERGE_NOT_VERIFIED", "cycle_id": cycle_id}
                self._update_cycle(
                    conn,
                    cycle_id,
                    status="MERGED_VERIFIED",
                    merge_tx_hash=merge.get("transaction_hash"),
                    details_json=_json({"merge": merge, "merge_wait": merge_wait, "settled": settled}),
                )
                return {"status": "MERGED_VERIFIED", "cycle_id": cycle_id, "window_id": window_id}

            # Any observed exposure that is not a complete set is immediately unwound.
            exposure: list[tuple[str, str, float, float]] = []
            if up_delta > 1e-6:
                exposure.append(("UP", up_token, up_delta, before_up))
            if down_delta > 1e-6:
                exposure.append(("DOWN", down_token, down_delta, before_down))
            if not exposure:
                self._update_cycle(conn, cycle_id, status="NO_FILL_VERIFIED")
                return {"status": "NO_FILL_VERIFIED", "cycle_id": cycle_id}

            unwind_ids: list[str] = []
            for side, token, shares, before_balance in exposure:
                unwind = gateway.unwind_fok(token_id=token, shares=shares)
                if unwind.get("order_id"):
                    unwind_ids.append(str(unwind["order_id"]))
                verified = gateway.wait_for_unwind(
                    token_id=token,
                    before_entry_balance=before_balance,
                )
                if not verified.get("verified"):
                    self._update_cycle(
                        conn,
                        cycle_id,
                        status="HALTED_UNWIND_NOT_VERIFIED",
                        unwind_side=side,
                        unwind_order_id=unwind.get("order_id"),
                        error_code="UNWIND_NOT_VERIFIED",
                        details_json=_json({"settled": settled, "unwind": unwind, "verify": verified}),
                    )
                    self.state.halt("UNWIND_NOT_VERIFIED")
                    return {"status": "HALTED_UNWIND_NOT_VERIFIED", "cycle_id": cycle_id}

            self._update_cycle(
                conn,
                cycle_id,
                status="ONE_LEG_UNWOUND_VERIFIED",
                unwind_side="+".join(item[0] for item in exposure),
                unwind_order_id=",".join(unwind_ids) or None,
                details_json=_json({"settled": settled, "exposure_count": len(exposure)}),
            )
            return {"status": "ONE_LEG_UNWOUND_VERIFIED", "cycle_id": cycle_id}
        except Exception as exc:  # noqa: BLE001
            # Any unexpected exception while LIVE is armed is fail-closed. If no
            # order was posted this is conservative; if exposure is uncertain it is
            # essential that the operator inspect before re-arming.
            self.state.halt(f"LIVE_EXECUTOR_EXCEPTION:{type(exc).__name__}")
            return {"status": "HALTED_EXCEPTION", "error": type(exc).__name__, "message": str(exc)[:240]}
        finally:
            p26.close()
            conn.close()

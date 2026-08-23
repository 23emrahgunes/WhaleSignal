"""Production token-resolution wrapper for the guarded P3 LIVE executor."""
from __future__ import annotations

from p3_live_executor import P3LiveExecutor as _BaseExecutor


class P3LiveExecutor(_BaseExecutor):
    """Resolve the latest UP/DOWN token IDs from P2.6 full-depth book rows."""

    @staticmethod
    def _tokens(p26, condition_id: str) -> tuple[str, str]:  # noqa: ANN001
        rows = p26.execute(
            """
            SELECT side,token_id,id
            FROM p26_clob_books
            WHERE condition_id=? AND side IN ('UP','DOWN')
            ORDER BY id DESC
            """,
            (str(condition_id),),
        ).fetchall()
        values: dict[str, str] = {}
        for row in rows:
            side = str(row["side"])
            if side not in values:
                values[side] = str(row["token_id"])
            if "UP" in values and "DOWN" in values:
                break
        if not values.get("UP") or not values.get("DOWN"):
            raise RuntimeError("latest P2.6 UP/DOWN token mapping missing")
        return values["UP"], values["DOWN"]

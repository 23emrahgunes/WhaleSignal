"""Read-only paper-trade record queries for the P2.5 dashboard.

This module exposes SQLite simulation records only.  It never creates orders,
loads credentials, signs payloads or calls a trading endpoint.  Queries are
parameterized, newest-first and bounded so the public dashboard cannot issue an
unlimited database scan.

``status=TRADED`` is a virtual filter meaning real paper entries only:
``OPEN`` or ``SETTLED``.  It deliberately excludes diagnostic ``SKIPPED`` rows.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping, Optional


_ASSETS = {"BTC", "ETH", "SOL", "XRP"}
_HORIZONS = {"5m", "15m", "1h"}
_STATUSES = {"OPEN", "SETTLED", "SKIPPED", "TRADED"}
_SIDES = {"UP", "DOWN"}
_RESULTS = {"UP", "DOWN"}
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_MAX_EXPORT = 5000


def _optional_choice(
    raw: Optional[str],
    allowed: set[str],
    *,
    upper: bool = True,
    field: str,
) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or value.upper() == "ALL":
        return None
    normalized = value.upper() if upper else value.lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field} gecersiz; izin verilenler: {choices}")
    return normalized


def _bounded_int(
    raw: Optional[str],
    *,
    default: int,
    minimum: int,
    maximum: int,
    field: str,
) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ValueError(f"{field} tam sayi olmali") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{field} {minimum}..{maximum} araliginda olmali")
    return value


@dataclass(frozen=True)
class PaperRecordFilters:
    asset: Optional[str] = None
    horizon: Optional[str] = None
    status: Optional[str] = None
    side: Optional[str] = None
    official_result: Optional[str] = None
    combo: Optional[str] = None
    query: Optional[str] = None
    limit: int = _DEFAULT_LIMIT
    offset: int = 0

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        export: bool = False,
    ) -> "PaperRecordFilters":
        max_limit = _MAX_EXPORT if export else _MAX_LIMIT
        default_limit = _MAX_EXPORT if export else _DEFAULT_LIMIT
        asset = _optional_choice(
            values.get("asset"), _ASSETS, field="asset"
        )
        horizon = _optional_choice(
            values.get("horizon"),
            _HORIZONS,
            upper=False,
            field="horizon",
        )
        status = _optional_choice(
            values.get("status"), _STATUSES, field="status"
        )
        side = _optional_choice(values.get("side"), _SIDES, field="side")
        official_result = _optional_choice(
            values.get("result") or values.get("official_result"),
            _RESULTS,
            field="result",
        )

        combo_raw = str(values.get("combo") or "").strip()
        combo: Optional[str] = None
        if combo_raw and combo_raw.upper() != "ALL":
            parts = combo_raw.upper().split(":", 1)
            if len(parts) != 2 or parts[0] not in _ASSETS or parts[1].lower() not in _HORIZONS:
                raise ValueError("combo BTC:5m benzeri olmali")
            combo = f"{parts[0]}:{parts[1].lower()}"

        query_raw = str(values.get("q") or values.get("query") or "").strip()
        if len(query_raw) > 120:
            raise ValueError("q en fazla 120 karakter olabilir")
        query = query_raw or None

        limit = _bounded_int(
            values.get("limit"),
            default=default_limit,
            minimum=1,
            maximum=max_limit,
            field="limit",
        )
        offset = _bounded_int(
            values.get("offset"),
            default=0,
            minimum=0,
            maximum=10_000_000,
            field="offset",
        )
        if export:
            offset = 0
        return cls(
            asset=asset,
            horizon=horizon,
            status=status,
            side=side,
            official_result=official_result,
            combo=combo,
            query=query,
            limit=limit,
            offset=offset,
        )

    def for_export(self) -> "PaperRecordFilters":
        return replace(self, limit=_MAX_EXPORT, offset=0)

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "horizon": self.horizon,
            "status": self.status,
            "side": self.side,
            "official_result": self.official_result,
            "combo": self.combo,
            "query": self.query,
            "limit": self.limit,
            "offset": self.offset,
        }


def _strategy_version(recorder) -> str:  # noqa: ANN001
    policy = getattr(recorder, "paper_policy", None)
    return str(getattr(policy, "strategy_version", "RESEARCH_PAPER_V1"))


def _where_clause(
    recorder,
    filters: PaperRecordFilters,
) -> tuple[str, list[object]]:  # noqa: ANN001
    clauses = ["strategy_version=?"]
    params: list[object] = [_strategy_version(recorder)]

    for column, value in (
        ("asset", filters.asset),
        ("horizon", filters.horizon),
        ("side", filters.side),
        ("official_result", filters.official_result),
        ("combo_key", filters.combo),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)

    if filters.status == "TRADED":
        clauses.append("status IN ('OPEN','SETTLED')")
    elif filters.status is not None:
        clauses.append("status=?")
        params.append(filters.status)

    if filters.query:
        needle = f"%{filters.query}%"
        clauses.append(
            "(slug LIKE ? OR condition_id LIKE ? OR market_id LIKE ? "
            "OR combo_key LIKE ? OR skip_reason LIKE ?)"
        )
        params.extend([needle, needle, needle, needle, needle])
    return " AND ".join(clauses), params


def _iso_utc(value: object) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _record_from_row(recorder, row) -> dict:  # noqa: ANN001
    converter = getattr(recorder, "_paper_row", None)
    record = converter(row) if callable(converter) else dict(row)
    status = str(record.get("status") or "UNKNOWN")
    correct = record.get("correct")
    if status == "SETTLED":
        outcome_label = "TUTTU" if int(correct or 0) == 1 else "KACTI"
    elif status == "OPEN":
        outcome_label = "ACIK"
    elif status == "SKIPPED":
        outcome_label = "ATLANDI"
    else:
        outcome_label = status
    record.update(
        {
            "outcome_label": outcome_label,
            "attempted_at_iso": _iso_utc(record.get("attempted_at")),
            "settled_at_iso": _iso_utc(record.get("settled_at")),
            "paper_only": True,
            "execution": False,
        }
    )
    return record


def query_paper_records(recorder, filters: PaperRecordFilters) -> dict:  # noqa: ANN001
    """Return a paginated, newest-first paper-trade record page."""
    where, params = _where_clause(recorder, filters)
    total = int(
        recorder.conn.execute(
            f"SELECT COUNT(*) FROM paper_trades WHERE {where}",
            params,
        ).fetchone()[0]
    )
    rows = recorder.conn.execute(
        f"""
        SELECT * FROM paper_trades
        WHERE {where}
        ORDER BY attempted_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, filters.limit, filters.offset],
    ).fetchall()
    records = [_record_from_row(recorder, row) for row in rows]
    next_offset = filters.offset + filters.limit
    return {
        "paperOnly": True,
        "source": "sqlite",
        "strategy_version": _strategy_version(recorder),
        "filters": filters.to_dict(),
        "pagination": {
            "total": total,
            "limit": filters.limit,
            "offset": filters.offset,
            "returned": len(records),
            "has_previous": filters.offset > 0,
            "has_next": next_offset < total,
            "previous_offset": max(0, filters.offset - filters.limit),
            "next_offset": next_offset if next_offset < total else None,
        },
        "records": records,
    }


_CSV_FIELDS = [
    "id",
    "attempted_at_iso",
    "settled_at_iso",
    "combo_key",
    "asset",
    "horizon",
    "slug",
    "condition_id",
    "status",
    "skip_reason",
    "side",
    "forecast_p_up",
    "selected_probability",
    "forecast_confidence",
    "forecast_grade",
    "forecast_status",
    "forecast_agreement",
    "entry_bid",
    "entry_ask",
    "fill_price",
    "forecast_edge",
    "stake_usdc",
    "shares",
    "official_result",
    "correct",
    "gross_payout",
    "realized_pnl",
    "roi",
    "outcome_label",
    "strategy_version",
]


def export_paper_records_csv(recorder, filters: PaperRecordFilters) -> str:  # noqa: ANN001
    page = query_paper_records(recorder, filters.for_export())
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for record in page["records"]:
        writer.writerow(record)
    return buffer.getvalue()

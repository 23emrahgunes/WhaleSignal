"""Purged, embargoed chronological nested walk-forward splits for P2.6."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TemporalRecord:
    condition_id: str
    market_start_ts_ms: int
    market_end_ts_ms: int
    decision_ts_ms: int


@dataclass(frozen=True)
class TemporalCluster:
    start_ts_ms: int
    end_ts_ms: int
    records: tuple[TemporalRecord, ...]


@dataclass(frozen=True)
class NestedFold:
    fold_id: str
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    train_end_ts_ms: int
    validation_start_ts_ms: int
    validation_end_ts_ms: int
    test_start_ts_ms: int
    test_end_ts_ms: int
    embargo_ms: int


def temporal_clusters(records: Iterable[TemporalRecord]) -> list[TemporalCluster]:
    ordered = sorted(
        records,
        key=lambda row: (row.market_start_ts_ms, row.market_end_ts_ms, row.condition_id),
    )
    clusters: list[TemporalCluster] = []
    current: list[TemporalRecord] = []
    current_start = 0
    current_end = -1
    for record in ordered:
        if not current or record.market_start_ts_ms <= current_end:
            if not current:
                current_start = record.market_start_ts_ms
            current.append(record)
            current_end = max(current_end, record.market_end_ts_ms)
            continue
        clusters.append(
            TemporalCluster(current_start, current_end, tuple(current))
        )
        current = [record]
        current_start = record.market_start_ts_ms
        current_end = record.market_end_ts_ms
    if current:
        clusters.append(TemporalCluster(current_start, current_end, tuple(current)))
    return clusters


def _ids(clusters: Sequence[TemporalCluster]) -> tuple[str, ...]:
    return tuple(record.condition_id for cluster in clusters for record in cluster.records)


def _record_count(clusters: Sequence[TemporalCluster]) -> int:
    return sum(len(cluster.records) for cluster in clusters)


def purged_nested_folds(
    records: Iterable[TemporalRecord],
    *,
    embargo_ms: int,
    min_train_records: int,
    min_validation_records: int,
    min_test_records: int,
    validation_cluster_count: int = 1,
    test_cluster_count: int = 1,
) -> list[NestedFold]:
    """Build expanding-window train/validation/test folds.

    All training market end times must precede validation start by the embargo.
    All validation market end times must precede test start by the embargo.
    Overlapping horizons are kept in the same temporal cluster.
    """
    clusters = temporal_clusters(records)
    if len(clusters) < 3:
        return []
    folds: list[NestedFold] = []
    for test_index in range(2, len(clusters), max(1, test_cluster_count)):
        test = clusters[test_index : test_index + max(1, test_cluster_count)]
        if not test:
            continue
        test_start = test[0].start_ts_ms
        validation_candidates = [
            cluster
            for cluster in clusters[:test_index]
            if cluster.end_ts_ms + embargo_ms < test_start
        ]
        if len(validation_candidates) < validation_cluster_count:
            continue
        validation = validation_candidates[-validation_cluster_count:]
        validation_start = validation[0].start_ts_ms
        train = [
            cluster
            for cluster in clusters[:test_index]
            if cluster.end_ts_ms + embargo_ms < validation_start
        ]
        if (
            _record_count(train) < min_train_records
            or _record_count(validation) < min_validation_records
            or _record_count(test) < min_test_records
        ):
            continue
        fold = NestedFold(
            fold_id=f"fold-{len(folds)+1:03d}",
            train_ids=_ids(train),
            validation_ids=_ids(validation),
            test_ids=_ids(test),
            train_end_ts_ms=max(cluster.end_ts_ms for cluster in train),
            validation_start_ts_ms=validation_start,
            validation_end_ts_ms=max(cluster.end_ts_ms for cluster in validation),
            test_start_ts_ms=test_start,
            test_end_ts_ms=max(cluster.end_ts_ms for cluster in test),
            embargo_ms=embargo_ms,
        )
        validate_fold(fold)
        folds.append(fold)
    return folds


def validate_fold(fold: NestedFold) -> None:
    train = set(fold.train_ids)
    validation = set(fold.validation_ids)
    test = set(fold.test_ids)
    if train & validation or train & test or validation & test:
        raise ValueError("condition_id leakage across fold partitions")
    if not (
        fold.train_end_ts_ms + fold.embargo_ms < fold.validation_start_ts_ms
    ):
        raise ValueError("train/validation purge or embargo violated")
    if not (
        fold.validation_end_ts_ms + fold.embargo_ms < fold.test_start_ts_ms
    ):
        raise ValueError("validation/test purge or embargo violated")

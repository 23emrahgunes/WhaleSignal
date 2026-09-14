"""Measure VPS network latency to trading endpoints.

The script uses only Python's standard library and sends read-only HTTP GET
requests.  It reports DNS, TCP connect, TLS handshake, time-to-first-byte and
total response timings so latency problems are easier to localize than with ping.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


DEFAULT_URLS = (
    "https://clob.polymarket.com/health",
    "https://clob.polymarket.com/markets?limit=1",
    "http://127.0.0.1:8093/health",
)


@dataclass(frozen=True)
class Sample:
    ok: bool
    url: str
    status_line: str | None
    bytes_read: int
    dns_ms: float
    connect_ms: float
    tls_ms: float | None
    ttfb_ms: float
    total_ms: float
    error: str | None = None


def _ms(start: float, end: float | None = None) -> float:
    return (time.perf_counter() - start if end is None else end - start) * 1000.0


def measure_once(url: str, *, timeout_sec: float) -> Sample:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme: {scheme}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"missing host: {url}")
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    total_start = time.perf_counter()
    tls_ms: float | None = None
    sock: socket.socket | None = None
    try:
        dns_start = time.perf_counter()
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_ms = _ms(dns_start)
        last_error: Exception | None = None
        connect_ms = 0.0
        for family, socktype, proto, _, sockaddr in infos:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(timeout_sec)
            try:
                connect_start = time.perf_counter()
                raw.connect(sockaddr)
                connect_ms = _ms(connect_start)
                sock = raw
                break
            except OSError as exc:
                last_error = exc
                raw.close()
        if sock is None:
            raise last_error or OSError("connect failed")
        if scheme == "https":
            tls_start = time.perf_counter()
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            sock.settimeout(timeout_sec)
            tls_ms = _ms(tls_start)

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: WhaleSignal-LatencyProbe/1\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        first_start = time.perf_counter()
        first = sock.recv(1)
        ttfb_ms = _ms(first_start)
        data = bytearray(first)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) >= 2_000_000:
                break
        total_ms = _ms(total_start)
        header = bytes(data).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        return Sample(
            ok=bool(first),
            url=url,
            status_line=header or None,
            bytes_read=len(data),
            dns_ms=dns_ms,
            connect_ms=connect_ms,
            tls_ms=tls_ms,
            ttfb_ms=ttfb_ms,
            total_ms=total_ms,
        )
    except Exception as exc:  # noqa: BLE001
        return Sample(
            ok=False,
            url=url,
            status_line=None,
            bytes_read=0,
            dns_ms=0.0,
            connect_ms=0.0,
            tls_ms=tls_ms,
            ttfb_ms=0.0,
            total_ms=_ms(total_start),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def _summary(samples: list[Sample], field: str) -> dict[str, float | None]:
    values = [float(getattr(sample, field)) for sample in samples if sample.ok and getattr(sample, field) is not None]
    if not values:
        return {"min": None, "p50": None, "p90": None, "p99": None, "max": None, "avg": None}
    return {
        "min": round(min(values), 3),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p99": _percentile(values, 0.99),
        "max": round(max(values), 3),
        "avg": round(statistics.fmean(values), 3),
    }


def summarize(url: str, samples: list[Sample]) -> dict[str, Any]:
    return {
        "url": url,
        "n": len(samples),
        "ok": sum(1 for sample in samples if sample.ok),
        "errors": [sample.error for sample in samples if sample.error][:5],
        "last_status": next((sample.status_line for sample in reversed(samples) if sample.status_line), None),
        "dns_ms": _summary(samples, "dns_ms"),
        "connect_ms": _summary(samples, "connect_ms"),
        "tls_ms": _summary(samples, "tls_ms"),
        "ttfb_ms": _summary(samples, "ttfb_ms"),
        "total_ms": _summary(samples, "total_ms"),
    }


def jitter_probe(*, iterations: int, sleep_ms: float) -> dict[str, Any]:
    delays: list[float] = []
    target = sleep_ms / 1000.0
    for _ in range(iterations):
        start = time.perf_counter()
        time.sleep(target)
        delays.append(max(0.0, _ms(start) - sleep_ms))
    return {
        "iterations": iterations,
        "sleep_ms": sleep_ms,
        "oversleep_ms": {
            "p50": _percentile(delays, 0.50),
            "p90": _percentile(delays, 0.90),
            "p99": _percentile(delays, 0.99),
            "max": round(max(delays), 3) if delays else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", dest="urls")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--timeout-sec", type=float, default=3.0)
    parser.add_argument("--pause-ms", type=float, default=100.0)
    parser.add_argument("--jitter-iterations", type=int, default=200)
    parser.add_argument("--jitter-sleep-ms", type=float, default=10.0)
    parser.add_argument("--samples", action="store_true", help="Include raw samples.")
    args = parser.parse_args()

    urls = tuple(args.urls or DEFAULT_URLS)
    report: dict[str, Any] = {
        "created_at_ms": int(time.time() * 1000),
        "host": socket.gethostname(),
        "repeats": int(args.repeats),
        "timeout_sec": float(args.timeout_sec),
        "endpoints": [],
        "scheduler_jitter": jitter_probe(
            iterations=max(1, int(args.jitter_iterations)),
            sleep_ms=float(args.jitter_sleep_ms),
        ),
    }
    for url in urls:
        samples: list[Sample] = []
        for index in range(max(1, int(args.repeats))):
            samples.append(measure_once(url, timeout_sec=float(args.timeout_sec)))
            if index + 1 < int(args.repeats):
                time.sleep(max(0.0, float(args.pause_ms)) / 1000.0)
        endpoint = summarize(url, samples)
        if args.samples:
            endpoint["samples"] = [asdict(sample) for sample in samples]
        report["endpoints"].append(endpoint)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from concurrent.futures import ThreadPoolExecutor
import threading

from p3_dual40_store import connect_dual40
from p3_web_dual40 import _DatabaseIntegrityCache


def test_database_integrity_check_is_cached(tmp_path, monkeypatch):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    conn.close()

    calls = []

    def checked(_conn):
        calls.append(True)
        return "ok"

    monkeypatch.setattr("p3_web_dual40.integrity_check", checked)
    cache = _DatabaseIntegrityCache(path, ttl_sec=60)

    assert cache.get() == "ok"
    assert cache.get() == "ok"
    assert calls == [True]


def test_concurrent_refresh_does_not_wait_for_integrity_scan(tmp_path, monkeypatch):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    conn.close()

    started = threading.Event()
    release = threading.Event()

    def slow_check(_conn):
        started.set()
        assert release.wait(timeout=2)
        return "ok"

    monkeypatch.setattr("p3_web_dual40.integrity_check", slow_check)
    cache = _DatabaseIntegrityCache(path, ttl_sec=60)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cache.get)
        assert started.wait(timeout=2)
        assert cache.get() == "checking"
        release.set()
        assert first.result(timeout=2) == "ok"

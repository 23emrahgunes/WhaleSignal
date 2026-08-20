from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from p26_config import P26Settings
from p26_dataset import open_p25_read_only


def test_p25_database_is_opened_query_only(tmp_path):
    path=tmp_path/"p25.sqlite"
    conn=sqlite3.connect(path); conn.execute("CREATE TABLE t(x INTEGER)"); conn.commit(); conn.close()
    ro=open_p25_read_only(str(path))
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()


def test_p26_settings_never_share_p25_database(tmp_path):
    settings=P26Settings(p25_db_path=str(tmp_path/"p25.sqlite"),p26_db_path=str(tmp_path/"p26.sqlite"))
    settings.validate_research_safety()
    assert Path(settings.p25_db_path)!=Path(settings.p26_db_path)


def test_p26_source_contains_no_execution_client_or_secret_fields():
    roots=[path for path in Path('.').glob('p26_*.py')]
    text='\n'.join(path.read_text(encoding='utf-8').lower() for path in roots)
    forbidden=('py_clob_client','private_key =','api_secret =','submit_order(','create_order(')
    for token in forbidden:
        assert token not in text

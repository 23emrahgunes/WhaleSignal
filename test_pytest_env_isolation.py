import os
import subprocess
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def test_pytest_does_not_load_local_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DIRECTION_ENGINE_TEST_DOTENV_SENTINEL=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DIRECTION_ENGINE_TEST_DOTENV_SENTINEL", raising=False)

    class TestSettings(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
        value: str = Field(
            default="repository-default",
            alias="DIRECTION_ENGINE_TEST_DOTENV_SENTINEL",
        )

    assert TestSettings().value == "repository-default"


def test_explicit_environment_variable_still_overrides_default(monkeypatch):
    monkeypatch.setenv("DIRECTION_ENGINE_TEST_ENV_SENTINEL", "from-process-env")

    class TestSettings(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
        value: str = Field(
            default="repository-default",
            alias="DIRECTION_ENGINE_TEST_ENV_SENTINEL",
        )

    assert TestSettings().value == "from-process-env"


def test_importing_pytest_conftest_removes_inherited_production_env_but_keeps_ci_overrides():
    env = os.environ.copy()
    env.update(
        {
            "P3_LIVE_FEATURE_ENABLED": "true",
            "P25_LIVE_ARMED": "true",
            "PAPER_STRATEGY_VERSION": "SHOULD_NOT_LEAK",
            "PAPER_DEEP_VALUE_MAX_ASK": "0.25",
            "POLYMARKET_SIGNATURE_TYPE": "3",
            "CLOB_API_KEY": "should-not-leak",
            "FORECAST_RECORDING_ENABLED": "true",
            "PHASE": "P1",
            "MODEL_TRAINING_ENABLED": "false",
            "CALIBRATION_ENABLED": "false",
            "PAPER_TRADING_ENABLED": "false",
        }
    )
    code = r'''
import os
import conftest
for name in (
    "P3_LIVE_FEATURE_ENABLED",
    "P25_LIVE_ARMED",
    "PAPER_STRATEGY_VERSION",
    "PAPER_DEEP_VALUE_MAX_ASK",
    "POLYMARKET_SIGNATURE_TYPE",
    "CLOB_API_KEY",
    "FORECAST_RECORDING_ENABLED",
):
    assert os.getenv(name) is None, (name, os.getenv(name))
assert os.getenv("PHASE") == "P1"
assert os.getenv("MODEL_TRAINING_ENABLED") == "false"
assert os.getenv("CALIBRATION_ENABLED") == "false"
assert os.getenv("PAPER_TRADING_ENABLED") == "false"
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

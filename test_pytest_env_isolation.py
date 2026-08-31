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

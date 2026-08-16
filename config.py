"""Merkezi konfigurasyon (Pydantic v2 + pydantic-settings).

Tum uc noktalar, esik degerleri ve risk parametreleri buradan yonetilir; `.env`
dosyasindan okunur. Gizli anahtarlar yalniz env'den gelir, koda gomulmez.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from models import ExecMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,  # bos env degeri (or. MANUAL_END_TS=) -> varsayilan kullan
    )

    # ----- Genel / calisma modu -----
    exec_mode: ExecMode = Field(default=ExecMode.SIM, alias="EXEC_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    symbol: str = Field(default="BTCUSDT", alias="SYMBOL")  # Binance sembolu

    # ----- Web dashboard -----
    web_enabled: bool = Field(default=True, alias="WEB_ENABLED")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8090, alias="WEB_PORT")

    # ----- Uc noktalar -----
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="CLOB_WS_URL",
    )
    clob_host: str = Field(default="https://clob.polymarket.com", alias="CLOB_HOST")
    binance_ws_base: str = Field(
        default="wss://stream.binance.com:9443/ws", alias="BINANCE_WS_BASE"
    )
    binance_rest_base: str = Field(
        default="https://api.binance.com", alias="BINANCE_REST_BASE"
    )
    deribit_rest_base: str = Field(
        default="https://www.deribit.com/api/v2", alias="DERIBIT_REST_BASE"
    )
    deribit_currency: str = Field(default="BTC", alias="DERIBIT_CURRENCY")
    gamma_host: str = Field(
        default="https://gamma-api.polymarket.com", alias="GAMMA_HOST"
    )
    # BTC 5dk up/down modu: /events/slug/btc-updown-5m-<pencere> ile aktif marketi
    # cozer ve 5dk'da bir DONEN marketi otomatik takip eder (VARSAYILAN).
    btc_5m: bool = Field(default=True, alias="BTC_5M")
    gamma_poll_sec: int = Field(default=5, alias="GAMMA_POLL_SEC")
    # Gamma market secimi: aranan slug parcasi (btc_5m=false iken kullanilir).
    gamma_market_slug: str = Field(default="", alias="GAMMA_MARKET_SLUG")
    # Manuel token id'leri (Gamma cozulemezse veya sabit market icin).
    manual_up_token_id: str = Field(default="", alias="MANUAL_UP_TOKEN_ID")
    manual_down_token_id: str = Field(default="", alias="MANUAL_DOWN_TOKEN_ID")
    manual_end_ts: float = Field(default=0.0, alias="MANUAL_END_TS")
    manual_duration_sec: float = Field(default=300.0, alias="MANUAL_DURATION_SEC")

    # ----- Strateji esikleri -----
    obi_max: float = Field(default=0.15, alias="OBI_MAX")  # |OBI| < bu -> simetrik
    atr_max_pct: float = Field(default=0.15, alias="ATR_MAX_PCT")  # ATR/fiyat yuzdesi
    adx_max: float = Field(default=20.0, alias="ADX_MAX")  # konsolidasyon
    time_decay_pct: float = Field(default=0.10, alias="TIME_DECAY_PCT")  # son %10
    saturation_eps: float = Field(default=0.02, alias="SATURATION_EPS")  # |dP/dt| esigi
    require_squeeze: bool = Field(default=False, alias="REQUIRE_SQUEEZE")

    # ----- Emir / risk -----
    # CLOSE_BUFFER_SEC: marketi gercek endDate'ten bu kadar sn ONCE bitmis say.
    # Polymarket UI islem-kilidine kadar sayar; gozlemlenen farki buraya yazinca
    # panel/karar Polymarket ile hizalanir (ve bot erken durup guvende kalir).
    close_buffer_sec: float = Field(default=0.0, alias="CLOSE_BUFFER_SEC")
    entry_price: float = Field(default=0.40, alias="ENTRY_PRICE")
    order_size: float = Field(default=5.0, alias="ORDER_SIZE")  # taraf basi pay
    single_leg_timeout_sec: float = Field(
        default=15.0, alias="SINGLE_LEG_TIMEOUT_SEC"
    )  # tek bacak guard
    max_open_boxes: int = Field(default=1, alias="MAX_OPEN_BOXES")
    max_pair_cost: float = Field(default=0.97, alias="MAX_PAIR_COST")

    # ----- Reconnect (exponential backoff) -----
    backoff_base_sec: float = Field(default=1.0, alias="BACKOFF_BASE_SEC")
    backoff_factor: float = Field(default=2.0, alias="BACKOFF_FACTOR")
    backoff_cap_sec: float = Field(default=30.0, alias="BACKOFF_CAP_SEC")
    ws_recv_timeout_sec: float = Field(default=30.0, alias="WS_RECV_TIMEOUT_SEC")

    # ----- CLOB gizli (DRY/LIVE) -----
    private_key: str = Field(default="", alias="PRIVATE_KEY")
    funder_address: str = Field(default="", alias="FUNDER_ADDRESS")
    signature_type: int = Field(default=3, alias="SIGNATURE_TYPE")
    chain_id: int = Field(default=137, alias="CHAIN_ID")
    clob_api_key: str = Field(default="", alias="CLOB_API_KEY")
    clob_api_secret: str = Field(default="", alias="CLOB_API_SECRET")
    clob_api_passphrase: str = Field(default="", alias="CLOB_API_PASSPHRASE")
    order_tick_size: str = Field(default="0.01", alias="ORDER_TICK_SIZE")
    neg_risk: bool = Field(default=False, alias="NEG_RISK")

    @field_validator("entry_price")
    @classmethod
    def _price_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("ENTRY_PRICE 0 ile 1 arasinda olmali")
        return v

    @field_validator("exec_mode", mode="before")
    @classmethod
    def _mode_upper(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    def live_ready(self) -> tuple[bool, Optional[str]]:
        """LIVE icin gerekli gizli alanlar dolu mu? (ok, eksik_sebep)."""
        if self.exec_mode != ExecMode.LIVE:
            return True, None
        if not self.private_key:
            return False, "PRIVATE_KEY eksik"
        if self.signature_type != 0 and not self.funder_address:
            return False, "FUNDER_ADDRESS eksik (signature_type != 0)"
        return True, None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Surec boyunca tekil ayar nesnesi."""
    return Settings()

"""Lazy Polymarket client adapters used only by guarded P3 LIVE workflows.

Importing this module does not require optional LIVE dependencies. SDK imports occur
inside functions, so normal DRY startup stays dependency-light. Secret values are
read from the process environment and are never returned by the public dashboard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class LiveSecretConfig:
    private_key: str | None
    wallet: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    signature_type: int
    funder: str | None

    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key)

    @property
    def has_full_clob_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def read_live_secrets() -> LiveSecretConfig:
    signature_raw = _env_first("POLYMARKET_SIGNATURE_TYPE") or "0"
    try:
        signature_type = int(signature_raw)
    except ValueError as exc:
        raise ValueError("POLYMARKET_SIGNATURE_TYPE must be an integer") from exc
    return LiveSecretConfig(
        private_key=_env_first("POLYMARKET_PRIVATE_KEY", "PK"),
        wallet=_env_first("POLYMARKET_WALLET", "POLYMARKET_DEPOSIT_WALLET"),
        api_key=_env_first("POLYMARKET_CLOB_API_KEY", "CLOB_API_KEY"),
        api_secret=_env_first("POLYMARKET_CLOB_API_SECRET", "CLOB_SECRET"),
        api_passphrase=_env_first(
            "POLYMARKET_CLOB_API_PASSPHRASE", "CLOB_PASS_PHRASE"
        ),
        signature_type=signature_type,
        funder=_env_first("POLYMARKET_FUNDER", "POLYMARKET_DEPOSIT_WALLET"),
    )


def require_live_dependency(module_name: str, install_hint: str) -> None:
    try:
        __import__(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"LIVE dependency missing: {module_name}. Install with: {install_hint}"
        ) from exc


def make_clob_client(*, host: str, chain_id: int):
    """Build an authenticated py-clob-client-v2 client."""
    require_live_dependency(
        "py_clob_client_v2", "./.venv/bin/pip install -r requirements-live.txt"
    )
    from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore

    secrets = read_live_secrets()
    if not secrets.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY/PK is not configured")

    kwargs: dict[str, Any] = {
        "host": host,
        "chain_id": int(chain_id),
        "key": secrets.private_key,
        "signature_type": int(secrets.signature_type),
    }
    if secrets.funder:
        kwargs["funder"] = secrets.funder

    if secrets.has_full_clob_creds:
        kwargs["creds"] = ApiCreds(
            api_key=str(secrets.api_key),
            api_secret=str(secrets.api_secret),
            api_passphrase=str(secrets.api_passphrase),
        )
        return ClobClient(**kwargs)

    bootstrap = ClobClient(**kwargs)
    creds = bootstrap.create_or_derive_api_key()
    kwargs["creds"] = creds
    return ClobClient(**kwargs)


def probe_clob_account(*, host: str, chain_id: int) -> dict[str, Any]:
    """Authenticated no-order account probe used by LIVE preflight.

    The returned payload is deliberately small and contains no credential material.
    ``balance_payload`` is retained in-memory only so the preflight can normalize the
    collateral balance and allowance; it is never exposed verbatim by the dashboard.
    """
    require_live_dependency(
        "py_clob_client_v2", "./.venv/bin/pip install -r requirements-live.txt"
    )
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # type: ignore

    client = make_clob_client(host=host, chain_id=chain_id)
    balance_payload = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    return {
        "signer": str(client.get_address()),
        "server_ok": client.get_ok(),
        "balance_payload": balance_payload,
    }


def make_secure_sdk_client():
    """Build the official unified SDK client for CTF merge workflows."""
    require_live_dependency(
        "polymarket", "./.venv/bin/pip install -r requirements-live.txt"
    )
    from polymarket.clients.secure import SecureClient  # type: ignore

    secrets = read_live_secrets()
    if not secrets.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY/PK is not configured")
    kwargs: dict[str, Any] = {"private_key": secrets.private_key}
    if secrets.wallet:
        kwargs["wallet"] = secrets.wallet
    return SecureClient.create(**kwargs)


def parse_clob_balance_usdc(payload: Any) -> float:
    """Normalize CLOB collateral balance to human USDC (6-decimal base units)."""
    if isinstance(payload, dict):
        raw = payload.get("balance", 0)
    else:
        raw = getattr(payload, "balance", 0)
    text = str(raw or "0").strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise RuntimeError(f"unrecognized CLOB balance value: {raw!r}") from exc
    if "." in text:
        return float(value)
    return float(value / Decimal(1_000_000))


def parse_conditional_balance_shares(payload: Any) -> float:
    """Normalize conditional-token balance to shares (6-decimal base units)."""
    if isinstance(payload, dict):
        raw = payload.get("balance", 0)
    else:
        raw = getattr(payload, "balance", 0)
    text = str(raw or "0").strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise RuntimeError(f"unrecognized conditional balance value: {raw!r}") from exc
    if "." in text:
        return float(value)
    return float(value / Decimal(1_000_000))

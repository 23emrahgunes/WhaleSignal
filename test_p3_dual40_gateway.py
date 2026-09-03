from pathlib import Path


LIVE_GATEWAY = Path("p3_live_dual40_gateway.py")


def test_dual40_gateway_uses_post_only_gtc_batch_and_heartbeat():
    text = LIVE_GATEWAY.read_text(encoding="utf-8")
    assert "post_orders(" in text
    assert "post_only=True" in text
    assert "OrderType.GTC" in text
    assert "post_heartbeat" in text
    assert "POST_ONLY_PAIR_NOT_BOTH_ACCEPTED" in text

    # Known accepted IDs are cancelled directly. Unknown/ambiguous submissions
    # are cancelled only within the two DUAL40 outcome-token scopes and are then
    # balance-reconciled by the runtime.
    assert "cancel_orders(values)" in text
    assert "cancel_market_orders(" in text
    assert "OrderMarketCancelParams(asset_id=token_id)" in text
    assert '"reconciliation_required": True' in text
    assert "cancel_all(" not in text


def test_dual40_gateway_never_uses_fok_or_fak_for_entry():
    text = LIVE_GATEWAY.read_text(encoding="utf-8")
    entry = text.split("def post_pair_post_only_gtc", 1)[1].split(
        "def cancel_pair",
        1,
    )[0]
    assert "OrderType.FOK" not in entry
    assert "OrderType.FAK" not in entry
    assert "post_only=True" in entry


def test_research_gateway_is_only_a_compatibility_import():
    text = Path("p3_dual40_gateway.py").read_text(encoding="utf-8")
    assert "from p3_live_dual40_gateway import Dual40Gateway" in text
    assert "py_clob_client" not in text
    assert "create_order(" not in text

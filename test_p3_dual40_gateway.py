from pathlib import Path


def test_dual40_gateway_uses_post_only_gtc_batch_and_heartbeat():
    text = Path("p3_dual40_gateway.py").read_text(encoding="utf-8")
    assert "post_orders(" in text
    assert "post_only=True" in text
    assert "OrderType.GTC" in text
    assert "post_heartbeat" in text
    assert "POST_ONLY_PAIR_NOT_BOTH_ACCEPTED" in text
    assert "cancel_orders(accepted)" in text


def test_dual40_gateway_never_uses_fok_or_fak_for_entry():
    text = Path("p3_dual40_gateway.py").read_text(encoding="utf-8")
    entry = text.split("def post_pair_post_only_gtc", 1)[1].split("def cancel_pair", 1)[0]
    assert "OrderType.FOK" not in entry
    assert "OrderType.FAK" not in entry
    assert "post_only=True" in entry

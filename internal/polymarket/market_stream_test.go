package polymarket

import "testing"

func TestDecodeRawLastTradePrice(t *testing.T) {
	rows := decodeMarketTrades([]byte(`{"event_type":"last_trade_price","asset_id":"tok","price":"0.41","size":"2.5","side":"SELL","timestamp":"1782753357257"}`))
	if len(rows) != 1 {
		t.Fatalf("rows=%d", len(rows))
	}
	if rows[0].TokenID != "tok" || rows[0].Price != .41 || rows[0].Size != 2.5 || rows[0].Side != "SELL" {
		t.Fatalf("bad trade %+v", rows[0])
	}
}

func TestDecodeNormalizedLastTradePrice(t *testing.T) {
	rows := decodeMarketTrades([]byte(`{"topic":"market","type":"last_trade_price","payload":{"tokenId":"tok2","price":"0.55","size":"5","side":"BUY","timestamp":"2026-06-29T17:15:57.257000Z"}}`))
	if len(rows) != 1 || rows[0].TokenID != "tok2" || rows[0].Side != "BUY" || rows[0].Size != 5 {
		t.Fatalf("bad normalized decode %+v", rows)
	}
}

func TestDecodeIgnoresNonTradeAndInvalidSide(t *testing.T) {
	if got := decodeMarketTrades([]byte(`{"event_type":"book","asset_id":"tok"}`)); len(got) != 0 {
		t.Fatalf("book must not decode as trade %+v", got)
	}
	if got := decodeMarketTrades([]byte(`{"event_type":"last_trade_price","asset_id":"tok","price":"0.4","size":"5","side":"X"}`)); len(got) != 0 {
		t.Fatalf("invalid side %+v", got)
	}
}

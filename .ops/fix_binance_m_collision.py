from pathlib import Path

p = Path('internal/binance/microstructure.go')
s = p.read_text()

old = '''type aggTradeEvent struct {\n\tEventTime        int64  `json:"E"`\n\tAggregateTradeID int64  `json:"a"`\n\tPrice            string `json:"p"`\n\tQuantity         string `json:"q"`\n\tTradeTime        int64  `json:"T"`\n\tBuyerIsMaker     bool   `json:"m"`\n}\n\ntype aggTradeRESTEvent struct {\n\tAggregateTradeID int64  `json:"a"`\n\tPrice            string `json:"p"`\n\tQuantity         string `json:"q"`\n\tTradeTime        int64  `json:"T"`\n\tBuyerIsMaker     bool   `json:"m"`\n}'''
new = '''type aggTradeEvent struct {\n\tEventTime        int64  `json:"E"`\n\tAggregateTradeID int64  `json:"a"`\n\tPrice            string `json:"p"`\n\tQuantity         string `json:"q"`\n\tTradeTime        int64  `json:"T"`\n\tBuyerIsMaker     bool   `json:"m"`\n\tBestPriceMatch   bool   `json:"M"`\n}\n\ntype aggTradeRESTEvent struct {\n\tAggregateTradeID int64  `json:"a"`\n\tPrice            string `json:"p"`\n\tQuantity         string `json:"q"`\n\tTradeTime        int64  `json:"T"`\n\tBuyerIsMaker     bool   `json:"m"`\n\tBestPriceMatch   bool   `json:"M"`\n}'''
assert old in s, 'aggTrade structs not found'
s = s.replace(old, new, 1)
p.write_text(s)

p = Path('internal/binance/microstructure_test.go')
s = p.read_text()
if '"encoding/json"' not in s:
    s = s.replace('import (\n', 'import (\n\t"encoding/json"\n', 1)

append = r'''

func TestAggTradeJSONLowercaseMakerDoesNotGetOverwrittenByUppercaseM(t *testing.T) {
	raw := []byte(`{"a":4032117918,"p":"63685.22000000","q":"0.00039000","T":1786483122661,"m":false,"M":true}`)
	var rest aggTradeRESTEvent
	if err := json.Unmarshal(raw, &rest); err != nil {
		t.Fatal(err)
	}
	if rest.BuyerIsMaker {
		t.Fatal("lowercase m=false was overwritten by uppercase M=true")
	}
	if !rest.BestPriceMatch {
		t.Fatal("expected uppercase M to decode into its own field")
	}

	var ws aggTradeEvent
	if err := json.Unmarshal(raw, &ws); err != nil {
		t.Fatal(err)
	}
	if ws.BuyerIsMaker {
		t.Fatal("websocket lowercase m=false was overwritten by uppercase M=true")
	}
	if !ws.BestPriceMatch {
		t.Fatal("expected websocket uppercase M to decode separately")
	}
}

func TestAggTradeJSONMakerTrueStillClassifiesSell(t *testing.T) {
	raw := []byte(`{"a":4032117921,"p":"63685.21000000","q":"0.00101000","T":1786483123963,"m":true,"M":true}`)
	var ev aggTradeRESTEvent
	if err := json.Unmarshal(raw, &ev); err != nil {
		t.Fatal(err)
	}
	if !ev.BuyerIsMaker {
		t.Fatal("expected lowercase m=true to remain true")
	}
}
'''
if 'TestAggTradeJSONLowercaseMakerDoesNotGetOverwrittenByUppercaseM' not in s:
    s += append
p.write_text(s)

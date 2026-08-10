package polymarket

import "testing"

func TestParsePriceToBeat(t *testing.T) {
	client := NewClient()

	tests := []struct {
		question string
		expected float64
		ok       bool
	}{
		{"Bitcoin above $118,500 at 14:35?", 118500.0, true},
		{"Bitcoin above 118500 at 14:35?", 118500.0, true},
		{"BTC above $118,500", 118500.0, true},
		{"BTC above 118500", 118500.0, true},
		{"BTC > 118500", 118500.0, true},
		{"BTC > $118,500", 118500.0, true},
		{"Bitcoin over 118500", 118500.0, true},
		{"Bitcoin over $118,500", 118500.0, true},
		{"Some random question with no numbers", 0.0, false},
	}

	for _, tt := range tests {
		val, ok := client.ParsePriceToBeat(tt.question)
		if ok != tt.ok {
			t.Errorf("question %q: expected ok=%v, got=%v", tt.question, tt.ok, ok)
		}
		if ok && val != tt.expected {
			t.Errorf("question %q: expected price=%f, got=%f", tt.question, tt.expected, val)
		}
	}
}

func TestIs5MinMarket(t *testing.T) {
	client := NewClient()

	tests := []struct {
		question string
		expected bool
	}{
		{"Bitcoin above $118,500 at 14:35?", true},
		{"BTC > 118500", false},
		{"BTC over $118,500 by 15:00?", true},
	}

	for _, tt := range tests {
		got := client.Is5MinMarket(tt.question)
		if got != tt.expected {
			t.Errorf("question %q: expected %v, got %v", tt.question, tt.expected, got)
		}
	}
}

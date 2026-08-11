package polymarket

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestFetchBookSnapshotParsesDynamicMinAndTick(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"bids":[{"price":"0.40","size":"9"},{"price":"0.41","size":"7"}],"asks":[{"price":"0.44","size":"8"},{"price":"0.43","size":"6"}],"min_order_size":"5","tick_size":"0.01"}`))
	}))
	defer srv.Close()
	c := NewClientWithBaseURL("http://unused", srv.Client())
	b, err := c.fetchBookSnapshot(srv.URL, "tok")
	if err != nil {
		t.Fatal(err)
	}
	if b.BestBid != .41 || b.BestAsk != .43 || b.MinOrderSize != 5 || b.TickSize != .01 {
		t.Fatalf("bad book %+v", b)
	}
	if len(b.Bids) != 2 || len(b.Asks) != 2 {
		t.Fatalf("levels missing %+v", b)
	}
}

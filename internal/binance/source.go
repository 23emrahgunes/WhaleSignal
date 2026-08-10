package binance

// SetDataSource updates the source marker without exposing Client internals to
// concurrent WS/mock goroutines.
func (c *Client) SetDataSource(source string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.DataSource = source
}

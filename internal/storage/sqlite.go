package storage

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

	"pm-edge/internal/engine"
	_ "modernc.org/sqlite"
)

type Database struct { db *sql.DB }

func NewDatabase(dbPath string) (*Database, error) {
	dir := ""
	for i := len(dbPath)-1; i >= 0; i-- { if dbPath[i]=='/' { dir=dbPath[:i]; break } }
	if dir != "" { _ = os.MkdirAll(dir, 0755) }
	db, err := sql.Open("sqlite", dbPath); if err != nil { return nil, err }
	inst := &Database{db:db}
	if err := inst.migrate(); err != nil { _=db.Close(); return nil,err }
	return inst,nil
}

func (d *Database) Close() error { if d.db!=nil{return d.db.Close()}; return nil }

func (d *Database) migrate() error {
	query := `
	CREATE TABLE IF NOT EXISTS signals (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp TEXT NOT NULL,
		question TEXT NOT NULL,
		slug TEXT NOT NULL,
		market_end_time TEXT NOT NULL,
		price_to_beat REAL NOT NULL,
		current_price REAL NOT NULL,
		spot_minus_price_to_beat REAL NOT NULL,
		seconds_remaining REAL NOT NULL,
		p_up REAL NOT NULL,
		p_down REAL NOT NULL,
		bid_vol REAL NOT NULL,
		ask_vol REAL NOT NULL,
		spoof_filtered_bid_vol REAL NOT NULL,
		spoof_filtered_ask_vol REAL NOT NULL,
		imbalance REAL NOT NULL,
		weighted_imbalance REAL NOT NULL,
		probability_score REAL NOT NULL,
		order_flow_score REAL NOT NULL,
		technical_score REAL NOT NULL,
		volatility REAL NOT NULL,
		drift REAL NOT NULL,
		composite_score REAL NOT NULL,
		final_score REAL NOT NULL,
		decision TEXT NOT NULL,
		confidence REAL NOT NULL,
		market_stale INTEGER NOT NULL,
		data_source TEXT NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
	-- REV-FIX: remove only the exact synthetic fallback rows produced by the old evaluator.
	DELETE FROM signals WHERE slug = 'btc-above-100k-1505' AND price_to_beat = 100000;
	`
	_,err:=d.db.Exec(query); return err
}

func (d *Database) InsertSignal(r *engine.EvaluationResult) error {
	if r==nil{return fmt.Errorf("refusing nil signal")}
	if r.PriceToBeat<=0||r.CurrentPrice<=0{return fmt.Errorf("refusing signal with invalid prices")}
	if r.SecondsRemaining<=0||r.SecondsRemaining>305{return fmt.Errorf("refusing signal with invalid remaining time %.3f",r.SecondsRemaining)}
	if r.MarketStale{return fmt.Errorf("refusing stale market signal")}
	if !strings.HasPrefix(r.Slug,"btc-updown-5m-"){return fmt.Errorf("refusing non-canonical BTC 5m slug %q",r.Slug)}
	if strings.Contains(strings.ToUpper(r.DataSource),"MOCK"){return fmt.Errorf("refusing mock signal")}

	query:=`
	INSERT INTO signals (
		timestamp, question, slug, market_end_time, price_to_beat, current_price,
		spot_minus_price_to_beat, seconds_remaining, p_up, p_down, bid_vol, ask_vol,
		spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
	staleInt:=0
	_,err:=d.db.Exec(query,r.Timestamp,r.Question,r.Slug,r.MarketEndTime,r.PriceToBeat,r.CurrentPrice,r.SpotMinusPriceToBeat,r.SecondsRemaining,r.PUp,r.PDown,r.BidVol,r.AskVol,r.SpoofFilteredBidVol,r.SpoofFilteredAskVol,r.Imbalance,r.WeightedImbalance,r.ProbabilityScore,r.OrderFlowScore,r.TechnicalScore,r.Volatility,r.Drift,r.CompositeScore,r.FinalScore,r.Decision,r.Confidence,staleInt,r.DataSource)
	return err
}

func (d *Database) GetHistory(limit int) ([]engine.EvaluationResult,error) {
	if limit<=0{return []engine.EvaluationResult{},nil}; if limit>10000{limit=10000}
	query:=`
	SELECT timestamp, question, slug, market_end_time, price_to_beat, current_price,
		spot_minus_price_to_beat, seconds_remaining, p_up, p_down, bid_vol, ask_vol,
		spoof_filtered_bid_vol, spoof_filtered_ask_vol, imbalance, weighted_imbalance,
		probability_score, order_flow_score, technical_score, volatility, drift,
		composite_score, final_score, decision, confidence, market_stale, data_source
	FROM signals ORDER BY id DESC LIMIT ?`
	rows,err:=d.db.Query(query,limit); if err!=nil{return nil,err}; defer rows.Close()
	results:=make([]engine.EvaluationResult,0)
	for rows.Next(){var r engine.EvaluationResult;var stale int; if err:=rows.Scan(&r.Timestamp,&r.Question,&r.Slug,&r.MarketEndTime,&r.PriceToBeat,&r.CurrentPrice,&r.SpotMinusPriceToBeat,&r.SecondsRemaining,&r.PUp,&r.PDown,&r.BidVol,&r.AskVol,&r.SpoofFilteredBidVol,&r.SpoofFilteredAskVol,&r.Imbalance,&r.WeightedImbalance,&r.ProbabilityScore,&r.OrderFlowScore,&r.TechnicalScore,&r.Volatility,&r.Drift,&r.CompositeScore,&r.FinalScore,&r.Decision,&r.Confidence,&stale,&r.DataSource);err!=nil{return nil,err};r.MarketStale=stale==1;results=append(results,r)}
	if err:=rows.Err();err!=nil{return nil,err}
	return results,nil
}

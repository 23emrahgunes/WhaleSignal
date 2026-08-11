package engine

import "pm-edge/internal/binance"

type MicrostructureScores struct {
	Ready               bool
	DeepBookScore       float64
	TradeFlowScore      float64
	WallDynamicsScore   float64
	PTBBarrierScore     float64
	MicrostructureScore float64
}

func ScoreMicrostructure(s binance.DeepMicroSnapshot) MicrostructureScores {
	out := MicrostructureScores{Ready: s.Ready && s.TradeFlowAvailable, PTBBarrierScore: clampScore(s.PTBBarrierScore)}
	band := func(distance float64) float64 {
		for _, b := range s.Bands {
			if b.DistanceUSD == distance {
				return clampScore(b.Imbalance)
			}
		}
		return 0
	}
	out.DeepBookScore = clampScore(0.15*band(10) + 0.20*band(25) + 0.35*band(50) + 0.30*band(75))
	trade := func(seconds int) float64 {
		for _, w := range s.Trades {
			if w.Seconds == seconds {
				return clampScore(w.Imbalance)
			}
		}
		return 0
	}
	out.TradeFlowScore = clampScore(0.30*trade(5) + 0.25*trade(15) + 0.20*trade(30) + 0.15*trade(60) + 0.10*s.TradeAcceleration)
	wallStanding := clampScore(s.BidWallScore - s.AskWallScore)
	wallDepletion := clampScore(s.AskDepletionScore - s.BidDepletionScore)
	out.WallDynamicsScore = clampScore(0.55*wallStanding + 0.45*wallDepletion)
	out.MicrostructureScore = clampScore(0.30*out.DeepBookScore + 0.40*out.TradeFlowScore + 0.20*out.WallDynamicsScore + 0.10*out.PTBBarrierScore)
	return out
}

func ShadowModelB(probabilityScore, technicalScore float64, m MicrostructureScores) float64 {
	if !m.Ready {
		return 0
	}
	return clampScore(0.45*probabilityScore + 0.20*m.DeepBookScore + 0.20*m.TradeFlowScore + 0.10*technicalScore + 0.05*m.PTBBarrierScore)
}

func ShadowDecision(score float64) (string, float64) {
	decision := "NEUTRAL"
	if score >= 0.20 {
		decision = "UP"
	} else if score <= -0.20 {
		decision = "DOWN"
	}
	confidence := score * 100
	if confidence < 0 {
		confidence = -confidence
	}
	if confidence > 100 {
		confidence = 100
	}
	return decision, confidence
}

func clampScore(v float64) float64 {
	if v < -1 {
		return -1
	}
	if v > 1 {
		return 1
	}
	return v
}

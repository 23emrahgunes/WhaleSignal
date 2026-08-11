from pathlib import Path

p=Path('internal/paper/engine_test.go')
s=p.read_text()
old='Tokens:[]polymarket.Token{{Outcome:"Up",Price:.5},{Outcome:"Down",Price:.5}}}'
new='Tokens:[]polymarket.Token{{Outcome:"Up",Price:.5,TokenID:"up-token"},{Outcome:"Down",Price:.5,TokenID:"down-token"}}}'
if old not in s: raise SystemExit('engine test token fixture anchor missing')
p.write_text(s.replace(old,new,1))

p=Path('internal/paper/hedge_test.go')
s=p.read_text()
old='''\t\tPUp: 0.70, PDown: 0.30, FinalScore: 0.60, Decision: "UP", Confidence: 60,\n\t\tDataSource: "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",\n'''
new='''\t\tPUp: 0.70, PDown: 0.30, FinalScore: 0.60, Decision: "UP", Confidence: 60,\n\t\tDataSource: "CHAINLINK_RTDS+BINANCE_WS+BINANCE_WS_DEPTH20",\n\t\tPTBTerminal: engine.PTBTerminalEstimate{Ready: true, Decision: "UP", PAbove: 0.80, PBelow: 0.20},\n'''
if old not in s: raise SystemExit('hedge test CLOB fixture anchor missing')
p.write_text(s.replace(old,new,1))
print('PTB economic gate fixtures fixed')

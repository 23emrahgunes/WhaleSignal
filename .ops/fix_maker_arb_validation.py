from pathlib import Path

p=Path('internal/storage/arb.go')
s=p.read_text().replace('boolInt(s.PairEdgePass)', 'arbBoolInt(s.PairEdgePass)').replace('boolInt(s.PTBReady)', 'arbBoolInt(s.PTBReady)').replace('func boolInt(v bool) int', 'func arbBoolInt(v bool) int')
p.write_text(s)

p=Path('internal/arb/engine_test.go')
s=p.read_text().replace('if got != .55 { t.Fatalf("got %.4f want .55 (post-only ceiling)", got) }', 'if got != .56 { t.Fatalf("got %.4f want .56 (arb ceiling)", got) }')
p.write_text(s)

from pathlib import Path
p=Path('.ops/apply_arb_paper.py')
s=p.read_text()
s=s.replace("'    DownCompletionMax float64 `json:\"downCompletionMax\"`\\n    UpCompletionMax   float64 `json:\"upCompletionMax\"`\\n'", "'\\tDownCompletionMax float64 `json:\"downCompletionMax\"`\\n\\tUpCompletionMax   float64 `json:\"upCompletionMax\"`\\n'")
s=s.replace("'    mux.HandleFunc(\"/api/arb/stats\", s.cors(s.handleArbStats))\\n'", "'\\tmux.HandleFunc(\"/api/arb/stats\", s.cors(s.handleArbStats))\\n'")
s=s.replace("'    ArbMaxStrandedUnits   int\\n'", "'\\tArbMaxStrandedUnits   int\\n'")
s=s.replace("'        ArbMaxStrandedUnits:       envInt(\"ARB_MAX_STRANDED_UNITS\", 1),\\n'", "'\\t\\tArbMaxStrandedUnits:       envInt(\"ARB_MAX_STRANDED_UNITS\", 1),\\n'")
p.write_text(s)

from pathlib import Path
p=Path('internal/engine/evaluator.go')
s=p.read_text()
old='\t\tShadowConfidence:         shadowConfidence,\n\t}'
new='\t\tShadowConfidence:         shadowConfidence,\n\t\tPTBTerminal:              ptbTerminal,\n\t}'
if old not in s:
    raise SystemExit('evaluator return anchor not found')
p.write_text(s.replace(old,new,1))
print('PTB terminal evaluator return wiring fixed')

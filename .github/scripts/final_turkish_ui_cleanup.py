from pathlib import Path
p=Path('web/static/index.html')
s=p.read_text(encoding='utf-8')
s=s.replace('<span>Status</span>','<span>Durum</span>')
s=s.replace(" · z=${Number(c.zScore||0).toFixed(2)}`"," · istatistik z-skoru=${Number(c.zScore||0).toFixed(2)}`")
s=s.replace("'CLOB_QUOTE_ERROR':'CLOB fiyat teklifi alınamadı'","'CLOB_QUOTE_ERROR':'Polymarket emir defteri fiyat teklifi alınamadı'")
s=s.replace("Momentum:'Momentum'","Momentum:'Fiyat İvmesi (Momentum)'")
s=s.replace("age+' ms'","age+' milisaniye'")
s=s.replace("${dm.ageMs||0}ms","${dm.ageMs||0} milisaniye")
s=s.replace("${dm.ageMs??'—'}ms","${dm.ageMs??'—'} milisaniye")
for x in ['<span>Status</span>',' · z=${Number(c.zScore||0).toFixed(2)}`']:
    if x in s: raise SystemExit('cleanup failed: '+x)
p.write_text(s,encoding='utf-8')

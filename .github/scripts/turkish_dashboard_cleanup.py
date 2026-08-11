from pathlib import Path

p=Path('web/static/index.html')
s=p.read_text(encoding='utf-8')

# Remaining visible English/abbreviated wording.
s=s.replace('CLOB kağıt işlem simülasyonu','Polymarket emir defteri kağıt işlem simülasyonu')
s=s.replace('CLOB verileri','Polymarket emir defteri verileri')
s=s.replace("chip('API HATASI','down')","chip('VERİ BAĞLANTISI HATASI','down')")
s=s.replace("'BINANCE_REST_DEPTH20':'Binance REST · İlk 20 kademe'","'BINANCE_REST_DEPTH20':'Binance yedek veri kaynağı · İlk 20 kademe'")
s=s.replace("'BINANCE_WS_DEPTH20':'Binance WebSocket · İlk 20 kademe'","'BINANCE_WS_DEPTH20':'Binance canlı veri akışı · İlk 20 kademe'")
s=s.replace("'BINANCE_DEEP_REST1000':'Binance REST · Derin 1000 kademe'","'BINANCE_DEEP_REST1000':'Binance yedek veri kaynağı · Derin 1000 kademe'")
s=s.replace("'BINANCE_DEEP_DIFF':'Binance WebSocket · Derin fark akışı'","'BINANCE_DEEP_DIFF':'Binance canlı derinlik fark akışı'")
s=s.replace("};return m[v]||v||'Bilinmiyor'}", "};return m[v]||'Bilinmeyen teknik veri kaynağı'}",1)
s=s.replace("};return m[v]||String(v||'ENGELLENDİ').replaceAll('_',' ')}", "};return m[v]||'Koşullardan biri henüz sağlanmadı'}")
s=s.replace("};return m[String(v||'').toLowerCase()]||v||'Veri toplanıyor'}", "};return m[String(v||'').toLowerCase()]||'Veri toplanıyor'}")

# Plain Turkish for dynamic labels.
s=s.replace("Henüz eşik koşullarını geçen paper trade yok.","Henüz eşik koşullarını geçen kağıt işlem yok.")
s=s.replace("document.getElementById('entryTf').textContent=activeTf;document.getElementById('hedgeTf').textContent=activeTf;", "const tfText=activeTf==='15m'?'15 dk':'5 dk';document.getElementById('entryTf').textContent=tfText;document.getElementById('hedgeTf').textContent=tfText;")
s=s.replace("`${Math.round(e.secondsRemaining||0)}s / ${Math.round(e.minSeconds||0)}-${Math.round(e.maxSeconds||0)}s ${boolChip(e.timePass)}`", "`${Math.round(e.secondsRemaining||0)} sn / ${Math.round(e.minSeconds||0)}-${Math.round(e.maxSeconds||0)} sn ${boolChip(e.timePass)}`")
s=s.replace("`${usd(e.cashBalance)} / stake ${usd(e.stake)} ${boolChip(e.balancePass)}`", "`${usd(e.cashBalance)} / işlem tutarı ${usd(e.stake)} ${boolChip(e.balancePass)}`")
s=s.replace("gateRow('CLOB satış fiyatı / ortalama gerçekleşme (VWAP)'", "gateRow('Polymarket emir defteri satış fiyatı / ortalama gerçekleşme (VWAP)'")
s=s.replace("`${h.reverseVotes||0}/${h.windowSize||0} · min ${h.minVotes||0}`", "`${h.reverseVotes||0}/${h.windowSize||0} · en az ${h.minVotes||0}`")
s=s.replace("`${pct(h.reverseProbability||0)} / min ${pct(h.minProbability||0)} ${boolChip(h.probabilityPass)}`", "`${pct(h.reverseProbability||0)} / en az ${pct(h.minProbability||0)} ${boolChip(h.probabilityPass)}`")
s=s.replace("`${pct(h.edge||0)} / min ${pct(h.minEdge||0)} ${boolChip(h.edgePass)}`", "`${pct(h.edge||0)} / en az ${pct(h.minEdge||0)} ${boolChip(h.edgePass)}`")
s=s.replace("`z=${Number(c.zScore||0).toFixed(2)}`", "`istatistik z-skoru=${Number(c.zScore||0).toFixed(2)}`")

# Fix B/A shorthand in deep panel.
s=s.replace("document.getElementById('wallDynamics').textContent=`${pct(data.wallDynamicsScore||0)} · B ${pct(dm.bidWallScore||0)} / A ${pct(dm.askWallScore||0)}`;", "document.getElementById('wallDynamics').textContent=`${pct(data.wallDynamicsScore||0)} · Alış duvarı ${pct(dm.bidWallScore||0)} / Satış duvarı ${pct(dm.askWallScore||0)}`;")

# Friendly market slug for both timeframes.
needle="function timeOnly(v){try{return new Date(v).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch{return v||'—'}}"
if needle in s and 'function marketLabel(' not in s:
    s=s.replace(needle, needle+"\nfunction marketLabel(v){return String(v||'—').replace('btc-updown-5m-','').replace('btc-updown-15m-','')}",1)
s=s.replace("t.marketSlug.replace('btc-updown-5m-','')","marketLabel(t.marketSlug)")
s=s.replace("h.marketSlug.replace('btc-updown-5m-','')","marketLabel(h.marketSlug)")

# Initial timeframe text.
s=s.replace('id="entryTf">5m</span>','id="entryTf">5 dk</span>')
s=s.replace('id="hedgeTf">5m</span>','id="hedgeTf">5 dk</span>')

# Explain CLOB as well; insert once after Baz Puan glossary card.
anchor='''      <div class="mini"><span>Baz Puan</span><strong style="font-size:13px">Fiyat hareketini küçük ölçekte gösterir. 1 baz puan = %0,01.</strong></div>'''
extra=anchor+'''\n      <div class="mini"><span>Polymarket Emir Defteri (CLOB)</span><strong style="font-size:13px">Polymarket'teki gerçek alış ve satış fiyatlarının bulunduğu emir defteridir. Kağıt işlemlerde gerçekçi giriş maliyetini buradan hesaplıyoruz.</strong></div>'''
if anchor in s and 'Polymarket Emir Defteri (CLOB)' not in s:
    s=s.replace(anchor,extra,1)

# Additional visible English fragments that should not remain.
for old,new in {
    'Paper entries':'Kağıt işlem girişleri',
    'Paper trade':'Kağıt işlem',
    'paper trade':'kağıt işlem',
    'Persistent Reverse Regime':'Kalıcı Ters Yön Rejimi',
    'Return on Stake':'Yatırılan Tutar Getirisi',
    'Avg Return':'Ortalama Getiri',
    'Win Rate':'Kazanma Oranı',
    'Cash Balance':'Nakit Bakiye',
    'Time Remaining':'Kalan Süre',
    'Final Bias':'Nihai Yön',
}.items():
    s=s.replace(old,new)

# Safety checks for visible leftovers we explicitly care about.
for needle in ['>Paper Trades<','>Cash Balance<','>Win Rate<','>Return on Stake<','>Time Remaining<','>Final Bias<','>Entry Economic Edge<','>Depth Freshness<','>Real-Time Signals & Prediction<']:
    if needle in s:
        raise SystemExit('visible English remains: '+needle)

p.write_text(s,encoding='utf-8')

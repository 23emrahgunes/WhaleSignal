from pathlib import Path

p = Path('web/static/index.html')
s = p.read_text(encoding='utf-8')

# Static visible labels.
R = {
    '<title>PM-Edge Research Dashboard</title>': '<title>PM-Edge BTC Yön Araştırma Paneli</title>',
    '<header><h1>PM-Edge TV-Direction</h1><div class="subtitle">BTC 5m + 15m · Chainlink Reference · Conservative Terminal Forecast · Binance Deep Microstructure · CLOB Paper Execution · Shadow Hedge A/B</div></header>': '<header><h1>PM-Edge BTC Yön Analizi</h1><div class="subtitle">BTC 5 dk + 15 dk · Chainlink referansı · Temkinli kapanış tahmini · Binance derin piyasa yapısı · CLOB kağıt işlem simülasyonu · Gölge koruma A/B testi</div></header>',
    '<div class="banner"><b>PAPER-ONLY RESEARCH ENGINE</b> — Canlı emir göndermez. CLOB okumaları yalnızca gerçekçi paper fill/hedge simülasyonu içindir.</div>': '<div class="banner"><b>YALNIZCA KAĞIT İŞLEM ARAŞTIRMA MOTORU</b> — Canlı emir göndermez. CLOB verileri yalnızca gerçekçi sanal gerçekleşme ve koruma simülasyonu için kullanılır.</div>',
    '>BTC 5 MIN<': '>BTC 5 DAKİKA<',
    '>BTC 15 MIN<': '>BTC 15 DAKİKA<',
    '<h2>Real-Time Signals & Prediction</h2>': '<h2>Anlık Sinyaller ve Yön Tahmini</h2>',
    '>Current BTC / Chainlink<': '>Güncel BTC / Chainlink Fiyatı<',
    '>Price To Beat<': '>Hedef Fiyat (PTB)<',
    '>Time Remaining<': '>Kalan Süre<',
    '>Final Bias<': '>Nihai Yön<',
    '>Composite Confidence<': '>Birleşik Güven Skoru<',
    '>Terminal P(UP) / P(DOWN)<': '>Kapanışta P(Yukarı) / P(Aşağı)<',
    '<h2>Legacy Depth20 — Model A Order-Book Input</h2>': '<h2>Eski İlk 20 Kademe — Model A Emir Defteri Girdisi</h2>',
    '>Bid / Ask Imbalance<': '>Alış / Satış Dengesizliği<',
    '>Rank-Weighted Imbalance<': '>Yakın Kademeye Ağırlıklı Dengesizlik<',
    '>Total Bid / Ask Volumes (Depth20)<': '>Toplam Alış / Satış Hacmi (İlk 20 Kademe)<',
    '>Total Depth20 USD<': '>İlk 20 Kademe Toplam USD<',
    '>Bid / Ask USD<': '>Alış / Satış USD<',
    '>Spread<': '>Alış-Satış Farkı<',
    '>Bid 20 Range<': '>20 Alış Kademesinin Fiyat Aralığı<',
    '>Ask 20 Range<': '>20 Satış Kademesinin Fiyat Aralığı<',
    '>Order-Flow Score<': '>Emir Defteri Baskı Skoru<',
    '>Depth Source<': '>Derinlik Veri Kaynağı<',
    '>Depth Freshness<': '>Derinlik Verisi Güncelliği<',
    '<h2>PTB Terminal Forecast — Uncertainty Diagnostics</h2>': '<h2>Hedef Fiyat Kapanış Tahmini — Belirsizlik Ölçümleri</h2>',
    '>Forecast @ Expiry<': '>Kapanış İçin Tahmini Fiyat<',
    '>68% Forecast Band<': '>%68 Olasılık Fiyat Aralığı<',
    '>95% Forecast Band<': '>%95 Olasılık Fiyat Aralığı<',
    '>PTB Z / Model Confidence<': '>Hedef Fiyata Uzaklık (Z/σ) / Model Güveni<',
    '>Required / Expected Move<': '>Gerekli / Beklenen Hareket<',
    '>1σ @ Expiry<': '>Kapanışa Kadar 1σ Oynaklık<',
    '>Binance / Chainlink Basis<': '>Binance / Chainlink Fiyat Farkı<',
    '>Forecast Samples<': '>Tahminde Kullanılan Örnek Sayısı<',
    '>Micro Vol Annual<': '>Kısa Vadeli Yıllıklandırılmış Oynaklık<',
    '>Macro Vol Floor Annual<': '>Asgari Yıllık Oynaklık Varsayımı<',
    '>Basis Vol Annual<': '>Fiyat Farkı Yıllık Oynaklığı<',
    '>Binance Price<': '>Binance Fiyatı<',
    '<h2>Binance Deep Microstructure — Shadow Model B</h2>': '<h2>Binance Derin Piyasa Yapısı — Gölge Model B</h2>',
    '<b>SHADOW ONLY</b> — Paper entries still follow Model A until out-of-sample evidence promotes Model B.': '<b>YALNIZCA GÖLGE TESTİ</b> — Model B bağımsız veride üstünlüğünü kanıtlayana kadar kağıt işlemleri Model A açar.',
    '>Deep Book Health<': '>Derin Emir Defteri Sağlığı<',
    '>Shadow B Direction<': '>Gölge Model B Yönü<',
    '>Shadow B Score / Confidence<': '>Gölge Model B Skoru / Güveni<',
    '>Microstructure Score<': '>Piyasa Yapısı Skoru<',
    '>±$10 Bid / Ask / Imb.<': '>±$10 Alış / Satış / Dengesizlik<',
    '>±$25 Bid / Ask / Imb.<': '>±$25 Alış / Satış / Dengesizlik<',
    '>±$50 Bid / Ask / Imb.<': '>±$50 Alış / Satış / Dengesizlik<',
    '>±$75 Bid / Ask / Imb.<': '>±$75 Alış / Satış / Dengesizlik<',
    '>Agg Trade Flow 5s<': '>Gerçekleşen Agresif Alış/Satış Akışı · 5 sn<',
    '>Agg Trade Flow 15s<': '>Gerçekleşen Agresif Alış/Satış Akışı · 15 sn<',
    '>Agg Trade Flow 30s<': '>Gerçekleşen Agresif Alış/Satış Akışı · 30 sn<',
    '>Agg Trade Flow 60s<': '>Gerçekleşen Agresif Alış/Satış Akışı · 60 sn<',
    '>Deep / Trade Scores<': '>Derinlik / Gerçek İşlem Skorları<',
    '>Walls / Depletion<': '>Emir Duvarları / Erime<',
    '>PTB Path / Barrier<': '>Hedefe Giden Likidite / Bariyer<',
    '>Entry Economic Edge<': '>Giriş Ekonomik Avantajı<',
    'Paper Entry Gate Monitor —': 'Kağıt İşlem Giriş Kontrolü —',
    'Hedge Gate Monitor —': 'Koruma İşlemi Kontrolü —',
    '<h2>BTC 5m vs 15m — Paper Efficiency Experiment</h2>': '<h2>BTC 5 dk ve 15 dk — Kağıt İşlem Verimlilik Karşılaştırması</h2>',
    '>5m: N / Win Rate<': '>5 dk: İşlem Sayısı / Kazanma Oranı<',
    '>5m: Return on Stake<': '>5 dk: Yatırılan Tutar Getirisi<',
    '>5m: Avg Return ± SE<': '>5 dk: Ortalama Getiri ± Standart Hata<',
    '>15m: N / Win Rate<': '>15 dk: İşlem Sayısı / Kazanma Oranı<',
    '>15m: Return on Stake<': '>15 dk: Yatırılan Tutar Getirisi<',
    '>15m: Avg Return ± SE<': '>15 dk: Ortalama Getiri ± Standart Hata<',
    '>5m Brier / Cal Gap<': '>5 dk: Brier Hata Skoru / Kalibrasyon Farkı<',
    '>15m Brier / Cal Gap<': '>15 dk: Brier Hata Skoru / Kalibrasyon Farkı<',
    '>Inference<': '>İstatistiksel Sonuç<',
    '>Collecting...<': '>Veri toplanıyor...<',
    '<h2>Paper Portfolio — Original Strategy A</h2>': '<h2>Kağıt İşlem Portföyü — Asıl Strateji A</h2>',
    '>Cash Balance<': '>Nakit Bakiye<',
    '>Realized PnL<': '>Gerçekleşen Kâr/Zarar<',
    '>Win Rate<': '>Kazanma Oranı<',
    '>Trades / Open<': '>İşlem / Açık İşlem<',
    '>Open Stake<': '>Açık İşlemlerdeki Tutar<',
    '>Avg Predicted Win P<': '>Ortalama Tahmini Kazanma Olasılığı<',
    '>Actual Win P<': '>Gerçekleşen Kazanma Oranı<',
    '>Calibration Gap<': '>Kalibrasyon Farkı (0’a yakın iyi)<',
    '>Brier Score / N<': '>Brier Hata Skoru (düşük iyi) / Örnek<',
    '<h2>Shadow Hedge Portfolio B — Persistent Reverse Regime</h2>': '<h2>Gölge Koruma Portföyü B — Kalıcı Ters Yön Rejimi</h2>',
    '>Hedges / Open<': '>Koruma İşlemi / Açık<',
    '>Original PnL on Hedged<': '>Korunan İşlemlerin Asıl Kâr/Zararı<',
    '>Hedge Contribution<': '>Koruma İşlemi Katkısı<',
    '>Combined PnL<': '>Birleşik Kâr/Zarar<',
    '>Avg Edge / Persistence<': '>Ort. Avantaj / Kalıcılık<',
    'Henüz persistent reverse hedge yok.': 'Henüz kalıcı ters yön koruma işlemi yok.',
    '<h2>Paper Trades</h2>': '<h2>Kağıt İşlemler</h2>',
    'Henüz paper trade yok.': 'Henüz kağıt işlem yok.',
    '<h2>Recent Signals (Last 20)</h2>': '<h2>Son Sinyaller (Son 20)</h2>',
    'Waiting for verified signal stream...': 'Doğrulanmış sinyal verisi bekleniyor...',
    '<h2>Technical Indicator Scores</h2>': '<h2>Teknik Gösterge Skorları</h2>',
    'No metrics generated': 'Henüz gösterge üretilmedi',
    '>CONNECTING<': '>BAĞLANIYOR<',
    '>Waiting...<': '>Bekleniyor...<',
    '>WAITING<': '>BEKLENİYOR<',
    '>WAIT<': '>BEKLE<',
}
for old, new in R.items():
    s = s.replace(old, new)

# Table headers: translate individually so generic Durum replacements cannot break them.
TH = {
    '<th>Time</th>':'<th>Saat</th>', '<th>Market</th>':'<th>Piyasa</th>', '<th>A</th>':'<th>Asıl Yön</th>',
    '<th>Hedge</th>':'<th>Koruma Yönü</th>', '<th>Px</th>':'<th>Fiyat</th>', '<th>Shares</th>':'<th>Pay</th>',
    '<th>P(reverse)</th>':'<th>Ters Yön Olasılığı</th>', '<th>Edge</th>':'<th>Avantaj</th>', '<th>Persist.</th>':'<th>Kalıcılık</th>',
    '<th>EWMA</th>':'<th>Yumuşatılmış Skor</th>', '<th>PTB Z</th>':'<th>Hedef Uzaklığı</th>', '<th>Locked PnL</th>':'<th>Kilitli K/Z</th>',
    '<th>Status</th>':'<th>Durum</th>', '<th>Combined</th>':'<th>Birleşik K/Z</th>', '<th>Entry</th>':'<th>Giriş Saati</th>',
    '<th>Side</th>':'<th>Yön</th>', '<th>Entry Px</th>':'<th>Giriş Fiyatı</th>', '<th>Cost</th>':'<th>Maliyet</th>',
    '<th>Conf.</th>':'<th>Güven</th>', '<th>Outcome</th>':'<th>Sonuç</th>', '<th>PnL</th>':'<th>Kâr/Zarar</th>',
    '<th>Current</th>':'<th>Güncel Fiyat</th>', '<th>Target</th>':'<th>Hedef Fiyat</th>', '<th>P_up</th>':'<th>P(Yukarı)</th>',
    '<th>P_down</th>':'<th>P(Aşağı)</th>', '<th>OrderFlow</th>':'<th>Emir Baskısı</th>', '<th>Bias</th>':'<th>Yön</th>',
    '<th>Confidence</th>':'<th>Güven</th>'
}
for old, new in TH.items():
    s = s.replace(old, new)

# Glossary for the terms that are still shown as standard finance/quant acronyms.
marker = "  <div class=\"tfbar\"><button class=\"tfbtn active\" data-tf=\"5m\" onclick=\"switchTf('5m')\">BTC 5 DAKİKA</button><button class=\"tfbtn\" data-tf=\"15m\" onclick=\"switchTf('15m')\">BTC 15 DAKİKA</button></div>\n"
if marker not in s:
    raise SystemExit('timeframe marker not found')
glossary = marker + '''  <div class="card" style="padding:14px 18px">
    <h2 style="margin-bottom:10px">Bu Terimler Ne Anlama Geliyor?</h2>
    <div class="grid3">
      <div class="mini"><span>Hedef Fiyat (PTB)</span><strong style="font-size:13px">Piyasa başında sabitlenen Chainlink fiyatıdır. Kapanış bunun üstündeyse YUKARI, altındaysa AŞAĞI sonucu oluşur.</strong></div>
      <div class="mini"><span>Hedef Uzaklığı (Z/σ)</span><strong style="font-size:13px">Fiyatın hedefe, beklenen oynaklığa göre kaç standart sapma uzakta olduğunu gösterir. Mutlak değer büyüdükçe mesafe artar.</strong></div>
      <div class="mini"><span>Brier Hata Skoru</span><strong style="font-size:13px">Olasılık tahminlerinin doğruluğunu ölçer. 0 en iyi değerdir; düşük olması daha iyidir.</strong></div>
      <div class="mini"><span>Kalibrasyon Farkı</span><strong style="font-size:13px">Tahmin edilen kazanma olasılığı ile gerçek kazanma oranı arasındaki farktır. 0’a yakın olması daha iyidir.</strong></div>
      <div class="mini"><span>Ekonomik Avantaj</span><strong style="font-size:13px">Modelin kazanma olasılığından gerçek giriş maliyetinin gerektirdiği başa baş olasılığı çıkarılır. Pozitif olması daha sağlıklıdır.</strong></div>
      <div class="mini"><span>Gölge Model B</span><strong style="font-size:13px">Yeni ±$10/$25/$50/$75 derinlik ve gerçek alış/satış akışını ölçen test modelidir. Şimdilik kağıt işlemi açmaz.</strong></div>
      <div class="mini"><span>VWAP</span><strong style="font-size:13px">Emrin birden fazla fiyat kademesinde gerçekleşmesi halinde oluşan hacim ağırlıklı ortalama gerçekleşme fiyatıdır.</strong></div>
      <div class="mini"><span>EWMA</span><strong style="font-size:13px">Yeni sinyallere daha fazla ağırlık veren yumuşatılmış ters yön skorudur.</strong></div>
      <div class="mini"><span>Baz Puan</span><strong style="font-size:13px">Fiyat hareketini küçük ölçekte gösterir. 1 baz puan = %0,01.</strong></div>
    </div>
  </div>
'''
s = s.replace(marker, glossary, 1)

# Keep existing CI smoke grep valid without showing English text to the user.
s = s.replace('<div class="card">\n    <h2>Binance Derin Piyasa Yapısı — Gölge Model B</h2>', '<!-- CI smoke marker: Binance Deep Microstructure -->\n  <div class="card">\n    <h2>Binance Derin Piyasa Yapısı — Gölge Model B</h2>', 1)

# JS helpers: translate backend enums/reason codes only at presentation time.
s = s.replace("const bps=(n,d=2)=>`${Number(n||0).toFixed(d)} bps`;", "const bps=(n,d=2)=>`${Number(n||0).toFixed(d)} baz puan`;")
s = s.replace("function decisionChip(d){return d==='UP'?chip('UP','up'):d==='DOWN'?chip('DOWN','down'):chip(d||'NEUTRAL','neutral')}", "function directionText(d){return d==='UP'?'YUKARI':d==='DOWN'?'AŞAĞI':d==='NEUTRAL'?'NÖTR':d==='WAITING'?'BEKLENİYOR':d||'NÖTR'}\nfunction decisionChip(d){return d==='UP'?chip('YUKARI','up'):d==='DOWN'?chip('AŞAĞI','down'):chip(directionText(d),'neutral')}")
s = s.replace("function setConnection(ok){document.getElementById('connection').innerHTML=ok?chip('LIVE','fresh'):chip('API ERROR','down')}", "function setConnection(ok){document.getElementById('connection').innerHTML=ok?chip('CANLI','fresh'):chip('API HATASI','down')}")
s = s.replace("document.getElementById('decision').innerHTML=chip('WAITING','warn');", "document.getElementById('decision').innerHTML=chip('BEKLENİYOR','warn');")
s = s.replace("document.getElementById('depthFresh').innerHTML=chip('WAIT','neutral');", "document.getElementById('depthFresh').innerHTML=chip('BEKLE','neutral');")
s = s.replace("document.getElementById('currentPrice').textContent='Waiting...';", "document.getElementById('currentPrice').textContent='Bekleniyor...';")
s = s.replace("document.getElementById('priceToBeat').textContent='Waiting...';", "document.getElementById('priceToBeat').textContent='Bekleniyor...';")
s = s.replace("document.getElementById('timeRemaining').textContent='Waiting...';", "document.getElementById('timeRemaining').textContent='Bekleniyor...';")
s = s.replace("`${Math.floor(s/60)}m ${s%60}s`", "`${Math.floor(s/60)} dk ${s%60} sn`")
s = s.replace("`Bids: ${Number(data.bidVol||0).toFixed(3)} | Asks: ${Number(data.askVol||0).toFixed(3)}`", "`Alış: ${Number(data.bidVol||0).toFixed(3)} | Satış: ${Number(data.askVol||0).toFixed(3)}`")
s = s.replace("`${usd(data.spreadUsd)} · ${Number(data.spreadBps||0).toFixed(3)} bps`", "`${usd(data.spreadUsd)} · ${Number(data.spreadBps||0).toFixed(3)} baz puan`")
s = s.replace("`${usd(data.bidRangeUsd)} · ${Number(data.bidRangeBps||0).toFixed(2)} bps`", "`${usd(data.bidRangeUsd)} · ${Number(data.bidRangeBps||0).toFixed(2)} baz puan`")
s = s.replace("`${usd(data.askRangeUsd)} · ${Number(data.askRangeBps||0).toFixed(2)} bps`", "`${usd(data.askRangeUsd)} · ${Number(data.askRangeBps||0).toFixed(2)} baz puan`")

insert_before = "async function updateLive(){"
helpers = r'''function sourceTr(v){const m={
'BINANCE_REST_DEPTH20':'Binance REST · İlk 20 kademe','BINANCE_WS_DEPTH20':'Binance WebSocket · İlk 20 kademe',
'BINANCE_DEEP_REST1000':'Binance REST · Derin 1000 kademe','BINANCE_DEEP_DIFF':'Binance WebSocket · Derin fark akışı',
'BINANCE_DEEP_SNAPSHOT':'Binance derinlik başlangıç görüntüsü','DEEP_WS_CONNECT_FAILED':'Derin veri bağlantısı kurulamadı',
'DEEP_SNAPSHOT_FAILED':'Derinlik görüntüsü alınamadı','DEEP_WS_DISCONNECTED':'Derin veri bağlantısı koptu','UNINITIALIZED':'Henüz başlatılmadı'};return m[v]||v||'Bilinmiyor'}
function indicatorTr(v){const m={ADX:'Trend Gücü (ADX)',CCI:'Emtia Kanal Endeksi (CCI)',EMA:'Üssel Hareketli Ortalama (EMA)',HMA:'Hull Hareketli Ortalama (HMA)',MACD:'Hareketli Ortalama Yakınsama/Farkı (MACD)',Momentum:'Momentum',RSI:'Göreceli Güç Endeksi (RSI)',SMA:'Basit Hareketli Ortalama (SMA)',Stochastic:'Stokastik Osilatör',VWMA:'Hacim Ağırlıklı Hareketli Ortalama (VWMA)',WilliamsR:'Williams %R'};return m[v]||v}
function reasonTr(v){const m={
'NEUTRAL_SIGNAL':'Yön sinyali yeterince güçlü değil','CONFIDENCE_BELOW_THRESHOLD':'Güven skoru eşik altında','OUTSIDE_ENTRY_WINDOW':'Giriş zaman aralığı dışında',
'DATA_NOT_FRESH_OR_MARKET_INACTIVE':'Veri güncel değil veya piyasa aktif değil','INSUFFICIENT_PAPER_BALANCE':'Kağıt işlem bakiyesi yetersiz','MIN_ORDER_SIZE_NOT_MET':'Minimum emir koşulu sağlanmadı',
'CLOB_QUOTE_ERROR':'CLOB fiyat teklifi alınamadı','POSITION_ALREADY_RECORDED':'Bu piyasa için işlem zaten kaydedildi','NO_OPEN_POSITION':'Korunacak açık işlem yok',
'ALREADY_HEDGED':'Bu işlem zaten korunmuş','OUTSIDE_HEDGE_WINDOW':'Koruma zaman aralığı dışında','REVERSE_DECISION_NOT_CONFIRMED':'Ters yön yeterince doğrulanmadı',
'REGIME_NOT_READY':'Ters yön örnek penceresi henüz dolmadı','PERSISTENCE_BELOW_THRESHOLD':'Ters yön kalıcılığı yetersiz','CONSECUTIVE_BELOW_THRESHOLD':'Ardışık ters sinyal sayısı yetersiz',
'PROBABILITY_BELOW_THRESHOLD':'Ters yön olasılığı eşik altında','PTB_Z_NOT_CONFIRMED':'Hedef fiyat uzaklığı ters yönü doğrulamıyor','SCORE_BELOW_THRESHOLD':'Yumuşatılmış ters yön skoru yetersiz',
'EDGE_BELOW_THRESHOLD':'Ekonomik avantaj eşik altında','EXPECTED_IMPROVEMENT_NOT_POSITIVE':'Koruma işlemi beklenen sonucu iyileştirmiyor','WAITING_FOR_DATA':'Veri bekleniyor',
'BLOCKED':'ENGELLENDİ','READY':'HAZIR'};return m[v]||String(v||'ENGELLENDİ').replaceAll('_',' ')}
function comparisonStatusTr(v){const m={collecting:'Veri toplanıyor',insufficient_samples:'Örnek sayısı yetersiz',no_significant_difference:'Anlamlı fark yok',significant_difference:'İstatistiksel fark var',leader:'Önde olan zaman dilimi'};return m[String(v||'').toLowerCase()]||v||'Veri toplanıyor'}
'''
if insert_before not in s:
    raise SystemExit('updateLive marker missing')
s = s.replace(insert_before, helpers + insert_before, 1)

s = s.replace("document.getElementById('depthSource').textContent=data.depthSource||'UNKNOWN';", "document.getElementById('depthSource').textContent=sourceTr(data.depthSource);")
s = s.replace("data.depthFresh?chip(`FRESH · ${age>=0?age+' ms':'—'}`,'fresh'):chip('STALE','stale')", "data.depthFresh?chip(`GÜNCEL · ${age>=0?age+' ms':'—'}`,'fresh'):chip('ESKİ VERİ','stale')")
s = s.replace("dm.ready?chip(`SYNC · ${dm.bidLevels||0}/${dm.askLevels||0} · ${dm.ageMs||0}ms`,'fresh'):chip(`${dm.source||'WAITING'} · ${dm.ageMs??'—'}ms`,'warn')", "dm.ready?chip(`SENKRON · ${dm.bidLevels||0}/${dm.askLevels||0} · ${dm.ageMs||0}ms`,'fresh'):chip(`${sourceTr(dm.source)} · ${dm.ageMs??'—'}ms`,'warn')")
s = s.replace("`B ${pct(dm.bidWallScore||0)} / A ${pct(dm.askWallScore||0)}`", "`Alış duvarı ${pct(dm.bidWallScore||0)} / Satış duvarı ${pct(dm.askWallScore||0)}`")
s = s.replace("`B ${usdCompact(dm.ptbPathBidUsd||0)} / A ${usdCompact(dm.ptbPathAskUsd||0)} · ${pct(data.ptbBarrierScore||0)}`", "`Alış desteği ${usdCompact(dm.ptbPathBidUsd||0)} / Satış bariyeri ${usdCompact(dm.ptbPathAskUsd||0)} · ${pct(data.ptbBarrierScore||0)}`")
s = s.replace("<span>${name}</span>", "<span>${indicatorTr(name)}</span>")
s = s.replace("'<div class=\"indrow\"><span>No metrics generated</span><span class=\"chip neutral\">0</span></div>'", "'<div class=\"indrow\"><span>Henüz gösterge üretilmedi</span><span class=\"chip neutral\">0</span></div>'")

# Paper and hedge dynamic labels.
s = s.replace("t.status==='OPEN'?chip('OPEN','open'):t.won?chip('WIN','win'):chip('LOSS','loss')", "t.status==='OPEN'?chip('AÇIK','open'):t.won?chip('KAZANDI','win'):chip('KAYBETTİ','loss')")
s = s.replace("${t.outcome||'—'}", "${t.outcome?directionText(t.outcome):'—'}")
s = s.replace("h.status==='OPEN'?chip('OPEN','open'):chip('SETTLED','neutral')", "h.status==='OPEN'?chip('AÇIK','open'):chip('SONUÇLANDI','neutral')")
s = s.replace("function boolChip(v){return v?chip('PASS','fresh'):chip('BLOCK','down')}", "function boolChip(v){return v?chip('GEÇTİ','fresh'):chip('ENGEL','down')}")

# Entry gate.
s = s.replace("gateRow('ENTRY',e.allowed?chip('READY','fresh'):chip(e.reason||'BLOCKED','warn'))", "gateRow('GİRİŞ',e.allowed?chip('HAZIR','fresh'):chip(reasonTr(e.reason),'warn'))")
s = s.replace("gateRow('Direction'", "gateRow('Yön'")
s = s.replace("`${e.decision||'—'} ${boolChip(e.directionPass)}`", "`${e.decision?directionText(e.decision):'—'} ${boolChip(e.directionPass)}`")
s = s.replace("gateRow('Confidence'", "gateRow('Güven skoru'")
s = s.replace("gateRow('Time'", "gateRow('Kalan süre'")
s = s.replace("gateRow('Fresh market/data'", "gateRow('Piyasa ve veri güncel mi?'")
s = s.replace("gateRow('Paper balance'", "gateRow('Kağıt işlem bakiyesi'")
s = s.replace("` / stake ${usd(e.stake)}", "` / işlem tutarı ${usd(e.stake)}")
s = s.replace("gateRow('CLOB ask / VWAP'", "gateRow('CLOB satış fiyatı / ortalama gerçekleşme (VWAP)'")
s = s.replace("gateRow('Shares / market BUY'", "gateRow('Pay adedi / piyasa alış emri'")
s = s.replace("gateRow('Economic Edge (shadow)'", "gateRow('Ekonomik avantaj (gölge test)'")
s = s.replace("chip('POSITIVE','fresh'):chip('NEGATIVE','down')", "chip('AVANTAJLI','fresh'):chip('AVANTAJ YOK','down')")

# Hedge gate.
s = s.replace("gateRow('HEDGE',h.allowed?chip('READY','fresh'):chip(h.reason||'BLOCKED','warn'))", "gateRow('KORUMA',h.allowed?chip('HAZIR','fresh'):chip(reasonTr(h.reason),'warn'))")
s = s.replace("gateRow('Open A position'", "gateRow('Açık A pozisyonu var mı?'")
s = s.replace("gateRow('Original → Reverse'", "gateRow('Asıl yön → Ters yön'")
s = s.replace("`${h.originalSide||'—'} → ${h.reverseSide||'—'} ${boolChip(h.decisionPass)}`", "`${h.originalSide?directionText(h.originalSide):'—'} → ${h.reverseSide?directionText(h.reverseSide):'—'} ${boolChip(h.decisionPass)}`")
s = s.replace("gateRow('Reverse votes'", "gateRow('Ters yön oyları'")
s = s.replace("` · min ${h.minVotes||0}`", "` · en az ${h.minVotes||0}`")
s = s.replace("gateRow('Consecutive'", "gateRow('Ardışık ters sinyal'")
s = s.replace("gateRow('P(reverse)'", "gateRow('Ters yön olasılığı'")
s = s.replace("` / min ${pct(h.minProbability||0)}", "` / en az ${pct(h.minProbability||0)}")
s = s.replace("gateRow('EWMA score'", "gateRow('Yumuşatılmış ters yön skoru (EWMA)'")
s = s.replace("gateRow('PTB Z'", "gateRow('Hedef fiyata uzaklık (Z/σ)'")
s = s.replace("gateRow('Edge'", "gateRow('Ekonomik avantaj'")
s = s.replace("` / min ${pct(h.minEdge||0)}", "` / en az ${pct(h.minEdge||0)}")
s = s.replace("gateRow('Expected improve'", "gateRow('Beklenen iyileşme'")

# Comparison wording.
s = s.replace("document.getElementById('cmpInference').textContent=`${c.status||'collecting'} · leader ${c.leader||'none'} · z=${Number(c.zScore||0).toFixed(2)}`;", "document.getElementById('cmpInference').textContent=`${comparisonStatusTr(c.status)} · önde: ${c.leader==='5m'?'5 dk':c.leader==='15m'?'15 dk':'yok'} · z=${Number(c.zScore||0).toFixed(2)}`;")

# Final visible-text safety checks. These are allowed to remain inside JS backend comparisons,
# but not as literal HTML headings/labels.
for needle in [
    '>Real-Time Signals & Prediction<','>Cash Balance<','>Win Rate<','>Paper Trades<',
    '>Recent Signals (Last 20)<','>Technical Indicator Scores<','>Entry Economic Edge<',
    '>Depth Freshness<','>Time Remaining<','>Final Bias<','>Composite Confidence<'
]:
    if needle in s:
        raise SystemExit(f'untranslated visible label remains: {needle}')

p.write_text(s, encoding='utf-8')

# P3 Guarded LIVE v2 — Equal-Share BUY + MERGE

P3 her process başlangıcında **DRY** başlar. LIVE feature `.env.p3` içinde açık olsa bile restart/deploy sonrası otomatik arm edilmez; operatör 8093 panelinden yeniden arm etmelidir.

## Temel execution kuralı

LIVE v2'de `$1 / DRY capital` şeklindeki proportional scaling **yoktur**.

Her fırsatta tek bir `Q` seçilir ve iki bacakta da aynı exact share kullanılır:

```text
Q = min(
  STRICT optimal share,
  LIVE target share,
  LIVE hard-max share,
  fresh UP executable depth,
  fresh DOWN executable depth
)
```

Sonra fresh pair-book snapshot üzerinde bu exact Q için VWAP, V2 fee, net PnL ve ROI yeniden hesaplanır. Şartlar hâlâ pozitifse:

```text
BUY UP   Q share FOK
BUY DOWN Q share FOK
```

İki bacağın share miktarı farklı olamaz. Eski `P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC` yalnız eski `.env` dosyalarının parse edilmesi için tutulur; LIVE v2 sizing girdisi değildir.

İlk production profili:

```dotenv
P3_LIVE_TARGET_QUANTITY_SHARES=5
P3_LIVE_MAX_QUANTITY_SHARES=10
P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC=0
```

Target 5 olduğu sürece sistem normalde **5 UP + 5 DOWN** hedefler; STRICT optimum veya fresh depth daha düşükse Q küçülür. Q marketin minimum order size'ının altına düşerse işlem yapılmaz.

## Tek-bacak riski nasıl sınırlandırılıyor?

CLOB batch atomic kabul edilmez. Dolayısıyla UP dolup DOWN FOK kill olabilir veya tersi olabilir. Bu risk sıfırlanamaz; LIVE v2 amacı exposure'ı **önceden ölçmek, miktarı sınırlamak ve oluşursa hızlı/fail-closed kapatmaktır**.

Emir gönderilmeden önce aynı pair-book snapshot'ta iki ayrı stres testi yapılır:

- yalnız UP dolarsa Q share UP'ın tamamı mevcut bid depth'te satılabiliyor mu?
- yalnız DOWN dolarsa Q share DOWN'ın tamamı mevcut bid depth'te satılabiliyor mu?
- tek bacağın entry notional'ı risk limitini aşıyor mu?
- projected immediate unwind loss limiti aşıyor mu?
- expected arb edge / worst unwind loss oranı yeterli mi?

Varsayılan ilk profil:

```dotenv
P3_LIVE_MAX_SINGLE_LEG_NOTIONAL_USDC=5.25
P3_LIVE_MAX_PROJECTED_UNWIND_LOSS_USDC=0.25
P3_LIVE_EMERGENCY_UNWIND_LOSS_USDC=0.50
P3_LIVE_MIN_EDGE_TO_UNWIND_LOSS_RATIO=0.10
```

Bu kontrollerden biri kalırsa **iki BUY emri hiç gönderilmez**.

### Tek bacak gerçekten oluşursa

Exposure kapatma zinciri:

```text
ONE/PARTIAL LEG
   ↓
1) pre-submit'te doğrulanmış bid floor'da SELL LIMIT FOK
   ↓ başarısız
2) book yeniden çek → emergency loss cap içinde yeni SELL LIMIT FOK
   ↓ hâlâ exposure
3) MARKET FAK emergency reducer
   ↓
conditional-token balance ile residual doğrulaması
   ├─ flat → ONE_LEG_UNWOUND_VERIFIED
   └─ residual → LIVE_HALTED / operatör incelemesi
```

FAK mevcut likiditeyi anında tüketir ve kalan miktarı resting order olarak bırakmaz. Residual exposure kaldığı doğrulanırsa sistem yeni işlem açmaz.

Varsayılan ayrıca:

```dotenv
P3_LIVE_HALT_AFTER_ONE_LEG=true
```

Yani tek bacak başarıyla kapatılsa dahi sistem **LIVE_HALTED** olur; operatör olayı görmeden ikinci gerçek trade'e geçmez.

## Realized LIVE PnL ve loss budget

`p3_live_ledger` her gerçek cycle için şunları kaydeder:

- exact Q,
- planned capital / edge / ROI,
- projected worst single-leg unwind loss,
- collateral before / after,
- realized PnL / ROI,
- one-leg event,
- unwind attempt sayısı,
- outcome.

Realized PnL, bot cüzdanındaki collateral delta üzerinden ölçülür. İlk kontrollü test için bu wallet üzerinde eşzamanlı manuel işlem yapılmaması gerekir; aksi halde collateral delta yalnız bot trade'ini temsil etmeyebilir.

Rolling 24 saat **gross loss** kill-switch:

```dotenv
P3_LIVE_ROLLING_24H_GROSS_LOSS_LIMIT_USDC=2.00
```

Kârlı trade'ler bu zarar sayacını sıfırlamaz. Son 24 saatte toplam gerçekleşmiş zarar $2'ye ulaşırsa yeni network order'ından önce LIVE fail-closed halt edilir.

## Diğer güvenlik sınırları

- Analitik ve operator paneli tek port: `8093`.
- LIVE feature açıkken `P3_WEB_AUTH_REQUIRED=true` zorunlu.
- Session cookie `HttpOnly` + `SameSite=Strict`; LIVE POST endpoint'leri ayrıca CSRF ister.
- `/health` yalnız minimal local smoke bilgisidir.
- Geoblock clear değilse arm yok.
- STRICT `DRY_VALIDATED` default arm gate'tir.
- Trading secret'ları DB/dashboard/log'a yazılmaz.
- Aynı LIVE session/window DB claim nedeniyle iki kez submit edilemez.
- Gerçek fill kararı order response'a değil conditional-token balance delta'ya dayanır.
- BOTH → CTF merge + verification.
- Merge/unwind/residual belirsizliği → fail-closed halt.

> Parola erişim kontrolüdür; düz HTTP trafiğini şifrelemez. Public 8093 kullanılıyorsa HTTPS/TLS reverse proxy önerilir ve `P3_WEB_COOKIE_SECURE=true` yapılmalıdır.

## `.env.p3` LIVE v2 örneği

```dotenv
P3_WEB_ENABLED=true
P3_WEB_HOST=127.0.0.1
P3_WEB_PORT=8093
P3_WEB_AUTH_REQUIRED=true
P3_WEB_USERNAME=operator
P3_WEB_PASSWORD=EN_AZ_12_KARAKTER_GUCLU_PAROLA
P3_WEB_COOKIE_SECURE=false

P3_LIVE_FEATURE_ENABLED=true
P3_LIVE_AUTO_EXECUTE_ENABLED=true
P3_LIVE_REQUIRE_DRY_VALIDATED=true
P3_LIVE_BUY_MERGE_ONLY=true

P3_LIVE_TARGET_QUANTITY_SHARES=5
P3_LIVE_MAX_QUANTITY_SHARES=10
P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC=0
P3_LIVE_MIN_NET_PROFIT_USDC=0.01
P3_LIVE_MIN_NET_ROI=0.0025

P3_LIVE_MIN_COLLATERAL_TO_ARM_USDC=5.00
P3_LIVE_MAX_SINGLE_LEG_NOTIONAL_USDC=5.25
P3_LIVE_MAX_PROJECTED_UNWIND_LOSS_USDC=0.25
P3_LIVE_EMERGENCY_UNWIND_LOSS_USDC=0.50
P3_LIVE_MIN_EDGE_TO_UNWIND_LOSS_RATIO=0.10
P3_LIVE_EMERGENCY_FAK_ENABLED=true
P3_LIVE_HALT_AFTER_ONE_LEG=true
P3_LIVE_ROLLING_24H_GROSS_LOSS_LIMIT_USDC=2.00

P3_LIVE_CLOB_HOST=https://clob.polymarket.com
P3_LIVE_CHAIN_ID=137
P3_LIVE_REQUIRE_GEOBLOCK_CLEAR=true

POLYMARKET_PRIVATE_KEY=
POLYMARKET_WALLET=0x_DEPOSIT_WALLET
POLYMARKET_FUNDER=0x_DEPOSIT_WALLET
POLYMARKET_SIGNATURE_TYPE=3
POLYMARKET_CLOB_API_KEY=
POLYMARKET_CLOB_API_SECRET=
POLYMARKET_CLOB_API_PASSPHRASE=
```

Signature type 3, Polymarket Deposit Wallet / `POLY_1271` akışıdır. Type 1/2/3 seçiliyken funder bulunamazsa preflight CLOB çağrısından önce fail-closed olur.

## İlk kullanım

Deploy:

```bash
cd ~/direction-engine
bash deploy_p3.sh
```

Servis her durumda DRY başlar. 8093 login sonrası:

1. **BAĞLANTI / KİMLİK TESTİ (EMİR YOK)**
2. Preflight ve collateral durumunu kontrol et.
3. STRICT readiness tamamlandıysa **CANLIYA GEÇ**.
4. İlk gerçek cycle'larda target 5 share'i değiştirme.
5. `bash scripts/status_p3.sh` ile realized PnL / one-leg / rolling 24h gross loss'u izle.

0 collateral ile connectivity probe geçebilir; full LIVE arm `INSUFFICIENT_COLLATERAL` ile reddedilir.

# P3 Guarded LIVE — BUY + MERGE v1

P3 her process başlangıcında **DRY** başlar. LIVE yeteneği `.env.p3` ile önceden izin verilmiş olsa bile restart/deploy sonrasında otomatik arm edilmez.

## Güvenlik sınırı

- Ana P3 paneli `8093`: salt okunur durum/analitik. LIVE arm/disarm endpoint'i içermez.
- Operatör paneli `8094`: yalnız `127.0.0.1` üzerinde dinler.
- `8094` EC2 Security Group, reverse proxy veya public bind ile internete açılmamalıdır.
- LIVE v1 yalnız `ARB_COMPLETE_SET_BUY_MERGE_V1` uygular.
- İki bacak exact-share **FOK limit** olarak aynı CLOB batch request içinde gönderilir; batch atomic kabul edilmez.
- Sonuç CLOB cevabından değil conditional-token bakiye farkından doğrulanır.
- İki bacak doğrulanırsa complete set CTF `merge_positions` ile USDC collateral'a çevrilir ve merge sonucu doğrulanır.
- Tek/partial bacak doğrulanırsa FOK SELL unwind yapılır. Unwind doğrulanamazsa sistem `LIVE_HALTED` olur.
- Geoblock kontrolü clear değilse LIVE arm fail-closed olur.
- Secret'lar DB'ye veya public dashboard'a yazılmaz.
- Her process restart/deploy LIVE state'i unutur ve yeniden DRY başlar.

## `.env.p3`

Varsayılan güvenli ayarlar:

```dotenv
P3_LIVE_FEATURE_ENABLED=false
P3_LIVE_AUTO_EXECUTE_ENABLED=false
P3_LIVE_REQUIRE_DRY_VALIDATED=true
P3_LIVE_BUY_MERGE_ONLY=true
P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC=1.00
P3_LIVE_MAX_QUANTITY_SHARES=10
P3_LIVE_MIN_NET_PROFIT_USDC=0.01
P3_LIVE_MIN_NET_ROI=0.0025
P3_LIVE_REQUIRE_GEOBLOCK_CLEAR=true
P3_LIVE_CONTROL_ENABLED=true
P3_LIVE_CONTROL_HOST=127.0.0.1
P3_LIVE_CONTROL_PORT=8094
```

LIVE altyapısını kurup yine DRY başlatmak için:

```dotenv
P3_LIVE_FEATURE_ENABLED=true
P3_LIVE_AUTO_EXECUTE_ENABLED=true
```

Secrets yalnız VPS'teki `.env.p3` içine girilir; gerçek değerler git'e commit edilmez:

```dotenv
POLYMARKET_PRIVATE_KEY=
POLYMARKET_WALLET=
POLYMARKET_FUNDER=
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_CLOB_API_KEY=
POLYMARKET_CLOB_API_SECRET=
POLYMARKET_CLOB_API_PASSPHRASE=
```

CLOB L2 API bilgileri boşsa client private key ile credential derive/create yolunu kullanabilir.

## DRY doğrulama kapısı

Varsayılan:

```dotenv
P3_LIVE_REQUIRE_DRY_VALIDATED=true
```

Bu durumda STRICT readiness `DRY_VALIDATED` olmadan `CANLIYA GEÇ` butonu arm etmez. Bu koruma kasıtlıdır.

Sadece kontrollü altyapı testi için operatör bilinçli olarak:

```dotenv
P3_LIVE_REQUIRE_DRY_VALIDATED=false
```

seçebilir. Geoblock, credential, collateral, allowance, live caps ve fresh-depth kontrolleri yine devrededir.

## Deploy

```bash
cd ~/direction-engine
bash deploy_p3.sh
```

`P3_LIVE_FEATURE_ENABLED=true` olduğunda deploy `requirements-live.txt` paketlerini de kurar. Smoke test mutlaka `mode=DRY`, `execution=false`, `orders=false` ile başlar.

## Yerel kontrol paneli

Kendi bilgisayarından SSH tunnel:

```bash
ssh -L 8094:127.0.0.1:8094 ubuntu@<VPS_PUBLIC_IP>
```

Ardından tarayıcı:

```text
http://127.0.0.1:8094
```

Butonlar:

1. **BAĞLANTI / KİMLİK TESTİ (EMİR YOK)** — geoblock, credential, CLOB auth, balance ve DRY durumunu okur; emir göndermez.
2. **CANLIYA GEÇ** — full preflight geçerse process-local `LIVE_ARMED` yapar.
3. **DRY'A DÖN** — yeni emirleri anında kapatır.

Cüzdan bakiyesi `0 USDC` ise bağlantı/kimlik testi başarılı olabilir; **CANLIYA GEÇ** `INSUFFICIENT_COLLATERAL` ile reddedilir ve sistem DRY kalır. Bilerek unfunded order gönderilmez.

## İlk LIVE emir akışı

```text
STRICT confirmation
  -> fresh CLOB depth revalidation
  -> live edge/capital/quantity gate
  -> collateral check
  -> DB PRE_SUBMIT_CLAIMED
  -> UP + DOWN exact-share FOK batch
  -> conditional balance verification
       BOTH -> CTF merge -> merge verification -> MERGED_VERIFIED
       NONE -> NO_FILL_VERIFIED
       ONE/PARTIAL -> FOK unwind -> verify
            fail -> LIVE_HALTED
```

LIVE durumunu kontrol etmek için:

```bash
cd ~/direction-engine
bash scripts/status_p3.sh
```

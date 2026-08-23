# P3 Guarded LIVE — BUY + MERGE v1

P3 her process başlangıcında **DRY** başlar. LIVE yeteneği `.env.p3` ile önceden izin verilmiş olsa bile restart/deploy sonrasında otomatik arm edilmez.

## Güvenlik sınırı

- P3 analitik ve operatör paneli tek portta: `8093`.
- LIVE feature açıldığında `P3_WEB_AUTH_REQUIRED=true` zorunludur; aksi config validation servisi başlatmaz.
- Dashboard/API verileri login arkasındadır. Yalnız `/health` local systemd/smoke için minimal anonim yanıt verir.
- Login process-local session cookie üretir (`HttpOnly`, `SameSite=Strict`); LIVE POST endpoint'leri ayrıca CSRF token ister.
- Başarısız login denemeleri remote IP bazında rate-limit edilir.
- Parola `SecretStr` olarak tutulur; API/dashboard/log çıktısına yazılmaz.
- Restart tüm web session'larını unutur ve LIVE state'i yeniden DRY başlatır.
- LIVE v1 yalnız `ARB_COMPLETE_SET_BUY_MERGE_V1` uygular.
- İki bacak exact-share **FOK limit** olarak aynı CLOB batch request içinde gönderilir; batch atomic kabul edilmez.
- Sonuç CLOB cevabından değil conditional-token bakiye farkından doğrulanır.
- İki bacak doğrulanırsa complete set CTF `merge_positions` ile USDC collateral'a çevrilir ve merge sonucu doğrulanır.
- Tek/partial bacak doğrulanırsa FOK SELL unwind yapılır. Unwind doğrulanamazsa sistem `LIVE_HALTED` olur.
- Geoblock kontrolü clear değilse LIVE arm fail-closed olur.
- Trading secret'ları DB'ye veya dashboard'a yazılmaz.

> **Transport notu:** Parola koruması erişim kontrolüdür; düz HTTP trafiğini şifrelemez. 8093 internete açılacaksa HTTPS/TLS reverse proxy kullan. HTTPS arkasında `P3_WEB_COOKIE_SECURE=true` yap. Alternatif olarak 8093'ü loopback'te tutup SSH tunnel kullan.

## `.env.p3`

DRY için güvenli varsayılanlar:

```dotenv
P3_WEB_ENABLED=true
P3_WEB_HOST=127.0.0.1
P3_WEB_PORT=8093
P3_WEB_AUTH_REQUIRED=false
P3_WEB_USERNAME=operator
P3_WEB_PASSWORD=
P3_WEB_COOKIE_SECURE=false

P3_LIVE_FEATURE_ENABLED=false
P3_LIVE_AUTO_EXECUTE_ENABLED=false
P3_LIVE_REQUIRE_DRY_VALIDATED=true
P3_LIVE_BUY_MERGE_ONLY=true
P3_LIVE_MAX_CAPITAL_PER_CYCLE_USDC=1.00
P3_LIVE_MAX_QUANTITY_SHARES=10
P3_LIVE_MIN_NET_PROFIT_USDC=0.01
P3_LIVE_MIN_NET_ROI=0.0025
P3_LIVE_REQUIRE_GEOBLOCK_CLEAR=true
```

8093'ü login arkasına alıp LIVE altyapısını kurmak için en az:

```dotenv
P3_WEB_AUTH_REQUIRED=true
P3_WEB_USERNAME=operator
P3_WEB_PASSWORD=EN_AZ_12_KARAKTER_GUCLU_PAROLA

P3_LIVE_FEATURE_ENABLED=true
P3_LIVE_AUTO_EXECUTE_ENABLED=true
P3_LIVE_REQUIRE_DRY_VALIDATED=true
```

Secrets yalnız VPS'teki `.env.p3` içine girilir; gerçek değerler git'e commit edilmez. WhaleSignal'ın varsayılan Polymarket akışı **Deposit Wallet / POLY_1271 / signature type 3**'tür:

```dotenv
POLYMARKET_PRIVATE_KEY=
POLYMARKET_WALLET=0x_DEPOSIT_WALLET
POLYMARKET_FUNDER=0x_DEPOSIT_WALLET
POLYMARKET_SIGNATURE_TYPE=3
POLYMARKET_CLOB_API_KEY=
POLYMARKET_CLOB_API_SECRET=
POLYMARKET_CLOB_API_PASSPHRASE=
```

`POLYMARKET_FUNDER` boş bırakılırsa kod `POLYMARKET_DEPOSIT_WALLET`, ardından `POLYMARKET_WALLET` değerini fallback olarak kullanır. Signature type 1/2/3 seçiliyken funder bulunamazsa preflight fail-closed olur. CLOB L2 API bilgileri boşsa client private key ile credential derive/create yolunu kullanabilir.

## DRY doğrulama kapısı

Varsayılan:

```dotenv
P3_LIVE_REQUIRE_DRY_VALIDATED=true
```

Bu durumda STRICT readiness `DRY_VALIDATED` olmadan **CANLIYA GEÇ** butonu arm etmez. Bu koruma kasıtlıdır; normal canlı kullanımdaki önerilen ayar budur.

## Deploy

```bash
cd ~/direction-engine
bash deploy_p3.sh
```

`P3_LIVE_FEATURE_ENABLED=true` olduğunda deploy `requirements-live.txt` paketlerini de kurar. Smoke test mutlaka `mode=DRY`, `execution=false`, `orders=false` ile başlar. Auth açıksa ayrıca anonim `/api/summary` isteğinin `401` verdiği doğrulanır; smoke scripti parolayı okumaz veya komut satırına yazmaz.

## 8093 operatör paneli

Reverse proxy/TLS yoksa güvenli erişim için SSH tunnel örneği:

```bash
ssh -L 8093:127.0.0.1:8093 ubuntu@<VPS_PUBLIC_IP>
```

Tarayıcı:

```text
http://127.0.0.1:8093
```

Login sonrasında aynı dashboard içinde üç kontrol vardır:

1. **BAĞLANTI / KİMLİK TESTİ (EMİR YOK)** — geoblock, credential, CLOB auth, balance/allowance ve DRY durumunu okur; emir göndermez.
2. **CANLIYA GEÇ** — full preflight geçerse process-local `LIVE_ARMED` yapar.
3. **DRY'A DÖN** — yeni emirleri kapatır.

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
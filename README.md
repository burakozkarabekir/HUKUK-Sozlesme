# Sözleşme Mevzuat Etki Analizi

Doğuş Otomotiv Hukuk birimi için yapay zeka destekli web uygulaması. Turkuaz'daki sözleşme PDF'lerini ve eklerini okur, madde bazında indeksler ve şunları sağlar:

| Özellik | Açıklama |
|---|---|
| ⚖️ **Mevzuat etki analizi** | Mevzuat değişikliği metni veya PDF'i girilir. Değişiklikten etkilenen sözleşmeler ve maddeler listelenir: etki seviyesi, gerekçe, alıntı, güven skoru. Alıntılar kodla doğrulanır. Sonuç Excel olarak indirilir. |
| 💬 **Sözleşme sohbeti (RAG)** | "X firmasıyla olan sözleşmede fesih süresi ne?" gibi sorular sorulur. Her cevap kaynak gösterir (sözleşme no, madde, sayfa). Kaynağa tıklanınca madde metni açılır, PDF ilgili sayfada açılır. |
| 🔎 **Keyword arama** | VE/VEYA, "tam ifade", Türkçe karakter ve büyük/küçük harf duyarsız arama. Eşleşmeler vurgulanır. |
| 📄 **Sözleşmeler** | Filtreli liste, madde listesi, OCR durumu, PDF görüntüleme. |
| ⚙️ **Yönetim** | Senkronizasyon, OCR bekleyenler, günlük token kullanımı, bağımlılık sağlığı, audit kaydı. |

> **Kurulumdan sonra yapmanız gerekenler:** `.env` dosyasına Azure AI Foundry endpoint ve API key'ini, Turkuaz endpoint ve kimlik bilgilerini, kimlik doğrulama ayarlarını girmek. Turkuaz'ın gerçek formatı geldiğinde `app/sources/turkuaz_mapping.py` dosyasını güncellemek ([rehber](#gerçek-turkuaz-endpointi-geldiğinde)).

---

## İçindekiler

1. [Mimari](#mimari)
2. [Yerel geliştirme (Windows + VS Code)](#yerel-geliştirme-windows--vs-code)
3. [Yapılandırma (.env)](#yapılandırma-env)
4. [Sunucuya ilk kurulum](#sunucuya-ilk-kurulum-cyberark-ssh)
5. [Güncelleme](#güncelleme)
6. [Kontrol komutları](#kontrol-komutları)
7. [Gerçek Turkuaz endpoint'i geldiğinde](#gerçek-turkuaz-endpointi-geldiğinde)
8. [Kimlik doğrulama](#kimlik-doğrulama)
9. [API uçları](#api-uçları)
10. [Güvenlik ve uyum](#güvenlik-ve-uyum)
11. [Sorun giderme](#sorun-giderme)
12. [Testler](#testler)
13. [Varsayımlar](#varsayımlar)
14. [Bilinen eksikler ve sonraki adımlar](#bilinen-eksikler-ve-sonraki-adımlar)

---

## Mimari

```
Tarayıcı ──HTTPS──> nginx (/sozlesme/) ──> uvicorn 127.0.0.1:8001 (2 worker, root_path=/sozlesme)
                                              │
                    ┌─────────────────────────┼──────────────────────────────┐
                    │ FastAPI                 │                              │
                    │  • Auth middleware (none/basic/entra, rate limit, CSRF)│
                    │  • API: sohbet (SSE), etki analizi, arama, sözleşmeler │
                    │  • Arka plan iş kuyruğu (analizler, DB tabanlı kilit)  │
                    └───────┬───────────────┬──────────────────┬─────────────┘
                            │               │                  │
                 SQLite + FTS5        Azure AI Foundry     Turkuaz API / yerel klasör
           data/app.db, data/files   (Responses API)     (sözleşme metadata + PDF)
                            ▲
       systemd timer 02:00 ─┘  cli.py sync: çek → PDF metni → (OCR) → madde bölme → indeks
```

| Katman | Seçim |
|---|---|
| Backend | FastAPI + Uvicorn, Python 3.12 |
| Veritabanı | SQLite (`data/app.db`, WAL) + FTS5 tam metin araması |
| LLM | Azure AI Foundry, Responses API (`openai` SDK, `base_url` ile) |
| Vektör arama | Opsiyonel. `EMBEDDING_MODEL` tanımlıysa embedding'ler SQLite'ta saklanır, cosine benzerliği numpy ile hesaplanır. |
| PDF | `pypdf` (birincil), `pdfplumber` (yedek). Opsiyonel Azure Document Intelligence OCR. |
| Frontend | Saf HTML + CSS + vanilla JS. Build adımı ve CDN yok. |

### Proje yapısı

```
app/
  main.py               Uygulama, middleware sırası, router'lar, statik dosyalar, iş kuyruğu
  config.py             Tüm ayarlar (pydantic-settings) + mod bazlı doğrulama
  db.py                 SQLite bağlantısı, migration'lar (1-3), FTS5, tr_fold() SQL fonksiyonu
  logging_conf.py       JSON log (logs/app.log, rotasyonlu), secret maskeleme, audit kaydı
  middleware.py         Request ID, güvenlik başlıkları, access log (SSE uyumlu saf ASGI)
  errors.py             {"error": {"code", "message"}} hata formatı
  net.py                Kurumsal CA + sistem sertifikaları ile SSL context
  locks.py              Süreçler arası DB kilidi (senkronizasyon)
  demo.py               Örnek sözleşme PDF'leri (cli.py demo-data)
  auth/                 none / basic (bcrypt) / entra (msal) + oturum çerezi imzalama
  sources/              ContractSource arayüzü; local, mock, turkuaz kaynakları; turkuaz_mapping.py
  ingest/               pdf_extract, ocr, chunker (Türkçe madde bölme), normalize, pipeline (sync)
  llm/                  client (Responses API), prompts (tüm Türkçe promptlar), schemas, usage
  services/             search, chat (RAG), impact (etki analizi), jobs (kuyruk), export (Excel), embeddings
  api/                  routes_* (health, me, chat, impact, search, contracts, admin)
  static/               index.html, app.css, core.js, markdown.js, chat.js, impact.js, search.js, contracts.js, admin.js, app.js
cli.py                  Yönetim komutları
deploy/                 sozlesme.service, sozlesme-sync.service/.timer, nginx-sozlesme.conf, setup.sh, update.sh
tests/                  pytest (test PDF'leri reportlab ile programatik üretilir)
```

---

## Yerel geliştirme (Windows + VS Code)

Gereksinimler: Python 3.12+, Git, VS Code (Python eklentisi).

```powershell
git clone https://github.com/burakozkarabekir/HUKUK-Sozlesme.git
cd HUKUK-Sozlesme
py -3.12 -m venv venv
venv\Scripts\activate                      # PowerShell engellerse: venv\Scripts\activate.bat (cmd)
pip install -r requirements-dev.txt
copy .env.example .env                     # APP_ENV=dev, AUTH_MODE=none kalabilir
python cli.py migrate
```

**Örnek verilerle hızlı deneme** (gerçek sözleşme gerekmez):

```powershell
python cli.py demo-data                    # sample_contracts\ altına 6 örnek sözleşme + 1 taranmış PDF
python cli.py sync                         # indeksle
uvicorn app.main:app --reload --port 8001
```

Tarayıcıda **http://127.0.0.1:8001/sozlesme/** adresini açın. Sözleşmeler ekranı ve keyword arama LLM olmadan da çalışır. Sohbet ve etki analizi için `.env` içinde `AZURE_OPENAI_BASE_URL`, `AZURE_OPENAI_API_KEY` ve `CHAT_MODEL` doldurulmalı, ardından `python cli.py check-llm` çalıştırılmalıdır. Örnek mevzuat metni `sample_contracts\ornek_mevzuat_degisikligi.txt` dosyasındadır.

- `CONTRACT_SOURCE=mock` yapılırsa aynı PDF'ler Turkuaz formatındaymış gibi sunulur ve Turkuaz alan eşleştirmesinden geçer.
- Kendi PDF'lerinizi `sample_contracts\` altına koyabilirsiniz. Tek PDF bir sözleşmedir. Klasör ise ana sözleşme + ekler demektir; adı "Ek" ile başlayan dosyalar ek sayılır. Klasöre isteğe bağlı `metadata.json` konabilir. Biçim için `app/sources/local_source.py` dosyasının başındaki açıklamaya bakın. Bu klasörün içeriği repoya girmez.
- Şirket ağında `pip install` proxy veya SSL hatası verirse: `pip install --proxy http://proxy:port --cert C:\yol\kurum-ca.pem -r requirements-dev.txt`.
- API dokümantasyonu: http://127.0.0.1:8001/sozlesme/docs (prod'da kapalıdır).

---

## Yapılandırma (.env)

Tüm ayarlar `.env.example` dosyasında açıklamalarıyla listelidir. Kurallar:

- `.env` repoya girmez. Sunucuda `chmod 600` olmalıdır (`setup.sh` bunu yapar).
- **Satır sonu yorumu kullanmayın.** systemd `EnvironmentFile` bunu değerin parçası sayar.
- `$` içeren değerleri (bcrypt hash'leri) **tek tırnak** içine alın.
- Göreli yollar (`./data`) proje köküne göre çözülür.

| Grup | Ayar | Açıklama |
|---|---|---|
| Uygulama | `APP_ENV` | `dev` / `test` (sarı TEST rozeti) / `prod` (docs kapalı, `AUTH_MODE=none` yasak) |
| | `ROOT_PATH`, `HOST`, `PORT` | `/sozlesme`, `127.0.0.1`, `8001` |
| | `DATA_DIR`, `LOG_DIR`, `LOG_LEVEL` | Veri/log klasörleri, log seviyesi |
| Azure AI Foundry | `AZURE_OPENAI_BASE_URL` | `https://<kaynak>.services.ai.azure.com/openai/v1/` |
| | `AZURE_OPENAI_API_KEY`, `CHAT_MODEL` | API key ve **deployment adı** |
| | `EMBEDDING_MODEL` | Boşsa vektör arama kapalıdır. Doluysa sonrasında `cli.py embed` çalıştırılır. |
| | `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MAX_CONCURRENCY` | Zaman aşımı, retry, eşzamanlılık (worker başına) |
| | `LLM_STRUCTURED_MODE`, `LLM_STREAMING` | `auto` ya da `check-llm`'in önerdiği sabit değer |
| | `LLM_MAX_OUTPUT_TOKENS`, `LLM_REASONING_EFFORT` | Boş/0 ise gönderilmez |
| Sohbet | `CHAT_HISTORY_MESSAGES`, `CHAT_MAX_QUESTION_CHARS`, `CHAT_MAX_PROMPT_CHARS`, `CHAT_TOP_K`, `CHAT_REWRITE_QUERY` | Geçmiş, limitler, kaynak sayısı, devam sorusunu yeniden yazma |
| Etki analizi | `IMPACT_CHUNKS_PER_CONTRACT`, `IMPACT_MAX_CONTRACTS`, `IMPACT_LEGISLATION_PROMPT_CHARS`, `IMPACT_MAX_PARALLEL_JOBS`, `JOB_POLL_SECONDS` | Aday madde/sözleşme sınırları, iş kuyruğu |
| Kaynak | `CONTRACT_SOURCE` | `local` / `mock` / `turkuaz` |
| | `TURKUAZ_*` | Base URL, auth modu ve kimlik bilgileri, timeout, SSL, retry |
| OCR | `OCR_ENABLED`, `DOC_INTELLIGENCE_ENDPOINT`, `DOC_INTELLIGENCE_KEY`, `OCR_MIN_CHARS_PER_PAGE` | Taranmış PDF'ler için Azure Document Intelligence |
| Kimlik | `AUTH_MODE` | `none` / `basic` / `entra` |
| | `BASIC_AUTH_USERS`, `BASIC_AUTH_ADMINS` | `kullanici:bcrypt_hash,...` ve yönetici adları |
| | `ENTRA_*`, `ALLOWED_GROUP_IDS`, `ADMIN_GROUP_IDS` | Entra uygulama kaydı ve gruplar |
| | `SESSION_SECRET_KEY`, `SESSION_MAX_AGE_HOURS`, `SESSION_COOKIE_SECURE` | Oturum çerezi (entra modunda zorunlu, en az 32 karakter) |
| Limitler | `MAX_LEGISLATION_CHARS`, `MAX_UPLOAD_MB`, `RATE_LIMIT_PER_MINUTE` | Girdi ve istek sınırları |
| Ağ | `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` | Proxy ve kurumsal CA |

---

## Sunucuya ilk kurulum (CyberArk SSH)

Sunucu: `DOASDMZAIWEBT01` · Kullanıcı: `dtpamcxaiteam` · Klasör: `/opt/apps/sozlesme` · Port: `8001` (yalnızca 127.0.0.1) · URL yolu: `/sozlesme`

```bash
# 1) CyberArk üzerinden SSH ile bağlanın (dtpamcxaiteam).
sudo mkdir -p /opt/apps && sudo chown dtpamcxaiteam: /opt/apps
cd /opt/apps
git clone https://github.com/burakozkarabekir/HUKUK-Sozlesme.git sozlesme
cd sozlesme
#    Git'e proxy gerekiyorsa: git config --global http.proxy http://proxy:port

# 2) Kurulum (idempotent; tekrar çalıştırılabilir)
bash deploy/setup.sh
#    Python 3.12 yoksa: sudo apt install python3.12 python3.12-venv
#    İnternet yoksa: başka bir makinede (aynı Ubuntu + Python sürümü)
#      pip download -r requirements.txt -d wheels   → wheels/ klasörünü kopyalayın
#      WHEELHOUSE=/opt/apps/sozlesme/wheels bash deploy/setup.sh

# 3) Yapılandırma
nano .env              # APP_ENV=prod, AUTH_MODE, Azure, Turkuaz, proxy/CA ...
venv/bin/python cli.py migrate
venv/bin/python cli.py check-llm
venv/bin/python cli.py check-turkuaz      # CONTRACT_SOURCE=turkuaz ise

# 4) systemd
sudo cp deploy/sozlesme.service deploy/sozlesme-sync.service deploy/sozlesme-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sozlesme
sudo systemctl enable --now sozlesme-sync.timer
systemctl list-timers sozlesme-sync.timer

# 5) nginx: mevcut server { } bloğuna include edin (başka uygulamalar etkilenmez)
sudo cp deploy/nginx-sozlesme.conf /etc/nginx/snippets/sozlesme.conf
sudo nano /etc/nginx/sites-enabled/<mevcut-site>      # server { ... include snippets/sozlesme.conf; }
sudo nginx -t && sudo systemctl reload nginx

# 6) İlk senkronizasyon ve kontrol
venv/bin/python cli.py sync               # veya Yönetim > Şimdi Senkronize Et
curl -s http://127.0.0.1:8001/health
```

- `deploy/nginx-sozlesme.conf` şunları ayarlar: `/sozlesme` adresini `/sozlesme/`'ye yönlendirme, prefix'i soyan `proxy_pass http://127.0.0.1:8001/`, SSE için `proxy_buffering off` ve 300 sn okuma süresi, `client_max_body_size 30m`, `X-Forwarded-*` başlıkları. HTTPS örneği dosyada yorum satırı olarak duruyor. **`AUTH_MODE=entra` için HTTPS zorunludur.**
- **Yedekleme:** `venv/bin/python cli.py backup` komutu tutarlı bir DB kopyası alır (`data/backups/`). PDF'ler `data/files/` altındadır; ikisi birlikte yedeklenmelidir.

## Güncelleme

```bash
cd /opt/apps/sozlesme && bash deploy/update.sh
```

Betik sırasıyla `git pull --ff-only`, `pip install`, `migrate`, `systemctl restart sozlesme` ve `/health` kontrolü (30 sn) yapar. Sağlık kontrolü başarısız olursa uyarır ve servis durumunu gösterir. systemd veya nginx dosyaları değiştiyse kopyalama komutunu da yazar.

---

## Kontrol komutları

| Komut | Ne yapar |
|---|---|
| `python cli.py migrate` | Veritabanı migration'larını uygular |
| `python cli.py stats` | Tablo sayıları ve şema sürümü |
| `python cli.py sync [--full]` | Kaynaktan çeker ve indeksler (incremental). `--full` kaynakta olmayanları da siler. |
| `python cli.py reindex` | Saklanan PDF'lerden maddeleri ve FTS indeksini yeniden oluşturur (chunker değişince) |
| `python cli.py embed` | Eksik embedding'leri üretir (`EMBEDDING_MODEL` doluysa) |
| `python cli.py check-llm` | Azure bağlantısı, streaming, json_schema ve prompt-JSON desteğini test eder; `.env` önerisi yazar |
| `python cli.py check-turkuaz [--out ornek.json]` | Turkuaz'a bağlanır; ilk 3 kaydı, ham alan yollarını ve eşleştirme sonucunu gösterir |
| `python cli.py hash-password` | `BASIC_AUTH_USERS` için bcrypt hash üretir |
| `python cli.py backup [--out dosya]` | Çalışırken güvenli DB yedeği alır |
| `python cli.py demo-data` | Örnek sözleşme PDF'leri üretir (geliştirme) |

```bash
curl -s http://127.0.0.1:8001/health                                   # herkese açık
curl -s http://127.0.0.1:8001/api/contracts?page_size=5                 # AUTH_MODE=none
curl -s -u kullanici:sifre http://127.0.0.1:8001/api/me                 # AUTH_MODE=basic
curl -s "http://127.0.0.1:8001/api/search?q=fesih%20%22yaz%C4%B1l%C4%B1%20bildirim%22&mode=and"
curl -N -X POST http://127.0.0.1:8001/api/chat -H "Content-Type: application/json" \
     -H "X-Requested-With: fetch" -d '{"message":"Fesih bildirim süreleri nedir?"}'
journalctl -u sozlesme -f                  # servis logları
tail -f /opt/apps/sozlesme/logs/app.log    # JSON uygulama logu
journalctl -u sozlesme-sync -n 50          # gece senkronizasyonu
```

---

## Gerçek Turkuaz endpoint'i geldiğinde

Turkuaz'a özgü her şey **`app/sources/turkuaz_mapping.py`** dosyasındadır. HTTP, auth, retry ve sayfalama kodu (`turkuaz_source.py`) değiştirilmez.

1. `.env` içinde `CONTRACT_SOURCE=turkuaz`, `TURKUAZ_BASE_URL`, `TURKUAZ_AUTH_MODE` ve ilgili kimlik bilgilerini doldurun:
   - `api_key`: `TURKUAZ_API_KEY` ve `TURKUAZ_API_KEY_HEADER` (header adı ayarlanabilir).
   - `bearer`: `TURKUAZ_BEARER_TOKEN`.
   - `oauth_client_credentials`: `TURKUAZ_TOKEN_URL`, `TURKUAZ_CLIENT_ID`, `TURKUAZ_CLIENT_SECRET`, `TURKUAZ_SCOPE`. Token önbelleğe alınır, süresi dolmadan yenilenir, 401'de bir kez yenilenip tekrar denenir.
   - `basic`: `TURKUAZ_USERNAME`, `TURKUAZ_PASSWORD`.
2. `python cli.py check-turkuaz --out turkuaz_ornek.json` çalıştırın. Çıktı her alanı eşleştirme yolu biçiminde gösterir (`contractNo`, `parties[].name`, `meta.status` ...).
3. `turkuaz_mapping.py` içinde şunları güncelleyin:
   - `LIST_PATH`, `DOCUMENTS_PATH`: uç noktalar (`{contract_id}` yer tutucusu).
   - `UPDATED_SINCE_PARAM` / `UPDATED_SINCE_FORMAT`: incremental sync parametresi. Yoksa `""` yapın; filtre istemci tarafında da uygulanır.
   - `PAGINATION`: `style` = `page` | `offset` | `cursor` | `link` | `none`, parametre adları, `items_path` (kayıtların yanıttaki yeri; yanıtın kendisi liste ise `""`), `total_path`.
   - `CONTRACT_FIELD_MAP`: `contract_id`, `contract_no`, `title`, `parties`, `contract_type`, `start_date`, `end_date`, `status`, `updated_at`. Eşleştirilmeyen alanlar otomatik olarak `extra` alanına gider ve ekranda görünür.
   - `DOCUMENTS_ITEMS_PATH`, `FILE_FIELD_MAP`: dosya kimliği, adı, ek bayrağı, içerik (base64) **veya** indirme adresi (`download_url`).
   - `DOCUMENT_TYPE_PATH` / `ATTACHMENT_TYPE_VALUES`: ek bayrağı yoksa doküman tipinden ek tespiti.
4. `check-turkuaz` komutunu "✅ Turkuaz bağlantısı ve eşleştirme çalışıyor" çıkana kadar tekrarlayın. Ardından `python cli.py sync --full` çalıştırın.
5. Testleri güncelleyin: `tests/test_turkuaz.py` içindeki `record()` örneğini gerçek formata göre düzenleyip `pytest` çalıştırın.

Tarih alanları ISO (`2024-01-31`, `2024-01-31T10:00:00Z`) ve Türkçe biçimleri (`31.01.2024`, `31/01/2024`) kabul eder. Hata durumlarında anlamlı Türkçe mesaj verilir: 401 (kimlik), 403 (yetki), 404 (yol), 429 (Retry-After'a uyarak tekrar), 5xx ve zaman aşımı (artan beklemeyle tekrar), bağlantı/proxy/SSL hatası.

---

## Kimlik doğrulama

| Mod | Kullanım | Notlar |
|---|---|---|
| `none` | Sadece geliştirme | Herkes "Geliştirici" (yönetici) sayılır. `APP_ENV=prod` ile uygulama **başlamaz**. |
| `basic` | Test ortamı | Tarayıcının kendi giriş penceresi açılır. Şifreler bcrypt ile saklanır. 5 dakikada 10 hatalı denemeden sonra IP geçici olarak engellenir. |
| `entra` | Üretim | Microsoft Entra ID, OIDC authorization code flow (msal). Oturum çerezi imzalı, `HttpOnly`, `Secure`, `SameSite=Lax`, yolu `/sozlesme/` ile sınırlı. |

**Basic kullanıcı eklemek:**

```bash
venv/bin/python cli.py hash-password      # kullanıcı adı + şifre sorar, "ali:$2b$12$..." yazar
# .env:  BASIC_AUTH_USERS='ali:$2b$12$...,veli:$2b$12$...'   (tek tırnak!)
#        BASIC_AUTH_ADMINS=ali
sudo systemctl restart sozlesme
```

**Entra ID uygulama kaydı** (Azure Portal → Entra ID → App registrations):

1. **Yeni kayıt.** Platform: *Web*. Redirect URI: `https://<sunucu-adı>/sozlesme/auth/callback`. Bu değer `ENTRA_REDIRECT_URI` ile birebir aynı olmalıdır.
2. **Certificates & secrets.** Yeni bir client secret oluşturun ve `ENTRA_CLIENT_SECRET`'a yazın.
3. **Token configuration → Add groups claim.** *Security groups* seçin ve ID token'a ekleyin. Uygulama grup bilgisini token'dan okur.
4. Hukuk ekibinin grup Object ID'lerini `ALLOWED_GROUP_IDS`, yönetici grubunun ID'sini `ADMIN_GROUP_IDS` alanına yazın. `ALLOWED_GROUP_IDS` boş bırakılırsa tenant'taki herkes girebilir.
5. `.env` içinde `AUTH_MODE=entra`, `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` ve `SESSION_SECRET_KEY` (en az 32 karakter; `setup.sh` otomatik üretir) ayarlanır.
6. Sunucunun `login.microsoftonline.com` adresine çıkışı olmalıdır (proxy ve CA ayarları geçerlidir).

Tüm modlarda: değişiklik yapan API çağrıları `X-Requested-With: fetch` başlığı ister (CSRF koruması; frontend bunu otomatik gönderir). İstekler kullanıcı başına dakikada `RATE_LIMIT_PER_MINUTE` ile sınırlanır. Kullanıcı adı access log'a ve audit kaydına yazılır.

---

## API uçları

Tüm hatalar `{"error": {"code": "...", "message": "Türkçe açıklama"}}` biçimindedir.

| Method | Path | Açıklama |
|---|---|---|
| GET | `/health` | Uygulama sağlığı (auth gerektirmez) |
| GET | `/health/deps` | DB, LLM, embedding, kaynak ve OCR kontrolü (admin; gerçek çağrı yapar) |
| GET | `/api/me` | Giriş yapan kullanıcı, ortam, auth modu |
| POST | `/api/chat` | Sohbet, SSE akışı: `start`, `sources`, `delta`, `done`, `error` |
| POST | `/api/impact/extract` | Mevzuattan kavram çıkarma. Gövde JSON `{"text"}` veya ham PDF (`Content-Type: application/pdf`). |
| POST | `/api/impact/run` | Analiz işi başlatır → `job_id` (202) |
| GET | `/api/impact/history` | Geçmiş analizler |
| GET | `/api/impact/{job_id}` | İlerleme ve sonuçlar |
| POST | `/api/impact/{job_id}/cancel` | İptal |
| GET | `/api/impact/{job_id}/export.xlsx` | Excel (her etkilenen madde ayrı satır, filtreli başlıklar) |
| GET | `/api/search` | Keyword arama (`q`, `mode=and\|or`, filtreler, sayfalama) |
| GET | `/api/contracts` | Liste + filtre (`q`, `contract_type`, `party`, `status`, `date_from`, `date_to`, `active_only`) + sayfalama |
| GET | `/api/contracts/filters` | Filtre seçenekleri |
| GET | `/api/contracts/{id}` | Metadata + dosyalar + maddeler |
| GET | `/api/contracts/{id}/files/{file_id}` | PDF görüntüleme (yetkili, audit'li; `#page=N` ile sayfa) |
| GET | `/api/chunks/{id}` | Tek madde (kaynak paneli) |
| POST | `/api/admin/sync` | Manuel senkronizasyon (arka planda; aynı anda tek sync) |
| GET | `/api/admin/status` | Sayılar, son sync'ler, OCR bekleyenler, token kullanımı |
| GET | `/api/admin/audit` | Son audit kayıtları |
| GET | `/auth/login`, `/auth/callback`, `/auth/logout` | Entra oturum akışı |

---

## Güvenlik ve uyum

- **Secret'lar:** Sadece `.env` dosyasında tutulur (`chmod 600`). Kodda `SecretStr` kullanılır; değerleri `repr` çıktısında ve loglarda görünmez. Loglarda bilinen secret değerleri ile `Bearer ...`, `api_key=...` gibi kalıplar maskelenir.
- **Loglar:** `logs/app.log`, JSON formatında, 10 MB × 10 dosya rotasyonlu. Prompt, sözleşme metni ve model cevabı loglanmaz. LLM çağrıları için amaç, model, token ve süre kaydedilir.
- **Audit (`audit_log` tablosu):** Kim, ne zaman, hangi işlemi yaptı (giriş/çıkış, sohbet sorusu + kaynak sözleşme ID'leri, arama ifadesi, sözleşme/PDF görüntüleme, analiz başlatma/iptal/Excel, manuel sync). Mevzuat ve sözleşme metinleri bu tabloya yazılmaz.
- **LLM:** Her istekte `store=false` gönderilir. Sözleşme ve mevzuat metinleri promptta `<sozlesme_metni>` / `<mevzuat_metni>` etiketleriyle **veri** olarak verilir; metin içindeki kapanış etiketi etkisizleştirilir. Sistem promptu bu metinlerdeki talimatların uygulanmamasını ve kaynakta olmayan bilginin uydurulmamasını söyler. Kaynak bulunamazsa model çağrılmaz.
- **Halüsinasyon kontrolü:** Etki analizindeki her alıntı sözleşme metninde aranır. Bulunamayanlar ⚠️ ile işaretlenir ve Excel'de "Alıntı Doğrulandı: HAYIR" olarak görünür.
- **Web:** CSP `default-src 'self'`, `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`, `Permissions-Policy`. Model çıktısı DOM'a asla `innerHTML` ile yazılmaz; bunu bir test de kontrol eder. Excel hücrelerinde formül enjeksiyonu engellenir.
- **Girdi limitleri:** Mevzuat metni `MAX_LEGISLATION_CHARS`, PDF `MAX_UPLOAD_MB` (nginx'te de 30 MB). PDF imzası kontrol edilir.
- **Ekranda uyarı:** "Bu çıktılar yapay zeka tarafından üretilmiştir ve hukuki görüş niteliği taşımaz. Nihai değerlendirme Hukuk birimine aittir." Aynı uyarı Excel'de de yer alır.

---

## Sorun giderme

| Belirti | Olası neden / çözüm |
|---|---|
| `check-llm`: "bağlanılamadı (ağ, proxy veya SSL)" | `.env` içindeki `HTTPS_PROXY` ve `NO_PROXY` değerlerini kontrol edin. Kurumsal SSL denetimi varsa kök sertifikayı `REQUESTS_CA_BUNDLE=/etc/ssl/certs/kurum-ca.pem` olarak verin. Uygulama sistem sertifikalarını **ve** bu dosyayı birlikte kullanır. |
| `CERTIFICATE_VERIFY_FAILED` (pip / git) | `pip install --cert /yol/kurum-ca.pem ...`; `git config --global http.sslCAInfo /yol/kurum-ca.pem` |
| `check-llm`: "Model veya uç nokta bulunamadı" | `CHAT_MODEL` deployment adı olmalıdır (model adı değil). `AZURE_OPENAI_BASE_URL` `/openai/v1/` ile bitmelidir. |
| `check-llm`: json_schema ❌, prompt ✅ | Normaldir. `.env` içine `LLM_STRUCTURED_MODE=prompt` yazın. |
| 502 Bad Gateway | Uygulama çalışmıyor: `systemctl status sozlesme` ve `journalctl -u sozlesme -n 100`. Port 8001'i kullanan başka bir süreç var mı: `ss -ltnp \| grep 8001`. |
| Sayfa açılıyor ama stil/JS yüklenmiyor | Adres `/sozlesme/` (sonunda `/` ile) olmalıdır. nginx'te `proxy_pass http://127.0.0.1:8001/;` sonundaki `/` eksik olabilir. |
| Sohbet cevabı akmıyor, toptan geliyor | nginx'te `proxy_buffering off` eksik. Araya giren başka bir proxy veya WAF da SSE'yi tamponlayabilir. |
| 413 Request Entity Too Large | PDF çok büyük. `client_max_body_size` ve `MAX_UPLOAD_MB` değerlerini birlikte artırın. |
| Uygulama başlamıyor: "AUTH_MODE=none üretim ortamında..." | `APP_ENV=prod` iken `AUTH_MODE=basic` veya `entra` gerekir. |
| Entra: giriş döngüsü / çerez kaydedilmiyor | `SESSION_COOKIE_SECURE=true` HTTPS gerektirir. Site HTTP üzerinden açılıyorsa çerez gönderilmez. `ENTRA_REDIRECT_URI` kayıttaki adresle birebir aynı olmalıdır. |
| Entra: "Bu uygulamayı kullanma yetkiniz yok" | Kullanıcı `ALLOWED_GROUP_IDS` içindeki gruplardan birinde değil, ya da groups claim eklenmemiş. |
| Taranmış PDF'ler aranamıyor | Yönetim → "OCR gerektiren dosyalar" listesine bakın. `OCR_ENABLED=true` ve Document Intelligence ayarlarını yapıp `cli.py reindex` çalıştırın. |
| "Şu anda başka bir senkronizasyon çalışıyor" | Normaldir; tek sync çalışabilir. Takılı kalırsa kilit 2 saat sonra kendiliğinden düşer. |
| `database is locked` | Çok nadirdir; WAL ve 30 sn bekleme açıktır. `data/` klasörünün yazılabilir olduğunu kontrol edin. |
| Servis logları | `journalctl -u sozlesme -f` · `tail -f logs/app.log` · `journalctl -u sozlesme-sync` |

---

## Testler

```bash
pip install -r requirements-dev.txt
pytest                      # tüm testler; harici servis gerekmez
ruff check . && ruff format --check .
```

- Test PDF'leri `reportlab` ile programatik üretilir; `sample_contracts/` boşken de testler çalışır.
- LLM testlerinde sahte bir Responses API kullanılır. Turkuaz testlerinde HTTP `respx` ile taklit edilir.
- Kapsam: Türkçe madde başlıkları (chunker), Türkçe normalizasyon ve FTS, PDF çıkarma ve OCR işaretleme, incremental sync ve kilitler, Turkuaz'ın tüm auth modları, sayfalama ve hata kodları, LLM katmanı (structured output yedeği, onarım, streaming yedeği, hata eşleme, eşzamanlılık), RAG sohbeti, uçtan uca etki analizi (alıntı doğrulama, iptal, kurtarma, Excel), basic/entra kimlik doğrulama, CSRF, rate limit, API uçları, frontend güvenlik kuralları.

---

## Varsayımlar

Kodda `# VARSAYIM:` ile işaretli yerler:

| Konu | Varsayım | Yer |
|---|---|---|
| Turkuaz formatı | Uç noktalar, alan adları ve sayfalama yer tutucudur; gerçek format gelince değiştirilecek | `sources/turkuaz_mapping.py` |
| Turkuaz dosyaları | İçerik base64 olarak veya indirme adresiyle gelebilir | `sources/base.py` |
| Ek dosyalar | Yerel kaynakta adı "Ek-1", "EK2", "Annex" ile başlayan dosyalar ek sayılır | `sources/local_source.py` |
| Taranmış PDF | Sayfaların yarısından fazlasında `OCR_MIN_CHARS_PER_PAGE` (40) karakterden az metin varsa | `ingest/pdf_extract.py` |
| Madde bölme | İlk başlıktan önceki metin "Giriş" bölümüdür; sayfa üst/alt bilgileri temizlenmez | `ingest/chunker.py` |
| Senkronizasyon | Incremental filtre yalnızca tam başarılı çalıştırmadan sonra ilerler; silme yalnızca `--full` ile yapılır | `ingest/pipeline.py` |
| Arama | 3+ harfli kelimeler önek olarak eşleşir; kök ünlü değişimleri (fesih→feshi) için LLM varyant önerir | `services/search.py` |
| Yürürlükte | Bitiş tarihi boş veya bugünden sonra **ve** durum kapalı değil ("Sona Erdi", "Feshedildi", "İptal" ...) | `services/contracts.py` |
| Alıntı doğrulama | Kelimeler (harf, Türkçe karakter ve noktalama duyarsız) aynı maddede aynı sırada geçmeli; "..." atlamaya izin verir | `services/impact.py` |
| Analizler | Ekip tüm analizleri görür (kişiye özel değil) | `api/routes_impact.py` |
| İş kuyruğu | 5 dk heartbeat'siz iş ölü sayılır, baştan çalıştırılır (en fazla 2 deneme) | `services/jobs.py` |
| LLM | Retry'lar SDK'nın artan beklemeli mekanizmasıyla yapılır; eşzamanlılık ve rate limit worker başınadır | `llm/client.py`, `auth/__init__.py` |
| Sohbet | Geçmiş tarayıcı oturumunda tutulur ve her istekle gönderilir; kapsam listesi en fazla 500 sözleşme gösterir | `models.py`, `static/chat.js` |
| Kimlik | none modunda herkes yönetici; basic'te yöneticiler `BASIC_AUTH_ADMINS`; Entra'da `ADMIN_GROUP_IDS`; 200+ grup (overage) desteklenmez | `auth/` |
| Oturum | 8 saat, Secure çerez (HTTPS gerekir) | `config.py` |
| Limitler | Mevzuat 200.000 karakter, PDF 25 MB, dakikada 60 istek, sohbette son 10 mesaj / 60.000 karakter / 8 kaynak | `config.py` |
| Ek ayarlar | `LOG_DIR`, `LLM_*` modları, `CHAT_*`, `IMPACT_*`, `SESSION_*`, `TURKUAZ_MAX_RETRIES`, `DOC_INTELLIGENCE_API_VERSION` (2024-11-30) | `config.py` |

Spesifikasyona eklenen uçlar: `GET /api/me`, `GET /api/contracts/filters`, `GET /api/chunks/{id}`, `GET /api/admin/audit`, `/auth/*`.

## Bilinen eksikler ve sonraki adımlar

- **Gerçek servislerle test edilmedi.** Azure AI Foundry, Turkuaz, Entra ID ve Document Intelligence sahte uç noktalarla test edildi. İlk iş: `check-llm` ve `check-turkuaz` çalıştırmak.
- **Rate limit ve LLM eşzamanlılığı süreç (worker) başınadır.** 2 worker'da etkin sınır iki katıdır.
- **Vektör arama tüm vektörleri belleğe alır.** On binlerce madde için yeterlidir; çok daha büyük arşivlerde ANN indeksi gerekir.
- **Entra grup "overage" durumu desteklenmez.** 200'den fazla grubu olan kullanıcılar için Graph sorgusu gerekir; alternatif olarak "uygulamaya atanmış gruplar" yayınlanabilir.
- **Tam senkronizasyonda silinen sözleşmelerin PDF dosyaları diskte kalır** (`data/files`). Periyodik temizlik eklenebilir.
- **Sayfa üst/alt bilgileri madde metninden ayıklanmaz.**
- **Keyword aramada kök değişimi olan Türkçe kelimeler eşleşmez** (fesih → feshi). Etki analizinde LLM bu varyantları anahtar kelime olarak önerir.
- **Sözleşme bazında yetkilendirme yoktur.** Giriş yapan herkes tüm sözleşmeleri görür.
- **Sadece PDF desteklenir.** DOCX gibi formatlar desteklenmez.

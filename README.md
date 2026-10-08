# Sözleşme Mevzuat Etki Analizi

Doğuş Otomotiv Hukuk birimi için yapay zeka destekli bir web uygulaması. Turkuaz'daki sözleşme PDF'lerini ve eklerini okur ve şunları yapar:

1. **Mevzuat etki analizi:** Girilen bir mevzuat değişikliğinden hangi sözleşmelerin ve maddelerin etkilendiğini gerekçesiyle listeler. Sonuç Excel olarak indirilebilir.
2. **Sözleşme sohbeti (RAG):** Sözleşmeler hakkında soru-cevap. Her cevapta kaynak gösterilir.
3. **Keyword arama:** Tüm sözleşmelerde kelime ve ifade araması.

> **Durum: Faz 1 (İskelet).** Proje yapısı, yapılandırma, loglama, veritabanı/migration, `/health` ve UI iskeleti hazır. Diğer özellikler sonraki fazlarda gelecek (aşağıdaki [Yol haritası](#yol-haritası) bölümüne bakın).

---

## Teknoloji

| Katman | Seçim |
|---|---|
| Backend | FastAPI + Uvicorn (Python 3.12) |
| Veritabanı | SQLite (`data/app.db`) + FTS5 |
| LLM | Azure AI Foundry, Responses API (OpenAI Python SDK). Faz 2'de eklenecek. |
| Frontend | Saf HTML + CSS + vanilla JS. Build adımı ve CDN yok. |
| Yapılandırma | `pydantic-settings` + `.env` |

## Proje yapısı

```
app/
  main.py            FastAPI uygulaması (root_path, middleware, router'lar, statik dosyalar)
  config.py          Tüm ayarlar (pydantic-settings); prod + AUTH_MODE=none kontrolü
  logging_conf.py    JSON log (logs/app.log, rotasyonlu), secret maskeleme, audit kaydı
  db.py              SQLite bağlantısı, migration'lar, FTS5 şeması
  errors.py          Tutarlı hata formatı: {"error": {"code", "message"}}
  middleware.py      Request ID, güvenlik başlıkları (CSP vb.), access log
  models.py          Pydantic şemaları
  auth/              Kimlik doğrulama (Faz 1: sadece AUTH_MODE=none)
  api/               HTTP uçları (routes_health, routes_me, ...)
  sources/ ingest/ llm/ services/   Sonraki fazlar
  static/            index.html, app.css, app.js
cli.py               python cli.py migrate | stats | sync | reindex | check-llm | check-turkuaz
tests/               pytest testleri
deploy/              systemd, nginx, setup/update script'leri (Faz 8)
sample_contracts/    Geliştirme için yerel PDF klasörü (repoya PDF konmaz)
```

## Yerel geliştirme (Windows + VS Code)

```powershell
# 1) Sanal ortam
py -3.12 -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt

# 2) Yapılandırma
copy .env.example .env
#    .env içinde APP_ENV=dev ve AUTH_MODE=none kalabilir.

# 3) Veritabanı
python cli.py migrate

# 4) Çalıştır
uvicorn app.main:app --reload --port 8001
```

Tarayıcıda **http://127.0.0.1:8001/sozlesme/** adresini açın. `http://127.0.0.1:8001/` adresi otomatik olarak `/sozlesme/` adresine yönlenir. API dokümantasyonu `http://127.0.0.1:8001/sozlesme/docs` adresindedir (prod'da kapalıdır).

> Linux/macOS: `python3.12 -m venv venv && source venv/bin/activate`

### Testler ve lint

```bash
pytest
ruff check .
ruff format --check .
```

## Kontrol komutları

```bash
python cli.py migrate     # migration'ları uygular (idempotent)
python cli.py stats       # tablo kayıt sayıları ve şema sürümü
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/api/me
```

`check-llm` (Faz 2), `sync`/`reindex` (Faz 3) ve `check-turkuaz` (Faz 6) komutları ilgili fazlarda eklenecek.

## Yapılandırma notları

- Tüm ayarlar `.env.example` içinde listelenir. **`.env` dosyası repoya girmez.** Sunucuda `chmod 600 .env` yapın.
- `.env` içinde **satır sonu yorumu kullanmayın.** systemd `EnvironmentFile` satır sonu yorumlarını değerin parçası sayar. Açıklamaları ayrı satıra yazın.
- `DATA_DIR`, `LOG_DIR` ve `LOCAL_CONTRACTS_DIR` göreli verilirse proje köküne göre çözülür, çalışma dizinine göre değil.
- `APP_ENV=prod` ve `AUTH_MODE=none` birlikte verilirse uygulama başlamaz.
- Proxy ve kurumsal CA: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`. Bu değerler sadece `.env` içinde verilmişse uygulama açılışta bunları ortam değişkenine aktarır. Ortamda zaten tanımlı olan değer önceliklidir.

## Root path ve nginx

Uygulama nginx arkasında `/sozlesme` altında çalışır. Uvicorn `--root-path /sozlesme` ile başlatılır. nginx `proxy_pass http://127.0.0.1:8001/;` ile prefix'i soyar, uvicorn prefix'i yeniden ekler. Frontend'deki tüm link ve `fetch` çağrıları **göreli**dir (`static/app.js`, `api/me`). Bu yüzden sayfanın her zaman `/sozlesme/` (sonunda `/` ile) adresinden açılması gerekir. Uygulama bu adrese kendisi yönlendirir. nginx yapılandırması Faz 8'de `deploy/` altına eklenecek.

## Güvenlik (Faz 1'de olanlar)

- Güvenlik başlıkları: `Content-Security-Policy: default-src 'self'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN` (PDF'lerin yan panelde gösterilebilmesi için), `Referrer-Policy`.
- Frontend'de model ve sözleşme çıktıları `innerHTML` ile yazılmaz. Bunu bir test de kontrol eder.
- Loglarda bilinen secret değerleri ve `Bearer ...`, `api_key=...` gibi kalıplar maskelenir.
- Hatalar tek bir JSON formatında döner, iç detay sızdırmaz: `{"error": {"code": "...", "message": "Türkçe açıklama"}}`.

## Veritabanı şeması (migration 1)

| Tablo | Amaç |
|---|---|
| `contracts` | Sözleşme metadata'sı (taraflar JSON, `extra_json` bilinmeyen alanlar için) |
| `contract_files` | Ana PDF ve ekler, SHA-256 (incremental sync), OCR durumu |
| `chunks` + `chunks_fts` | Madde bazlı parçalar. Orijinal metin `text`, normalize metin `text_norm`. FTS5 indeksi trigger'larla senkron tutulur. |
| `embeddings` | Opsiyonel vektörler (`EMBEDDING_MODEL` tanımlıysa) |
| `sync_runs` | Senkronizasyon geçmişi |
| `analysis_jobs`, `analysis_results` | Etki analizi işleri ve sonuçları |
| `llm_usage` | Token kullanımı (günlük toplam için) |
| `audit_log` | Kim, ne zaman, ne yaptı (sözleşme metni ve prompt loglanmaz) |
| `app_locks` | Birden fazla worker arasında DB tabanlı kilit (sync, arka plan işleri) |

Migration'lar `BEGIN IMMEDIATE` altında uygulanır. Bu sayede 2 uvicorn worker aynı anda açılsa da çakışmaz. Uygulanmış bir migration değiştirilmez; her değişiklik yeni bir migration olarak eklenir.

## Varsayımlar

Kodda `# VARSAYIM:` ile işaretlenen yerler:

| # | Varsayım | Yer |
|---|---|---|
| 1 | Log klasörü `LOG_DIR` ile ayarlanabilir (varsayılan `./logs`). | `app/config.py` |
| 2 | basic modda yöneticiler `BASIC_AUTH_ADMINS` (kullanıcı adı listesi) ile belirlenir. | `app/config.py` |
| 3 | Entra'da admin rolü `ADMIN_GROUP_IDS` değişkeniyle verilir (spesifikasyonda değişken adı yoktu). | `app/config.py` |
| 4 | Oturum çerezleri `SESSION_SECRET_KEY` ile imzalanır. Boşsa her süreç için rastgele üretilir (Faz 7). | `app/config.py` |
| 5 | Varsayılan limitler: mevzuat metni 200.000 karakter, PDF 25 MB, kullanıcı başına dakikada 60 istek. | `app/config.py` |
| 6 | `AUTH_MODE=none` (sadece dev) iken her istek yerel "Geliştirici" yönetici kullanıcısı olarak işlenir. | `app/auth/__init__.py` |
| 7 | Access log'a kullanıcı adı Faz 7'de (auth middleware'e taşındığında) yazılacak. Şimdilik `-`. | `app/auth/__init__.py` |

Spesifikasyona eklenen küçük uçlar: `GET /api/me` (üst bardaki kullanıcı adı ve Yönetim menüsünün görünürlüğü için).

## Yol haritası

- [x] **Faz 1 – İskelet**
- [ ] Faz 2 – LLM katmanı (`llm/client.py`, `check-llm`, streaming sohbet)
- [ ] Faz 3 – Kaynak + ingestion (local/mock kaynak, PDF çıkarma, chunker, FTS5, `sync`, Sözleşmeler ekranı)
- [ ] Faz 4 – Hibrit arama + RAG sohbet (kaynak paneli, PDF görüntüleme)
- [ ] Faz 5 – Mevzuat etki analizi (arka plan işi, alıntı doğrulama, Excel, geçmiş)
- [ ] Faz 6 – Gerçek Turkuaz kaynağı (auth modları, pagination, mapping, `check-turkuaz`)
- [ ] Faz 7 – Auth + güvenlik (basic/entra, audit, rate limit, yönetim ekranı)
- [ ] Faz 8 – Deployment (systemd, timer, nginx, `setup.sh`, `update.sh`, sunucu kurulum rehberi)
- [ ] Faz 9 – Testler ve son kontrol

Sunucu kurulumu (CyberArk SSH, `/opt/apps/sozlesme`, `sozlesme.service`), Turkuaz mapping rehberi ve sorun giderme bölümleri Faz 6 ve Faz 8'de eklenecek.

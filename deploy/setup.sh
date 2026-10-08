#!/usr/bin/env bash
# =============================================================================
# Sözleşme Mevzuat Etki Analizi - ilk kurulum (idempotent: tekrar çalıştırılabilir)
#
# Kullanım (sunucuda, dtpamcxaiteam kullanıcısıyla):
#   cd /opt/apps/sozlesme && bash deploy/setup.sh
#
# Ortam değişkenleri (opsiyonel):
#   PYTHON=python3.12        Kullanılacak Python
#   WHEELHOUSE=/yol/wheels   İnternet yoksa önceden indirilmiş paket klasörü
#   HTTPS_PROXY=...          pip için proxy (.env içinde varsa oradan da okunur)
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-}"
cd "$APP_DIR"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[UYARI]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[HATA]\033[0m %s\n' "$*" >&2; exit 1; }

# .env içinden bir değeri oku (tırnaksız/tırnaklı), yoksa boş.
env_value() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//"
}

# --- 1. Python ---------------------------------------------------------------
if [ -z "$PYTHON" ]; then
    if command -v python3.12 >/dev/null 2>&1; then PYTHON=python3.12; else PYTHON=python3; fi
fi
command -v "$PYTHON" >/dev/null 2>&1 || fail "$PYTHON bulunamadı. Python 3.12 kurun (sudo apt install python3.12 python3.12-venv)."
"$PYTHON" - <<'PY' || fail "Python 3.12 veya üzeri gerekli."
import sys
sys.exit(0 if sys.version_info >= (3, 12) else 1)
PY
"$PYTHON" - <<'PY' || fail "Python'un SQLite kütüphanesinde FTS5 desteği yok."
import sqlite3
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
PY
info "Python: $("$PYTHON" --version) ($APP_DIR)"

# --- 2. Sanal ortam ---------------------------------------------------------
if [ ! -x venv/bin/python ]; then
    info "Sanal ortam oluşturuluyor (venv/)"
    "$PYTHON" -m venv venv || fail "venv oluşturulamadı (sudo apt install python3.12-venv gerekebilir)."
else
    info "Sanal ortam mevcut (venv/)"
fi

# --- 3. Paketler -------------------------------------------------------------
PROXY="${HTTPS_PROXY:-$(env_value HTTPS_PROXY)}"
CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$(env_value REQUESTS_CA_BUNDLE)}"
PIP_ARGS=(--disable-pip-version-check)
[ -n "$PROXY" ] && PIP_ARGS+=(--proxy "$PROXY") && info "pip proxy: $PROXY"
[ -n "$CA_BUNDLE" ] && PIP_ARGS+=(--cert "$CA_BUNDLE") && info "pip CA: $CA_BUNDLE"
if [ -n "${WHEELHOUSE:-}" ]; then
    info "Paketler çevrimdışı klasörden kuruluyor: $WHEELHOUSE"
    venv/bin/pip install "${PIP_ARGS[@]}" --no-index --find-links "$WHEELHOUSE" -r requirements.txt
else
    info "Paketler kuruluyor (requirements.txt)"
    venv/bin/pip install "${PIP_ARGS[@]}" --upgrade pip >/dev/null || warn "pip güncellenemedi; devam ediliyor."
    venv/bin/pip install "${PIP_ARGS[@]}" -r requirements.txt
fi

# --- 4. Klasörler ------------------------------------------------------------
mkdir -p data logs sample_contracts
chmod 750 data logs
info "data/ ve logs/ hazır"

# --- 5. .env -------------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    # Oturum çerezleri için rastgele anahtar üret (tüm worker'lar aynı anahtarı kullanır).
    SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    sed -i "s|^SESSION_SECRET_KEY=.*|SESSION_SECRET_KEY=${SECRET}|" .env
    warn ".env oluşturuldu (.env.example'dan). Şimdi düzenleyin: nano $APP_DIR/.env"
    warn "En az şunları doldurun: APP_ENV, AUTH_MODE, AZURE_OPENAI_BASE_URL, AZURE_OPENAI_API_KEY, CHAT_MODEL, CONTRACT_SOURCE"
    NEW_ENV=1
else
    info ".env mevcut (değiştirilmedi)"
    NEW_ENV=0
fi
chmod 600 .env

# --- 6. Veritabanı -------------------------------------------------------------
if [ "$NEW_ENV" = "1" ]; then
    warn "Veritabanı migration'ı .env düzenlendikten sonra çalıştırılmalı: venv/bin/python cli.py migrate"
else
    info "Veritabanı migration'ları uygulanıyor"
    venv/bin/python cli.py migrate || fail "Migration başarısız. .env ayarlarını kontrol edin (APP_ENV=prod ise AUTH_MODE none olamaz)."
fi

# --- 7. Sonraki adımlar -------------------------------------------------------
cat <<EOF

=============================================================================
Kurulum tamamlandı. Sonraki adımlar (sudo yetkisi gerekir):

  1) .env dosyasını düzenleyin ve kontrol edin:
       nano $APP_DIR/.env
       venv/bin/python cli.py migrate
       venv/bin/python cli.py check-llm
       venv/bin/python cli.py check-turkuaz      # CONTRACT_SOURCE=turkuaz ise

  2) systemd servisleri:
       sudo cp deploy/sozlesme.service deploy/sozlesme-sync.service deploy/sozlesme-sync.timer /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now sozlesme
       sudo systemctl enable --now sozlesme-sync.timer

  3) nginx (mevcut server bloğuna include edin):
       sudo cp deploy/nginx-sozlesme.conf /etc/nginx/snippets/sozlesme.conf
       # server { ... include snippets/sozlesme.conf; ... }
       sudo nginx -t && sudo systemctl reload nginx

  4) Kontrol:
       curl -s http://127.0.0.1:8001/health
       journalctl -u sozlesme -f
       İlk senkronizasyon: venv/bin/python cli.py sync   (veya Yönetim ekranı)
=============================================================================
EOF

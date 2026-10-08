#!/usr/bin/env bash
# =============================================================================
# Sözleşme Mevzuat Etki Analizi - güncelleme
#
#   cd /opt/apps/sozlesme && bash deploy/update.sh
#
# git pull -> pip install -> migrate -> servis restart -> /health kontrolü.
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="${SERVICE:-sozlesme}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
cd "$APP_DIR"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[UYARI]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[HATA]\033[0m %s\n' "$*" >&2; exit 1; }

env_value() {
    [ -f .env ] || return 0
    grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//"
}

[ -x venv/bin/python ] || fail "venv bulunamadı. Önce deploy/setup.sh çalıştırın."
[ -f .env ] || fail ".env bulunamadı."

# Değişen deploy dosyalarını tespit etmek için önceki sürüm.
BEFORE="$(git rev-parse HEAD)"

info "Kod güncelleniyor (git pull --ff-only)"
git pull --ff-only || fail "git pull başarısız (yerel değişiklik veya ağ/proxy sorunu olabilir: git status)."
AFTER="$(git rev-parse HEAD)"
if [ "$BEFORE" = "$AFTER" ]; then
    info "Yeni commit yok ($AFTER)."
else
    info "Güncellendi: ${BEFORE:0:8} -> ${AFTER:0:8}"
    git --no-pager log --oneline "$BEFORE..$AFTER" | head -20
fi

PROXY="${HTTPS_PROXY:-$(env_value HTTPS_PROXY)}"
CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$(env_value REQUESTS_CA_BUNDLE)}"
PIP_ARGS=(--disable-pip-version-check -q)
[ -n "$PROXY" ] && PIP_ARGS+=(--proxy "$PROXY")
[ -n "$CA_BUNDLE" ] && PIP_ARGS+=(--cert "$CA_BUNDLE")
if [ -n "${WHEELHOUSE:-}" ]; then
    PIP_ARGS+=(--no-index --find-links "$WHEELHOUSE")
fi
info "Paketler güncelleniyor"
venv/bin/pip install "${PIP_ARGS[@]}" -r requirements.txt

info "Veritabanı migration'ları"
venv/bin/python cli.py migrate

if [ "$BEFORE" != "$AFTER" ] && git diff --name-only "$BEFORE" "$AFTER" | grep -q '^deploy/.*\.\(service\|timer\)$'; then
    warn "systemd dosyaları değişti. Kopyalayıp yeniden yükleyin:"
    warn "  sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload"
fi
if [ "$BEFORE" != "$AFTER" ] && git diff --name-only "$BEFORE" "$AFTER" | grep -q '^deploy/nginx-sozlesme.conf$'; then
    warn "nginx yapılandırması değişti: sudo cp deploy/nginx-sozlesme.conf /etc/nginx/snippets/sozlesme.conf && sudo nginx -t && sudo systemctl reload nginx"
fi

info "Servis yeniden başlatılıyor ($SERVICE)"
sudo systemctl restart "$SERVICE"

info "Sağlık kontrolü: $HEALTH_URL"
for _ in $(seq 1 30); do
    if BODY="$(curl -fsS --noproxy '*' --max-time 3 "$HEALTH_URL" 2>/dev/null)"; then
        info "Uygulama çalışıyor: $BODY"
        exit 0
    fi
    sleep 1
done

warn "Uygulama 30 saniye içinde sağlıklı yanıt vermedi!"
warn "Loglar:  journalctl -u $SERVICE -n 100 --no-pager"
warn "Durum:   systemctl status $SERVICE"
sudo systemctl --no-pager status "$SERVICE" | head -20 || true
exit 1

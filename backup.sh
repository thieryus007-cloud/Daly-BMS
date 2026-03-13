#!/usr/bin/env bash
# backup.sh — DalyBMS Interface — Santuario
# Sauvegarde : configuration BMS, dashboards Grafana, base InfluxDB, alertes SQLite

set -euo pipefail

INSTALL_DIR="/opt/dalybms"
DATA_DIR="/data/dalybms"
LOG_DIR="/var/log/dalybms"
BACKUP_BASE="${1:-/data/dalybms/backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_BASE}/${TIMESTAMP}"
ENV_FILE="${INSTALL_DIR}/.env"

# ── Couleurs ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[backup]${NC} $*"; }
warn() { echo -e "${YELLOW}[backup]${NC} $*"; }
fail() { echo -e "${RED}[backup]${NC} $*" >&2; exit 1; }

# ── Chargement .env ───────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    fail "Fichier .env introuvable : $ENV_FILE"
fi

INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
INFLUX_TOKEN="${INFLUX_TOKEN:-}"
INFLUX_ORG="${INFLUX_ORG:-santuario}"
INFLUX_BUCKET="${INFLUX_BUCKET:-daly_bms}"
INFLUX_BUCKET_DS="${INFLUX_BUCKET_DS:-daly_bms_1m}"
GRAFANA_HOST="${GRAFANA_HOST:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASS="${GRAFANA_ADMIN_PASS:-admin}"

# ── Création répertoire de sauvegarde ─────────────────────────────────────────
mkdir -p "${BACKUP_DIR}"/{config,influxdb,grafana,sqlite,logs}
log "Répertoire de sauvegarde : ${BACKUP_DIR}"

# ── 1. Configuration ──────────────────────────────────────────────────────────
log "Sauvegarde configuration..."

# .env (token masqué dans les logs, fichier copié intégralement)
cp "${ENV_FILE}" "${BACKUP_DIR}/config/.env"

# Sources Python
cp "${INSTALL_DIR}"/*.py "${BACKUP_DIR}/config/" 2>/dev/null || true

# Services systemd
if ls /etc/systemd/system/dalybms*.service /etc/systemd/system/dalybms.target 2>/dev/null | head -1 &>/dev/null; then
    cp /etc/systemd/system/dalybms*.service "${BACKUP_DIR}/config/" 2>/dev/null || true
    cp /etc/systemd/system/dalybms.target   "${BACKUP_DIR}/config/" 2>/dev/null || true
fi

# Nginx
if [[ -f /etc/nginx/sites-available/dalybms ]]; then
    cp /etc/nginx/sites-available/dalybms "${BACKUP_DIR}/config/nginx_dalybms.conf"
fi

log "  ✔ Configuration sauvegardée"

# ── 2. InfluxDB ───────────────────────────────────────────────────────────────
log "Sauvegarde InfluxDB..."

if [[ -z "$INFLUX_TOKEN" ]]; then
    warn "  INFLUX_TOKEN non défini — export InfluxDB ignoré"
else
    if command -v influx &>/dev/null; then
        # Bucket full-résolution
        if influx backup \
            --host "$INFLUX_URL" \
            --token "$INFLUX_TOKEN" \
            --org "$INFLUX_ORG" \
            --bucket "$INFLUX_BUCKET" \
            "${BACKUP_DIR}/influxdb/full_res" 2>/dev/null; then
            log "  ✔ Bucket ${INFLUX_BUCKET} exporté"
        else
            warn "  Export bucket ${INFLUX_BUCKET} échoué"
        fi

        # Bucket downsampled
        if influx backup \
            --host "$INFLUX_URL" \
            --token "$INFLUX_TOKEN" \
            --org "$INFLUX_ORG" \
            --bucket "$INFLUX_BUCKET_DS" \
            "${BACKUP_DIR}/influxdb/downsampled" 2>/dev/null; then
            log "  ✔ Bucket ${INFLUX_BUCKET_DS} exporté"
        else
            warn "  Export bucket ${INFLUX_BUCKET_DS} échoué (peut être vide)"
        fi

        # Export tâches Flux (downsampling)
        influx task list \
            --host "$INFLUX_URL" \
            --token "$INFLUX_TOKEN" \
            --org "$INFLUX_ORG" \
            --json > "${BACKUP_DIR}/influxdb/tasks.json" 2>/dev/null || true
    else
        warn "  Commande 'influx' introuvable — export InfluxDB ignoré"
    fi
fi

# ── 3. Grafana ────────────────────────────────────────────────────────────────
log "Sauvegarde dashboards Grafana..."

GRAFANA_API="${GRAFANA_HOST}/api"
GRAFANA_AUTH="${GRAFANA_USER}:${GRAFANA_PASS}"

if curl -s -o /dev/null -w "%{http_code}" \
        -u "$GRAFANA_AUTH" "${GRAFANA_API}/health" 2>/dev/null | grep -q "200"; then

    # Liste des dashboards
    mapfile -t DASHBOARD_UIDS < <(
        curl -s -u "$GRAFANA_AUTH" "${GRAFANA_API}/search?type=dash-db" \
        | python3 -c "import sys,json; [print(d['uid']) for d in json.load(sys.stdin)]" 2>/dev/null
    )

    COUNT=0
    for UID in "${DASHBOARD_UIDS[@]}"; do
        FNAME="${BACKUP_DIR}/grafana/dashboard_${UID}.json"
        if curl -s -u "$GRAFANA_AUTH" \
                "${GRAFANA_API}/dashboards/uid/${UID}" \
                -o "$FNAME" 2>/dev/null; then
            (( COUNT++ )) || true
        fi
    done
    log "  ✔ ${COUNT} dashboard(s) exporté(s)"

    # Datasources
    curl -s -u "$GRAFANA_AUTH" \
        "${GRAFANA_API}/datasources" \
        -o "${BACKUP_DIR}/grafana/datasources.json" 2>/dev/null || true
    log "  ✔ Datasources exportées"
else
    warn "  Grafana inaccessible sur ${GRAFANA_HOST} — export ignoré"
fi

# ── 4. SQLite alertes ─────────────────────────────────────────────────────────
log "Sauvegarde base SQLite alertes..."

ALERT_DB="${ALERT_DB_PATH:-${DATA_DIR}/alerts.db}"
if [[ -f "$ALERT_DB" ]]; then
    cp "$ALERT_DB" "${BACKUP_DIR}/sqlite/alerts.db"
    log "  ✔ ${ALERT_DB} copié"
else
    warn "  Base alertes introuvable : ${ALERT_DB}"
fi

# ── 5. Logs récents ───────────────────────────────────────────────────────────
log "Export logs récents (48h)..."
for SVC in api mqtt influx alerts venus; do
    journalctl -u "dalybms-${SVC}" \
        --since "48 hours ago" \
        --no-pager \
        > "${BACKUP_DIR}/logs/dalybms-${SVC}.log" 2>/dev/null || true
done
log "  ✔ Logs exportés"

# ── 6. Archive finale ─────────────────────────────────────────────────────────
ARCHIVE="${BACKUP_BASE}/dalybms_backup_${TIMESTAMP}.tar.gz"
tar -czf "$ARCHIVE" -C "$BACKUP_BASE" "$TIMESTAMP"
rm -rf "$BACKUP_DIR"

SIZE=$(du -sh "$ARCHIVE" | cut -f1)
log "Archive créée : ${ARCHIVE} (${SIZE})"

# ── 7. Rotation — conserver les 10 dernières sauvegardes ─────────────────────
KEPT=10
mapfile -t OLD_BACKUPS < <(
    ls -t "${BACKUP_BASE}"/dalybms_backup_*.tar.gz 2>/dev/null | tail -n +$(( KEPT + 1 ))
)
for OLD in "${OLD_BACKUPS[@]}"; do
    rm -f "$OLD"
    warn "  Suppression ancienne sauvegarde : $(basename "$OLD")"
done

echo ""
log "Sauvegarde terminée → ${ARCHIVE}"

#!/usr/bin/env bash
# update.sh — DalyBMS Interface — Santuario
# Mise à jour de l'application sans perte de configuration

set -euo pipefail

INSTALL_DIR="/opt/dalybms"
DATA_DIR="/data/dalybms"
BACKUP_DIR="${DATA_DIR}/backups"
ENV_FILE="${INSTALL_DIR}/.env"
VENV="${INSTALL_DIR}/venv"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Couleurs ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[update]${NC} $*"; }
info() { echo -e "${BLUE}[update]${NC} $*"; }
warn() { echo -e "${YELLOW}[update]${NC} $*"; }
fail() { echo -e "${RED}[update]${NC} $*" >&2; exit 1; }

# ── Vérifications préalables ──────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "Ce script doit être exécuté en root (sudo ./update.sh)"

[[ -d "$INSTALL_DIR" ]] || fail "Répertoire d'installation introuvable : $INSTALL_DIR"
[[ -f "$ENV_FILE"    ]] || fail "Fichier .env introuvable : $ENV_FILE"
[[ -d "$VENV"        ]] || fail "Environnement Python introuvable : $VENV"

info "═══════════════════════════════════════════════════"
info "  DalyBMS Interface — Mise à jour"
info "  Source  : ${SOURCE_DIR}"
info "  Cible   : ${INSTALL_DIR}"
info "═══════════════════════════════════════════════════"

# ── Étape 1 : sauvegarde préventive ───────────────────────────────────────────
log "Étape 1/6 — Sauvegarde préventive..."
mkdir -p "$BACKUP_DIR"
if [[ -x "${SOURCE_DIR}/backup.sh" ]]; then
    "${SOURCE_DIR}/backup.sh" "$BACKUP_DIR" && log "  ✔ Sauvegarde effectuée"
else
    # Sauvegarde minimale de la configuration
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    PRE_BACKUP="${BACKUP_DIR}/pre_update_${TIMESTAMP}"
    mkdir -p "$PRE_BACKUP"
    cp "$ENV_FILE" "${PRE_BACKUP}/.env"
    cp "${INSTALL_DIR}"/*.py "${PRE_BACKUP}/" 2>/dev/null || true
    log "  ✔ Sauvegarde minimale : ${PRE_BACKUP}"
fi

# ── Étape 2 : arrêt des services ─────────────────────────────────────────────
log "Étape 2/6 — Arrêt des services DalyBMS..."
if systemctl is-active --quiet dalybms.target 2>/dev/null; then
    systemctl stop dalybms.target
    log "  ✔ Services arrêtés"
else
    warn "  Services déjà arrêtés ou dalybms.target non trouvé"
fi

# Attendre l'arrêt effectif
sleep 3

# ── Étape 3 : mise à jour des sources Python ─────────────────────────────────
log "Étape 3/6 — Mise à jour des fichiers sources..."

PYTHON_FILES=(
    daly_protocol.py
    daly_write.py
    daly_api.py
    daly_mqtt.py
    daly_influx.py
    daly_alerts.py
)

UPDATED=0
SKIPPED=0

for FILE in "${PYTHON_FILES[@]}"; do
    SRC="${SOURCE_DIR}/${FILE}"
    DST="${INSTALL_DIR}/${FILE}"

    if [[ ! -f "$SRC" ]]; then
        warn "  Source introuvable, ignoré : ${FILE}"
        (( SKIPPED++ )) || true
        continue
    fi

    if [[ -f "$DST" ]]; then
        if ! diff -q "$SRC" "$DST" &>/dev/null; then
            cp "$SRC" "$DST"
            chown dalybms:dalybms "$DST"
            chmod 644 "$DST"
            log "  ↑ Mis à jour : ${FILE}"
            (( UPDATED++ )) || true
        else
            info "  = Inchangé  : ${FILE}"
            (( SKIPPED++ )) || true
        fi
    else
        cp "$SRC" "$DST"
        chown dalybms:dalybms "$DST"
        chmod 644 "$DST"
        log "  + Ajouté    : ${FILE}"
        (( UPDATED++ )) || true
    fi
done

log "  ✔ Sources : ${UPDATED} mis à jour, ${SKIPPED} inchangés"

# ── Étape 4 : mise à jour des dépendances Python ─────────────────────────────
log "Étape 4/6 — Mise à jour dépendances Python..."

if [[ -f "${SOURCE_DIR}/requirements.txt" ]]; then
    "${VENV}/bin/pip" install --quiet --upgrade \
        -r "${SOURCE_DIR}/requirements.txt"
    log "  ✔ Dépendances mises à jour depuis requirements.txt"
else
    # Mise à jour des packages connus
    "${VENV}/bin/pip" install --quiet --upgrade \
        fastapi \
        "uvicorn[standard]" \
        pyserial \
        serial-asyncio \
        aiomqtt \
        "influxdb-client[async]" \
        httpx \
        pydantic
    log "  ✔ Packages Python mis à jour"
fi

# ── Étape 5 : mise à jour des services systemd (si modifiés) ─────────────────
log "Étape 5/6 — Vérification services systemd..."

SYSTEMD_UPDATED=0
for SVC_FILE in "${SOURCE_DIR}"/dalybms*.service "${SOURCE_DIR}"/dalybms.target; do
    [[ -f "$SVC_FILE" ]] || continue
    BASENAME=$(basename "$SVC_FILE")
    DST_SVC="/etc/systemd/system/${BASENAME}"

    if [[ -f "$DST_SVC" ]]; then
        if ! diff -q "$SVC_FILE" "$DST_SVC" &>/dev/null; then
            cp "$SVC_FILE" "$DST_SVC"
            log "  ↑ Service mis à jour : ${BASENAME}"
            (( SYSTEMD_UPDATED++ )) || true
        fi
    else
        cp "$SVC_FILE" "$DST_SVC"
        log "  + Service ajouté : ${BASENAME}"
        (( SYSTEMD_UPDATED++ )) || true
    fi
done

if [[ $SYSTEMD_UPDATED -gt 0 ]]; then
    systemctl daemon-reload
    log "  ✔ daemon-reload effectué"
else
    info "  = Services systemd inchangés"
fi

# ── Étape 6 : redémarrage et vérification ────────────────────────────────────
log "Étape 6/6 — Redémarrage des services..."
systemctl start dalybms.target
sleep 5

# Vérification état
ALL_OK=true
for SVC in dalybms-api dalybms-mqtt dalybms-influx dalybms-alerts; do
    if systemctl is-active --quiet "$SVC" 2>/dev/null; then
        log "  ✔ ${SVC} : actif"
    else
        warn "  ✗ ${SVC} : inactif"
        ALL_OK=false
    fi
done

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
info "═══════════════════════════════════════════════════"
if $ALL_OK; then
    log "Mise à jour terminée avec succès"
    log "API disponible sur http://dalybms.local/api/v1/system/status"
else
    warn "Mise à jour terminée — certains services sont inactifs"
    warn "Consulter les logs : journalctl -u 'dalybms-*' -n 50"
fi
info "═══════════════════════════════════════════════════"

# ── Rollback en cas d'échec critique ─────────────────────────────────────────
if ! $ALL_OK; then
    echo ""
    warn "Pour rollback manuel :"
    warn "  1. systemctl stop dalybms.target"
    warn "  2. cp ${BACKUP_DIR}/pre_update_*/.env ${ENV_FILE}"
    warn "  3. cp ${BACKUP_DIR}/pre_update_*/*.py ${INSTALL_DIR}/"
    warn "  4. systemctl start dalybms.target"
fi

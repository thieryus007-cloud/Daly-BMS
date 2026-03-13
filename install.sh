#!/usr/bin/env bash
# =============================================================================
# install.sh — D10 : Déploiement DalyBMS Interface
# Raspberry Pi CM5 — Debian Bookworm / Ubuntu 24.04
# Installation Santuario — Badalucco
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BOLD}${BLUE}══ $* ${NC}"; }

# ─── Variables d'installation ─────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/dalybms}"
DATA_DIR="${DATA_DIR:-/data/dalybms}"
LOG_DIR="${LOG_DIR:-/var/log/dalybms}"
VENV_DIR="${INSTALL_DIR}/venv"
USER="${DALYBMS_USER:-dalybms}"
GROUP="${DALYBMS_GROUP:-dalybms}"
PYTHON="${PYTHON:-python3}"
NGINX_CONF="/etc/nginx/sites-available/dalybms"
ENV_FILE="${INSTALL_DIR}/.env"

# ─── Détection source ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Vérifications préalables ─────────────────────────────────────────────────
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "Ce script doit être exécuté en root (sudo ./install.sh)"
        exit 1
    fi
}

check_python() {
    if ! command -v "$PYTHON" &>/dev/null; then
        log_error "Python3 introuvable"
        exit 1
    fi
    PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    log_info "Python version : $PY_VER"
    if [[ "${PY_VER/./}" -lt "311" ]]; then
        log_error "Python >= 3.11 requis (trouvé $PY_VER)"
        exit 1
    fi
}

check_node() {
    if command -v node &>/dev/null; then
        log_info "Node.js : $(node --version) / npm : $(npm --version)"
        return
    fi
    log_info "Installation Node.js LTS..."
    if ! command -v curl &>/dev/null; then
        apt-get install -y curl
    fi
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - >/dev/null 2>&1
    apt-get install -y nodejs >/dev/null 2>&1
    log_ok "Node.js installé : $(node --version)"
}

check_uart() {
    log_info "Vérification port UART…"
    UART_PORT="${DALY_PORT:-/dev/ttyUSB1}"
    if [[ -e "$UART_PORT" ]]; then
        log_ok "Port UART trouvé : $UART_PORT"
    else
        log_warn "Port UART non trouvé : $UART_PORT — vérifier branchement USB/RS485"
        log_warn "Ports disponibles :"
        ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  Aucun port série détecté"
    fi
}

# ─── Étape 1 : Utilisateur système ───────────────────────────────────────────
create_user() {
    log_step "Création utilisateur système"
    if id "$USER" &>/dev/null; then
        log_info "Utilisateur $USER existant"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "DalyBMS Interface Service" "$USER"
        log_ok "Utilisateur $USER créé"
    fi
    # Accès port série
    usermod -aG dialout "$USER" 2>/dev/null || true
    usermod -aG tty     "$USER" 2>/dev/null || true
    log_ok "Groupes dialout/tty assignés"
}

# ─── Étape 2 : Dépendances système ───────────────────────────────────────────
install_system_deps() {
    log_step "Installation dépendances système"
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev \
        build-essential pkg-config \
        libffi-dev libssl-dev \
        nginx \
        sqlite3 \
        curl wget \
        logrotate \
        2>/dev/null
    log_ok "Dépendances système installées"
}

install_influxdb() {
    log_step "Installation InfluxDB 2.x"
    if command -v influx &>/dev/null; then
        log_info "InfluxDB déjà installé : $(influx version 2>/dev/null || echo 'version inconnue')"
        return
    fi
    # Clé + dépôt InfluxDB
    curl -fsSL https://repos.influxdata.com/influxdata-archive_compat.key \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg
    echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' \
        > /etc/apt/sources.list.d/influxdata.list
    apt-get update -qq
    apt-get install -y influxdb2 influxdb2-cli
    systemctl enable influxdb
    systemctl start  influxdb
    log_ok "InfluxDB 2.x installé et démarré"
}

install_mosquitto() {
    log_step "Installation Mosquitto MQTT"
    if command -v mosquitto &>/dev/null; then
        log_info "Mosquitto déjà installé"
        return
    fi
    apt-get install -y mosquitto mosquitto-clients
    systemctl enable mosquitto
    systemctl start  mosquitto
    log_ok "Mosquitto installé et démarré"
}

install_grafana() {
    log_step "Installation Grafana OSS"
    if command -v grafana-server &>/dev/null; then
        log_info "Grafana déjà installé"
        return
    fi
    apt-get install -y apt-transport-https software-properties-common
    curl -fsSL https://packages.grafana.com/gpg.key \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/grafana.gpg
    echo "deb [signed-by=/etc/apt/trusted.gpg.d/grafana.gpg] https://packages.grafana.com/oss/deb stable main" \
        > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq
    apt-get install -y grafana
    systemctl enable grafana-server
    systemctl start  grafana-server
    log_ok "Grafana installé et démarré (port 3000)"
}

# ─── Étape 3 : Répertoires ────────────────────────────────────────────────────
create_directories() {
    log_step "Création répertoires"
    for dir in "$INSTALL_DIR" "$DATA_DIR" "$LOG_DIR" \
               "$DATA_DIR/snapshots" "$DATA_DIR/exports"; do
        mkdir -p "$dir"
        chown "$USER:$GROUP" "$dir"
        chmod 750 "$dir"
        log_info "  $dir"
    done
    log_ok "Répertoires créés"
}

# ─── Étape 4 : Environnement Python ──────────────────────────────────────────
setup_venv() {
    log_step "Environnement Python virtuel"
    if [[ ! -d "$VENV_DIR" ]]; then
        "$PYTHON" -m venv "$VENV_DIR"
        log_ok "venv créé : $VENV_DIR"
    else
        log_info "venv existant : $VENV_DIR"
    fi

    # Mise à jour pip
    "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q

    # Dépendances Python
    "$VENV_DIR/bin/pip" install --upgrade \
        fastapi \
        uvicorn[standard] \
        pyserial \
        serial-asyncio \
        aiomqtt \
        influxdb-client[async] \
        httpx \
        pydantic \
        pydantic-settings \
        python-multipart \
        aiofiles \
        -q

    log_ok "Dépendances Python installées"
}

# ─── Étape 5 : Copie des sources ─────────────────────────────────────────────
install_sources() {
    log_step "Installation sources Python"
    MODULES=(
        daly_protocol.py
        daly_write.py
        daly_api.py
        daly_mqtt.py
        daly_influx.py
        daly_alerts.py
        daly_venus.py
    )
    MISSING=()
    for mod in "${MODULES[@]}"; do
        SRC="$SCRIPT_DIR/$mod"
        if [[ -f "$SRC" ]]; then
            cp "$SRC" "$INSTALL_DIR/$mod"
            chown "$USER:$GROUP" "$INSTALL_DIR/$mod"
            chmod 640 "$INSTALL_DIR/$mod"
            log_info "  Installé : $mod"
        else
            MISSING+=("$mod")
            log_warn "  Manquant : $mod"
        fi
    done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        log_warn "Modules manquants : ${MISSING[*]}"
        log_warn "Copier manuellement dans $INSTALL_DIR/"
    fi
    log_ok "Sources installées"
}

# ─── Étape 6 : Fichier .env ───────────────────────────────────────────────────
create_env_file() {
    log_step "Fichier de configuration .env"
    if [[ -f "$ENV_FILE" ]]; then
        log_info ".env existant — sauvegarde → .env.bak"
        cp "$ENV_FILE" "${ENV_FILE}.bak"
    fi

    cat > "$ENV_FILE" <<'EOF'
# =============================================================================
# DalyBMS Interface — Configuration
# Santuario — Badalucco
# =============================================================================

# ── UART / BMS ───────────────────────────────────────────────────────────────
DALY_PORT=/dev/ttyUSB1
DALY_BAUD=9600
DALY_POLL_INTERVAL=1.0
DALY_CELL_COUNT=16
DALY_SENSOR_COUNT=4

# Adresses BMS sur le bus RS485 — ajouter les adresses supplémentaires ici
# RS485 supporte jusqu'à 32 BMS par segment — protocole Daly : 255 adresses
# Pour 3 BMS : DALY_ADDRESSES=0x01,0x02,0x03
# Pour 4 BMS : DALY_ADDRESSES=0x01,0x02,0x03,0x04
DALY_ADDRESSES=0x01,0x02
# Découverte automatique (0=désactivé, 1=activer pour détecter nouveaux BMS)
DALY_AUTO_DISCOVER=0

BMS1_CAPACITY_AH=320
BMS2_CAPACITY_AH=360
BMS3_CAPACITY_AH=0
BMS4_CAPACITY_AH=0
BMS1_PRODUCT_NAME=Daly LiFePO4 320Ah
BMS2_PRODUCT_NAME=Daly LiFePO4 360Ah
BMS3_PRODUCT_NAME=Daly LiFePO4 XXXAh
BMS4_PRODUCT_NAME=Daly LiFePO4 XXXAh
BMS1_NAME=Pack 320Ah
BMS2_NAME=Pack 360Ah
BMS3_NAME=Pack BMS 3
BMS4_NAME=Pack BMS 4

# ── MQTT local (Mosquitto RPi CM5) ───────────────────────────────────────────
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_PREFIX=santuario/bms
MQTT_CLIENT_ID=dalybms-publisher
MQTT_QOS_DATA=0
MQTT_QOS_ALARM=1
MQTT_INTERVAL=5.0

# ── MQTT Bridge (republication topics vers NanoPi, indépendant de daly_venus.py) ─
MQTT_BRIDGE_ENABLED=false
MQTT_BRIDGE_HOST=192.168.1.120
MQTT_BRIDGE_PORT=1883
MQTT_BRIDGE_PREFIX=santuario/bms

# ── MQTT NanoPi (Venus OS bridge) ────────────────────────────────────────────
NANOPI_MQTT_HOST=192.168.1.120
NANOPI_MQTT_PORT=1883
NANOPI_CLIENT_ID=dalybms-venus-bridge
VENUS_PORTAL_ID=c0619ab9929a
VENUS_BMS1_INSTANCE=10
VENUS_BMS2_INSTANCE=11
VENUS_METEO_INSTANCE=20
VENUS_PUBLISH_INTERVAL=5.0

# ── InfluxDB ─────────────────────────────────────────────────────────────────
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=CHANGE_ME
INFLUX_ORG=santuario
INFLUX_BUCKET=daly_bms
INFLUX_BUCKET_DS=daly_bms_1m
INFLUX_BATCH_SIZE=50
INFLUX_BATCH_INTERVAL=5
INFLUX_RETENTION_DAYS=30

# ── API FastAPI ───────────────────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8000
API_WORKERS=1
# Laisser vide pour désactiver l'auth
DALY_API_KEY=

# ── Bridges intégrés dans dalybms-api ────────────────────────────────────────
# Mettre à 1 pour activer MQTT dans le processus API (évite dalybms-mqtt.service)
MQTT_ENABLED=0
# InfluxDB activé automatiquement si INFLUX_TOKEN est renseigné

# ── Alertes ───────────────────────────────────────────────────────────────────
ALERT_DB_PATH=/data/dalybms/alerts.db
ALERT_CHECK_INTERVAL=1.0

# Telegram (laisser vide si non utilisé)
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# Email (laisser vide si non utilisé)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=daly-bms@santuario.local
SMTP_TO=

# Seuils alertes logicielles
ALERT_CELL_OVP_V=3.60
ALERT_CELL_OVP_CLR_V=3.55
ALERT_CELL_UVP_V=2.90
ALERT_CELL_UVP_CLR_V=2.95
ALERT_CELL_DELTA_MV=100
ALERT_CELL_DELTA_CLR=80
ALERT_SOC_LOW=20.0
ALERT_SOC_CRITICAL=10.0
ALERT_TEMP_HIGH_C=45.0
ALERT_CURRENT_HIGH_A=80.0
ALERT_SOC_LOW_CLR=25.0
ALERT_SOC_CRITICAL_CLR=12.0
ALERT_TEMP_HIGH_CLR_C=40.0
ALERT_CURRENT_HIGH_CLR=70.0
EOF

    chown "$USER:$GROUP" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    log_ok ".env créé : $ENV_FILE"
    log_warn "Éditer $ENV_FILE avant de démarrer les services"
}

# ─── Étape 6b : Build dashboard React ────────────────────────────────────────
build_dashboard() {
    log_step "Build dashboard React"
    DASHBOARD_DIR="$SCRIPT_DIR/dashboard"
    FRONTEND_DIR="$INSTALL_DIR/frontend"

    if [[ ! -d "$DASHBOARD_DIR" ]]; then
        log_warn "Répertoire dashboard/ introuvable — ignoré"
        return
    fi

    check_node

    log_info "npm install…"
    (cd "$DASHBOARD_DIR" && npm install --prefer-offline 2>&1 | tail -3)

    log_info "npm run build…"
    (cd "$DASHBOARD_DIR" && npm run build 2>&1 | tail -5)

    # Le vite.config.js place le dist dans $SCRIPT_DIR/dist (outDir: '../dist')
    DIST_SRC="$SCRIPT_DIR/dist"
    if [[ ! -d "$DIST_SRC" ]]; then
        # Fallback : dist dans dashboard/dist
        DIST_SRC="$DASHBOARD_DIR/dist"
    fi

    if [[ ! -d "$DIST_SRC" ]]; then
        log_warn "Build dashboard introuvable — dashboard non déployé"
        return
    fi

    mkdir -p "$FRONTEND_DIR"
    rm -rf "$FRONTEND_DIR/dist"
    cp -r "$DIST_SRC" "$FRONTEND_DIR/dist"
    chown -R "$USER:$GROUP" "$FRONTEND_DIR"
    chmod -R 755 "$FRONTEND_DIR/dist"
    log_ok "Dashboard déployé : $FRONTEND_DIR/dist"
}

# ─── Étape 7 : Services systemd ───────────────────────────────────────────────
install_systemd_services() {
    log_step "Installation services systemd"

    # ── 7.1 dalybms-api.service ──────────────────────────────────────────────
    cat > /etc/systemd/system/dalybms-api.service <<EOF
[Unit]
Description=DalyBMS API Service (FastAPI + WebSocket)
Documentation=https://github.com/santuario/dalybms
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn daly_api:app \\
    --host \${API_HOST:-0.0.0.0} \\
    --port \${API_PORT:-8000} \\
    --workers \${API_WORKERS:-1} \\
    --no-access-log \\
    --log-level info
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dalybms-api

# Sécurité
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=${DATA_DIR} ${LOG_DIR}
ProtectHome=yes
PrivateTmp=yes
SupplementaryGroups=dialout tty

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms-api.service"

    # ── 7.2 dalybms-mqtt.service ─────────────────────────────────────────────
    cat > /etc/systemd/system/dalybms-mqtt.service <<EOF
[Unit]
Description=DalyBMS MQTT Publisher
After=network.target mosquitto.service dalybms-api.service
Wants=mosquitto.service
BindsTo=dalybms-api.service

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python -m daly_mqtt
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dalybms-mqtt

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
SupplementaryGroups=dialout tty

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms-mqtt.service"

    # ── 7.3 dalybms-influx.service ───────────────────────────────────────────
    cat > /etc/systemd/system/dalybms-influx.service <<EOF
[Unit]
Description=DalyBMS InfluxDB Writer
After=network.target influxdb.service dalybms-api.service
Wants=influxdb.service

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python -m daly_influx
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dalybms-influx

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms-influx.service"

    # ── 7.4 dalybms-alerts.service ───────────────────────────────────────────
    cat > /etc/systemd/system/dalybms-alerts.service <<EOF
[Unit]
Description=DalyBMS Alert Engine
After=network.target dalybms-api.service
BindsTo=dalybms-api.service

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python -m daly_alerts
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dalybms-alerts

NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms-alerts.service"

    # ── 7.5 dalybms-venus.service ────────────────────────────────────────────
    cat > /etc/systemd/system/dalybms-venus.service <<EOF
[Unit]
Description=DalyBMS Venus OS Bridge (MQTT → dbus-mqtt-devices)
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python daly_venus.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dalybms-venus

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms-venus.service"

    # ── 7.6 dalybms.target (groupe) ──────────────────────────────────────────
    cat > /etc/systemd/system/dalybms.target <<EOF
[Unit]
Description=DalyBMS Interface — Tous les services
After=network.target
Wants=dalybms-api.service dalybms-mqtt.service dalybms-influx.service dalybms-alerts.service dalybms-venus.service

[Install]
WantedBy=multi-user.target
EOF
    log_info "  dalybms.target"

    systemctl daemon-reload
    log_ok "Services systemd installés"
}

# ─── Étape 8 : Nginx ─────────────────────────────────────────────────────────
install_nginx() {
    log_step "Configuration Nginx"

    API_PORT="${API_PORT:-8000}"

    cat > "$NGINX_CONF" <<EOF
# DalyBMS Interface — Santuario
# Reverse proxy FastAPI + WebSocket + Grafana

upstream dalybms_api {
    server 127.0.0.1:${API_PORT};
    keepalive 32;
}

upstream grafana {
    server 127.0.0.1:3000;
    keepalive 8;
}

# ── Redirect HTTP → HTTPS (optionnel — décommenter si certificat disponible)
# server {
#     listen 80;
#     server_name dalybms.santuario.local;
#     return 301 https://\$host\$request_uri;
# }

server {
    listen 80;
    listen [::]:80;
    server_name dalybms.santuario.local dalybms _;

    # Sécurité headers
    add_header X-Frame-Options           SAMEORIGIN;
    add_header X-Content-Type-Options    nosniff;
    add_header X-XSS-Protection         "1; mode=block";
    add_header Referrer-Policy           strict-origin-when-cross-origin;

    # ── API REST ──────────────────────────────────────────────────────────────
    location /api/ {
        proxy_pass         http://dalybms_api;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
        proxy_buffering    off;
    }

    # ── WebSocket BMS stream ──────────────────────────────────────────────────
    location /ws/ {
        proxy_pass          http://dalybms_api;
        proxy_http_version  1.1;
        proxy_set_header    Upgrade    \$http_upgrade;
        proxy_set_header    Connection "upgrade";
        proxy_set_header    Host       \$host;
        proxy_read_timeout  3600s;
        proxy_send_timeout  3600s;
    }

    # ── Server-Sent Events (SSE) ──────────────────────────────────────────────
    location ~ ^/api/v1/bms/[0-9]+/sse$ {
        proxy_pass         http://dalybms_api;
        proxy_http_version 1.1;
        proxy_set_header   Connection '';
        proxy_set_header   Cache-Control 'no-cache';
        proxy_set_header   X-Accel-Buffering no;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }

    # ── OpenAPI docs ──────────────────────────────────────────────────────────
    location ~ ^/(docs|redoc|openapi.json)$ {
        proxy_pass       http://dalybms_api;
        proxy_set_header Host \$host;
    }

    # ── Interface Web React (SPA) ─────────────────────────────────────────────
    location / {
        root  ${INSTALL_DIR}/frontend/dist;
        index index.html;
        try_files \$uri \$uri/ /index.html;

        # Cache assets statiques
        location ~* \.(js|css|png|svg|ico|woff2?)$ {
            expires 7d;
            add_header Cache-Control "public, immutable";
        }
    }

    # ── Grafana (sous-chemin /grafana/) ───────────────────────────────────────
    location /grafana/ {
        proxy_pass         http://grafana/;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        # WebSocket Grafana Live
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection \$connection_upgrade;
        proxy_read_timeout 3600s;
    }

    # ── Health check ──────────────────────────────────────────────────────────
    location /health {
        access_log off;
        proxy_pass http://dalybms_api/api/v1/system/status;
    }

    # Logs
    access_log /var/log/nginx/dalybms_access.log combined;
    error_log  /var/log/nginx/dalybms_error.log warn;
}
EOF

    # Map WebSocket upgrade (bloc http global)
    NGINX_MAIN="/etc/nginx/nginx.conf"
    if ! grep -q "connection_upgrade" "$NGINX_MAIN" 2>/dev/null; then
        # Insérer dans le bloc http {} si absent
        sed -i '/http {/a\    map $http_upgrade $connection_upgrade {\n        default upgrade;\n        '\'''\'' close;\n    }' "$NGINX_MAIN"
        log_info "  Map WebSocket ajouté dans nginx.conf"
    fi

    # Activation vhost
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/dalybms 2>/dev/null || true
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

    nginx -t && log_ok "Configuration Nginx valide" || {
        log_error "Erreur configuration Nginx — vérifier $NGINX_CONF"
        return 1
    }

    systemctl enable nginx
    systemctl reload nginx 2>/dev/null || systemctl start nginx
    log_ok "Nginx configuré (port 80)"
}

# ─── Étape 9 : Logrotate ──────────────────────────────────────────────────────
install_logrotate() {
    log_step "Configuration logrotate"
    cat > /etc/logrotate.d/dalybms <<EOF
${LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 ${USER} ${GROUP}
    postrotate
        systemctl kill -s HUP dalybms-api.service 2>/dev/null || true
    endscript
}
EOF
    log_ok "Logrotate configuré"
}

# ─── Étape 10 : Mosquitto config ─────────────────────────────────────────────
configure_mosquitto() {
    log_step "Configuration Mosquitto"
    MOSQ_CONF="/etc/mosquitto/conf.d/dalybms.conf"
    cat > "$MOSQ_CONF" <<'EOF'
# DalyBMS — Santuario
# Broker local RPi CM5 — port 1883

listener 1883
allow_anonymous true
max_queued_messages 1000
persistence true
persistence_location /var/lib/mosquitto/
log_type error
log_type warning
log_type information
EOF
    systemctl restart mosquitto 2>/dev/null || true
    log_ok "Mosquitto configuré"
}

# ─── Étape 11 : InfluxDB setup ───────────────────────────────────────────────
configure_influxdb() {
    log_step "Configuration InfluxDB"
    log_info "InfluxDB setup initial — interface web : http://localhost:8086"
    log_info "Créer manuellement :"
    log_info "  Organisation : santuario"
    log_info "  Bucket       : daly_bms (retention 30j)"
    log_info "  Bucket       : daly_bms_1m (retention 365j)"
    log_info "  Token API    → copier dans ${ENV_FILE} (INFLUX_TOKEN)"
    log_warn "Puis relancer : systemctl restart dalybms-influx"
}

# ─── Étape 12 : Activation services ──────────────────────────────────────────
enable_services() {
    log_step "Activation services systemd"

    # Services core — démarrent automatiquement
    systemctl enable dalybms-api
    systemctl enable dalybms-venus
    log_info "  Activé : dalybms-api (inclut AlertBridge + bridges MQTT/Influx si configurés)"
    log_info "  Activé : dalybms-venus"

    # Services standalone (désactivés par défaut — bridges intégrés dans dalybms-api)
    # Activer manuellement si déploiement multi-processus souhaité :
    #   systemctl enable dalybms-mqtt dalybms-influx dalybms-alerts
    log_info "  Note : dalybms-mqtt / dalybms-influx / dalybms-alerts sont désactivés"
    log_info "         Les bridges sont intégrés dans dalybms-api (MQTT_ENABLED, INFLUX_TOKEN)"

    log_ok "Services activés (démarrage automatique au boot)"
    log_warn "Démarrage différé — configurer .env en premier"
}

# ─── Résumé final ─────────────────────────────────────────────────────────────
print_summary() {
    echo
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  DalyBMS Interface installé — Santuario${NC}"
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
    echo
    echo -e "  ${CYAN}Répertoire${NC}  : $INSTALL_DIR"
    echo -e "  ${CYAN}Données${NC}     : $DATA_DIR"
    echo -e "  ${CYAN}Config${NC}      : $ENV_FILE"
    echo
    echo -e "  ${YELLOW}Étapes suivantes :${NC}"
    echo
    echo -e "  1. Éditer la configuration :"
    echo -e "     ${BOLD}nano ${ENV_FILE}${NC}"
    echo
    echo -e "  2. Configurer InfluxDB (http://localhost:8086)"
    echo -e "     puis copier le token dans INFLUX_TOKEN"
    echo
    echo -e "  3. Démarrer les services :"
    echo -e "     ${BOLD}systemctl start dalybms.target${NC}"
    echo
    echo -e "  4. Vérifier l'état :"
    echo -e "     ${BOLD}systemctl status dalybms-api${NC}"
    echo -e "     ${BOLD}journalctl -u dalybms-api -f${NC}"
    echo
    echo -e "  5. Interfaces :"
    echo -e "     Dashboard : ${CYAN}http://dalybms.local/${NC}"
    echo -e "     API REST  : ${CYAN}http://dalybms.local/api/v1/system/status${NC}"
    echo -e "     Docs API  : ${CYAN}http://dalybms.local/docs${NC}"
    echo -e "     Alertes   : ${CYAN}http://dalybms.local/api/v1/alerts/active${NC}"
    echo -e "     Grafana   : ${CYAN}http://dalybms.local/grafana/${NC}"
    echo -e "     InfluxDB  : ${CYAN}http://dalybms.local:8086${NC}"
    echo
    echo -e "  6. Venus OS Bridge — vérification :"
    echo -e "     ${BOLD}cd $INSTALL_DIR && ${VENV_DIR}/bin/python daly_venus.py check${NC}"
    echo
    echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════${NC}"
}

# ─── Désinstallation ─────────────────────────────────────────────────────────
uninstall() {
    log_step "Désinstallation DalyBMS"
    SERVICES=(dalybms-api dalybms-mqtt dalybms-influx dalybms-alerts dalybms-venus)
    for svc in "${SERVICES[@]}"; do
        systemctl stop    "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
    done
    rm -f /etc/systemd/system/dalybms.target
    systemctl daemon-reload
    rm -f /etc/nginx/sites-enabled/dalybms
    rm -f /etc/nginx/sites-available/dalybms
    systemctl reload nginx 2>/dev/null || true
    rm -f /etc/logrotate.d/dalybms
    log_warn "Répertoire $INSTALL_DIR conservé — supprimer manuellement si souhaité"
    log_warn "Données $DATA_DIR conservées"
    log_ok "Désinstallation terminée"
}

# ─── Point d'entrée ───────────────────────────────────────────────────────────
main() {
    echo -e "${BOLD}${BLUE}"
    echo "  ██████╗  █████╗ ██╗  ██╗   ██╗██████╗ ███╗   ███╗███████╗"
    echo "  ██╔══██╗██╔══██╗██║  ╚██╗ ██╔╝██╔══██╗████╗ ████║██╔════╝"
    echo "  ██║  ██║███████║██║   ╚████╔╝ ██████╔╝██╔████╔██║███████╗"
    echo "  ██║  ██║██╔══██║██║    ╚██╔╝  ██╔══██╗██║╚██╔╝██║╚════██║"
    echo "  ██████╔╝██║  ██║███████╗██║   ██████╔╝██║ ╚═╝ ██║███████║"
    echo "  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝   ╚═════╝ ╚═╝     ╚═╝╚══════╝"
    echo -e "${NC}${CYAN}  Installation Santuario — Badalucco (Ligurie)${NC}"
    echo

    case "${1:-install}" in
        install)
            check_root
            check_python
            check_uart
            install_system_deps
            install_influxdb
            install_mosquitto
            install_grafana
            create_user
            create_directories
            setup_venv
            install_sources
            build_dashboard
            create_env_file
            install_systemd_services
            configure_mosquitto
            install_nginx
            install_logrotate
            configure_influxdb
            enable_services
            print_summary
            ;;
        uninstall)
            check_root
            uninstall
            ;;
        update)
            check_root
            install_sources
            build_dashboard
            setup_venv
            systemctl restart dalybms-api 2>/dev/null || true
            log_ok "Mise à jour terminée"
            ;;
        status)
            for svc in dalybms-api dalybms-mqtt dalybms-influx dalybms-alerts dalybms-venus; do
                echo -e "${CYAN}── $svc${NC}"
                systemctl status "$svc" --no-pager -l 2>/dev/null | tail -5 || true
            done
            ;;
        check-uart)
            check_uart
            ;;
        *)
            echo "Usage: $0 {install|uninstall|update|status|check-uart}"
            exit 1
            ;;
    esac
}

main "$@"
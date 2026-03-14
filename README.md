# DalyBMS Interface — Santuario

Interface complète de monitoring et contrôle pour batteries LiFePO4 Daly Smart BMS.
Conçue pour le Raspberry Pi CM5, installation solaire Santuario.

```
Pack A (BMS 0x01) ──┐
Pack B (BMS 0x02) ──┤
Pack C (BMS 0x03) ──┼── RS485/USB ── RPi CM5 ── FastAPI ── Dashboard React
Pack D (BMS 0x04) ──┘      [natif]                 │          [natif]
  (jusqu'à 32)                                     │
                                                   ├── Mosquitto  ─┐
                                                   ├── InfluxDB    ├─ Docker
                                                   ├── Grafana     │  (Phase 1)
                                                   ├── Node-RED  ──┘
                                                   ├── Alertes (Telegram/Email)
                                                   └── Venus OS Bridge
```

> **Stratégie de déploiement en deux phases :**
> - **Phase 1 (actuelle)** — Infrastructure (Mosquitto, InfluxDB, Grafana, Node-RED) dans Docker. Scripts Python et Dashboard tournent nativement sur le Pi.
> - **Phase 2 (après validation)** — Stack complète dans Docker, y compris `daly_api` et le Dashboard.

> **RS485** supporte jusqu'à 32 BMS par segment (standard TIA-485) et 255 adresses selon
> le protocole Daly. En pratique, 4 BMS sur un seul adaptateur USB/RS485 fonctionne
> parfaitement à 9600 baud.

---

## Table des matières

- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Infrastructure Docker — Phase 1](#infrastructure-docker--phase-1)
- [Installation rapide](#installation-rapide)
- [Configuration](#configuration)
- [Démarrage](#démarrage)
- [Dashboard](#dashboard)
- [API REST](#api-rest)
- [Alertes](#alertes)
- [Développement local](#développement-local)
- [Ajouter un BMS / Découverte automatique](#ajouter-un-bms--découverte-automatique)
- [Dépannage](#dépannage)

---

## Architecture

| Module | Rôle |
|--------|------|
| `daly_protocol.py` | Protocole Daly UART — parsing trames, BmsSnapshot |
| `daly_write.py` | Commandes d'écriture BMS — MOS, SOC, protections |
| `daly_api.py` | API REST FastAPI + WebSocket + orchestration des bridges |
| `daly_mqtt.py` | Publication MQTT vers Mosquitto |
| `daly_influx.py` | Écriture InfluxDB 2.x avec batch et downsampling 1min |
| `daly_alerts.py` | Moteur d'alertes — hysteresis, journal SQLite, notifications |
| `dashboard/` | SPA React — monitoring temps réel via WebSocket |

### Flux de données

```
BMS UART ──► DalyWriteManager.poll_loop()
                │
                ▼ _on_snapshot(snaps)
         ┌──────┴───────┐
         │              │
         ▼              ▼
   state.snapshots    Bridges (en parallèle)
   state.ring         ├── AlertBridge  → SQLite + Telegram/Email
   WebSocket push     ├── MqttBridge   → Mosquitto (si MQTT_ENABLED=1)
                      └── InfluxBridge → InfluxDB  (si INFLUX_TOKEN défini)
```

---

## Prérequis

- **Matériel** : Raspberry Pi CM5 (ou Pi 4/5) + adaptateur USB/RS485
- **OS** : Debian Bookworm ou Ubuntu 24.04
- **Python** : 3.11+
- **Docker** : 24+ avec Docker Compose v2 (`docker compose`)
- **Node.js** : 20 LTS (dashboard dev uniquement)
- **Connexion BMS** : port RS485 sur `/dev/ttyUSB1` (ou `/dev/ttyUSB0`)

---

## Infrastructure Docker — Phase 1

Mosquitto, InfluxDB, Grafana et Node-RED tournent dans des containers Docker.
Les ports sont exposés sur l'hôte : les scripts Python natifs se connectent à
`localhost:1883` / `localhost:8086` **sans aucun changement de `.env`**.

### Démarrage rapide

```bash
# 1. Copier et personnaliser les credentials Docker
cp .env.docker.example .env.docker
nano .env.docker          # changer les mots de passe et le token InfluxDB

# 2. Démarrer la stack
make up
```

### Commandes disponibles

| Commande | Action |
|----------|--------|
| `make up` | Démarrer tous les services |
| `make down` | Arrêter (données conservées) |
| `make restart` | Redémarrer |
| `make logs` | Logs temps réel de tous les services |
| `make logs-grafana` | Logs d'un service spécifique |
| `make ps` | État des containers |
| `make pull` | Mettre à jour les images |
| `make reset` | ⚠ Supprimer containers ET volumes |

### Services et ports

| Service | URL / Port | Description |
|---------|-----------|-------------|
| **Mosquitto** | `localhost:1883` | MQTT broker |
| **Mosquitto WS** | `localhost:9001` | MQTT over WebSocket (Node-RED) |
| **InfluxDB** | `http://localhost:8086` | Time-series, console admin |
| **Grafana** | `http://localhost:3001` | Dashboards (dashboard BMS pré-chargé) |
| **Node-RED** | `http://localhost:1880` | Automatisation et flows |

> Le port Grafana est **3001** (le 3000 est réservé au serveur de dev Vite).

### Initialisation InfluxDB

Au premier `make up`, InfluxDB se configure automatiquement (org, bucket, token)
à partir des valeurs dans `.env.docker`. Aucune action manuelle requise.

### Grafana

Le dashboard `daly_bms_grafana.json` est automatiquement provisionné au démarrage.
La datasource InfluxDB est également pré-configurée — accès immédiat sans configuration manuelle.

Identifiants par défaut (modifiables dans `.env.docker`) :
```
URL      : http://localhost:3001
Login    : admin
Password : (valeur GRAFANA_PASSWORD dans .env.docker)
```

### Fichiers de configuration Docker

```
docker-compose.infra.yml                  ← stack principale
.env.docker.example                       ← template credentials (copier en .env.docker)
Makefile                                  ← commandes make
docker/
  mosquitto/mosquitto.conf                ← MQTT listener + WebSocket + persistance
  grafana/provisioning/
    datasources/influxdb.yml              ← datasource InfluxDB auto-configurée
    dashboards/provider.yml               ← chargement auto du dashboard JSON
```

### Phase 2 — Intégration future (après validation Daly + Dashboard)

Quand les tests BMS et le dashboard React seront validés, on ajoutera :
- `Dockerfile` pour les scripts Python (`daly_api`, bridges)
- `Dockerfile` multi-stage pour le dashboard (build Node.js → Nginx)
- Accès port série `/dev/ttyUSB0` via `devices:` dans Compose
- Fusion en `docker-compose.yml` unique avec `make up` global

---

## Installation rapide

```bash
# 1. Cloner le dépôt
git clone https://github.com/santuario/dalybms /opt/dalybms-src
cd /opt/dalybms-src

# 2. Lancer l'installation (root requis)
sudo ./install.sh

# 3. Éditer la configuration
sudo nano /opt/dalybms/.env

# 4. Démarrer
sudo systemctl start dalybms-api
```

Le script `install.sh` réalise automatiquement :
- Installation des dépendances système (Python, nginx, Node.js, InfluxDB, Mosquitto, Grafana)
- Création de l'utilisateur système `dalybms`
- Virtualenv Python + installation des packages
- **Build du dashboard React** (`npm install && npm run build`)
- Déploiement dans `/opt/dalybms/frontend/dist/` (servi par nginx)
- Génération du fichier `.env`
- Installation des services systemd
- Configuration nginx (reverse proxy API + WebSocket + SPA)

---

## Configuration

Tous les paramètres sont dans `/opt/dalybms/.env` (copie de `.env.example`).

### UART / BMS

| Variable | Défaut | Description |
|----------|--------|-------------|
| `DALY_PORT` | `/dev/ttyUSB1` | Port série RS485 |
| `DALY_POLL_INTERVAL` | `1.0` | Intervalle de polling en secondes |
| `DALY_CELL_COUNT` | `16` | Nombre de cellules en série |
| `DALY_SENSOR_COUNT` | `4` | Nombre de sondes NTC |
| `DALY_RING_SIZE` | `3600` | Taille du ring buffer (points en mémoire) |

### API

| Variable | Défaut | Description |
|----------|--------|-------------|
| `API_HOST` | `0.0.0.0` | Interface d'écoute uvicorn |
| `API_PORT` | `8000` | Port uvicorn |
| `DALY_API_KEY` | *(vide)* | Clé API (header `X-API-Key`). Vide = pas d'auth |

### Bridges intégrés dans `dalybms-api`

| Variable | Défaut | Description |
|----------|--------|-------------|
| `MQTT_ENABLED` | `0` | `1` = démarre MqttBridge dans le processus API |
| `INFLUX_TOKEN` | *(vide)* | Token InfluxDB — InfluxBridge démarré automatiquement si renseigné |

### MQTT

| Variable | Défaut | Description |
|----------|--------|-------------|
| `MQTT_HOST` | `localhost` | Broker MQTT |
| `MQTT_PORT` | `1883` | Port broker |
| `MQTT_PREFIX` | `santuario/bms` | Préfixe des topics |
| `MQTT_INTERVAL` | `5.0` | Intervalle de publication (secondes) |

### InfluxDB

| Variable | Défaut | Description |
|----------|--------|-------------|
| `INFLUX_URL` | `http://localhost:8086` | URL InfluxDB |
| `INFLUX_TOKEN` | *(vide)* | Token API InfluxDB |
| `INFLUX_ORG` | `santuario` | Organisation |
| `INFLUX_BUCKET` | `daly_bms` | Bucket principal (rétention 30j) |
| `INFLUX_BUCKET_DS` | `daly_bms_1m` | Bucket downsampled 1min (rétention 365j) |
| `INFLUX_BATCH_SIZE` | `50` | Taille du batch d'écriture |
| `INFLUX_BATCH_INTERVAL` | `5.0` | Flush forcé toutes les N secondes |

### Alertes

| Variable | Défaut | Description |
|----------|--------|-------------|
| `ALERT_DB_PATH` | `/data/dalybms/alerts.db` | Base SQLite journal des alertes |
| `ALERT_CHECK_INTERVAL` | `1.0` | Intervalle d'évaluation (secondes) |
| `TELEGRAM_TOKEN` | *(vide)* | Token bot Telegram (vide = désactivé) |
| `TELEGRAM_CHAT_ID` | *(vide)* | Chat ID destinataire |
| `SMTP_HOST` | *(vide)* | Serveur SMTP (vide = email désactivé) |
| `SMTP_TO` | *(vide)* | Adresse destinataire |

**Seuils alertes logicielles** (indépendants des seuils hardware BMS) :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `ALERT_CELL_OVP_V` | `3.60` | Surtension cellule (V) |
| `ALERT_CELL_UVP_V` | `2.90` | Sous-tension cellule (V) |
| `ALERT_CELL_DELTA_MV` | `100` | Déséquilibre max (mV) |
| `ALERT_SOC_LOW` | `20.0` | SOC faible (%) |
| `ALERT_SOC_CRITICAL` | `10.0` | SOC critique (%) |
| `ALERT_TEMP_HIGH_C` | `45.0` | Température haute (°C) |
| `ALERT_CURRENT_HIGH_A` | `80.0` | Courant de charge élevé (A) |

---

## Démarrage

```bash
# Démarrer l'API (inclut AlertBridge + MQTT/Influx si configurés)
sudo systemctl start dalybms-api

# Démarrer le bridge Venus OS (optionnel)
sudo systemctl start dalybms-venus

# Démarrer tous les services
sudo systemctl start dalybms.target

# Vérifier l'état
sudo systemctl status dalybms-api
journalctl -u dalybms-api -f

# Tester l'API
curl http://localhost:8000/api/v1/system/status
```

### Ordre de démarrage recommandé

```
1. make up             → Mosquitto, InfluxDB, Grafana, Node-RED (Docker)
2. systemctl start dalybms-api   → API + AlertBridge + MqttBridge + InfluxBridge
3. systemctl start dalybms-venus → Bridge Venus OS (optionnel)
```

> **Note :** Depuis la Phase 3, les bridges MQTT, InfluxDB et Alertes s'exécutent
> **dans le même processus** que `dalybms-api`. Les services `dalybms-mqtt`,
> `dalybms-influx` et `dalybms-alerts` existent toujours mais ne sont pas activés
> par défaut (ils peuvent être utilisés pour un déploiement multi-processus manuel).

---

## Dashboard

Interface React accessible via nginx sur `http://dalybms.local/`.

### Pages disponibles

| Page | Description |
|------|-------------|
| **Dashboard** | SOC gauge, tension/courant/puissance, sparklines 3min |
| **Cellules** | Tensions individuelles 16 cellules, range chart, balancing |
| **Températures** | 4 sondes NTC, historique, statut thermique |
| **Alarmes** | Flags hardware + alertes logicielles + journal SQLite temps réel |
| **Contrôle** | Commandes MOS CHG/DSG, calibration SOC, reset |
| **Config** | Paramètres de protection (tension, courant, temp, balancing) |
| **Dual BMS** | Vue comparée BMS 1 vs BMS 2 |
| **Stats** | Statistiques session, énergie, santé estimée |

### Connexion WebSocket

Le dashboard se connecte automatiquement à `ws://{host}/ws/bms/stream`.
- Reconnexion automatique toutes les 3 secondes en cas de coupure
- État initial = données mock (indicateur "OFF" en haut à droite)
- Indicateur "LIVE" dès que la connexion WebSocket est établie

### Build manuel (développement)

```bash
cd dashboard
npm install
npm run dev          # Serveur dev sur :3000 avec proxy vers API :8000
npm run build        # Build production → dist/
```

---

## API REST

Documentation interactive : `http://dalybms.local/docs`

### Authentification

Si `DALY_API_KEY` est défini, ajouter le header `X-API-Key: <valeur>` à chaque requête.

### Endpoints principaux

#### Monitoring

```http
GET /api/v1/system/status
GET /api/v1/bms/{id}/status          # Snapshot complet
GET /api/v1/bms/{id}/cells           # Tensions cellules
GET /api/v1/bms/{id}/temperatures    # Sondes NTC
GET /api/v1/bms/{id}/alarms          # Flags hardware
GET /api/v1/bms/{id}/mos             # État MOSFET
GET /api/v1/bms/{id}/history?duration=1h
GET /api/v1/bms/{id}/history/summary
GET /api/v1/bms/compare              # Comparaison BMS 1 vs 2
```

#### Contrôle (POST — requiert API key si configurée)

```http
POST /api/v1/bms/{id}/mos            # {"chg": true, "dsg": true}
POST /api/v1/bms/{id}/soc            # {"value": 75.0}
POST /api/v1/bms/{id}/soc/full
POST /api/v1/bms/{id}/soc/empty
POST /api/v1/bms/{id}/reset          # {"confirm": "CONFIRM_RESET"}
```

#### Configuration (POST)

```http
POST /api/v1/bms/{id}/config/ovp/cell     # {"trigger_v": 3.65, "recovery_v": 3.60}
POST /api/v1/bms/{id}/config/uvp/cell
POST /api/v1/bms/{id}/config/ocp/charge   # {"current_a": 70, "delay_ms": 1000}
POST /api/v1/bms/{id}/config/ocp/discharge
POST /api/v1/bms/{id}/config/balancing    # {"enabled": true, "trigger_voltage_v": 3.40}
POST /api/v1/bms/{id}/config/pack         # {"capacity_ah": 320, "cell_count": 16}
POST /api/v1/bms/{id}/config/full         # Profil complet en une requête
POST /api/v1/bms/{id}/config/preset/santuario_320ah
POST /api/v1/bms/{id}/config/preset/santuario_360ah
```

#### Alertes

```http
GET  /api/v1/alerts/active
GET  /api/v1/alerts/history?bms_id=1&limit=100
GET  /api/v1/alerts/counters
GET  /api/v1/alerts/rules
GET  /api/v1/alerts/states
POST /api/v1/alerts/snooze/{bms_id}/{rule_name}   # {"duration_s": 3600}
DELETE /api/v1/alerts/snooze/{bms_id}/{rule_name}
```

#### Streaming

```http
WebSocket  /ws/bms/stream              # Tous BMS — {"type":"snapshot","data":{1:{...},2:{...}}}
WebSocket  /ws/bms/{id}/stream         # Un seul BMS
GET        /api/v1/bms/{id}/sse        # Server-Sent Events (alternative)
GET        /api/v1/bms/{id}/export/csv # Export CSV ring buffer
```

### Format snapshot WebSocket

```json
{
  "type": "snapshot",
  "data": {
    "1": {
      "bms_id": 1,
      "timestamp": 1710000000.0,
      "soc": 75.2,
      "pack_voltage": 53.28,
      "pack_current": 12.5,
      "power": 666,
      "cell_01": 3330, "cell_02": 3328, "..._16": 3325,
      "cell_min_v": 3325, "cell_min_num": 16,
      "cell_max_v": 3335, "cell_max_num": 3,
      "cell_delta": 10,
      "temp_01": 28.5, "temp_02": 29.1, "temp_03": 27.8, "temp_04": 28.3,
      "temp_max": 29.1, "temp_min": 27.8,
      "charge_mos": true, "discharge_mos": true,
      "bms_cycles": 147, "remaining_capacity": 240.6,
      "balancing_mask": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
      "alarm_cell_ovp": false, "alarm_cell_uvp": false,
      "alarm_pack_ovp": false, "alarm_pack_uvp": false,
      "alarm_chg_otp": false, "alarm_chg_ocp": false,
      "alarm_dsg_ocp": false, "alarm_scp": false,
      "alarm_cell_delta": false, "any_alarm": false
    },
    "2": { "..." }
  }
}
```

---

## Alertes

Le moteur d'alertes `AlertBridge` s'exécute dans le processus `dalybms-api`.

### Règles prédéfinies

| Règle | Sévérité | Condition par défaut |
|-------|----------|----------------------|
| `cell_voltage_high` | CRITICAL | cellule max > 3.60V |
| `cell_voltage_low` | CRITICAL | cellule min < 2.90V |
| `cell_delta_high` | WARNING | delta > 100mV |
| `soc_low` | WARNING | SOC < 20% |
| `soc_critical` | CRITICAL | SOC < 10% |
| `temperature_high` | WARNING | temp max > 45°C |
| `current_high` | WARNING | courant > 80A |
| `charge_mos_off` | CRITICAL | CHG MOS = OFF inattendu |
| `discharge_mos_off` | CRITICAL | DSG MOS = OFF inattendu |
| `hw_cell_ovp` … `hw_scp` | CRITICAL | Flags hardware BMS |

### Canaux de notification

- **Telegram** : configurer `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`
- **Email** : configurer `SMTP_HOST` + `SMTP_TO` (et optionnellement `SMTP_USER`/`SMTP_PASS`)

### Snooze d'une alerte

```bash
# Silencer l'alerte cell_delta_high sur BMS 1 pendant 1h
curl -X POST http://dalybms.local/api/v1/alerts/snooze/1/cell_delta_high \
     -H "Content-Type: application/json" \
     -d '{"duration_s": 3600}'
```

---

## Développement local

```bash
# 1. Cloner et préparer l'environnement Python
git clone <repo>
cd Daly-BMS
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Démarrer l'infrastructure Docker (Mosquitto, InfluxDB, Grafana, Node-RED)
cp .env.docker.example .env.docker   # éditer credentials
make up

# 3. Copier et adapter la config Python
cp .env.example .env
# Éditer .env : DALY_PORT, INFLUX_TOKEN (même valeur que dans .env.docker), etc.

# 4. Lancer l'API
python daly_api.py

# 5. Dashboard en mode dev (proxy vers API:8000)
cd dashboard
npm install
npm run dev
# → http://localhost:3000
```

### Variables d'environnement utiles en dev

```bash
# Désactiver l'auth API (déjà le défaut)
DALY_API_KEY=

# Polling rapide pour tests
DALY_POLL_INTERVAL=0.5

# Ring buffer réduit
DALY_RING_SIZE=300

# Chemin DB alertes dans /tmp pour dev
ALERT_DB_PATH=/tmp/alerts_dev.db
```

### Tests

```bash
pip install -r requirements-test.txt
pytest test_suite.py -v
```

---

## Mise à jour

```bash
cd /chemin/vers/sources
git pull
sudo ./install.sh update
```

Le mode `update` : copie les sources, rebuild le dashboard React, redémarre `dalybms-api`.

---

## Ajouter un BMS / Découverte automatique

### Limites RS485

| Couche | Limite |
|--------|--------|
| Physique RS485 (standard TIA-485) | 32 unités par segment |
| RS485 avec transceivers 1/8-load | jusqu'à 256 unités |
| Protocole Daly (1 octet adresse) | 255 adresses (0x01..0xFF) |
| **Recommandé** | 4 BMS sur un USB/RS485 |

### Méthode 1 — Configuration manuelle (recommandée en production)

Éditer `.env` :
```bash
# 3 BMS :
DALY_ADDRESSES=0x01,0x02,0x03
BMS3_NAME=Pack XXXAh
MQTT_BMS3_NAME=pack_xxx
INFLUX_BMS3_NAME=pack_xxx
ALERT_BMS3_NAME=Pack XXXAh
```

Redémarrer le service :
```bash
sudo systemctl restart dalybms-api
```

### Méthode 2 — Découverte automatique (utile pour diagnostiquer)

Activer ponctuellement la découverte au démarrage :
```bash
# Dans .env :
DALY_AUTO_DISCOVER=1   # sonde 0x01..0x08 au démarrage

sudo systemctl restart dalybms-api
# Les logs indiquent les adresses trouvées :
journalctl -u dalybms-api -f | grep "BMS détecté"
```

Une fois les adresses confirmées, repasser en mode manuel :
```bash
DALY_AUTO_DISCOVER=0
DALY_ADDRESSES=0x01,0x02,0x03   # liste des adresses trouvées
```

### Méthode 3 — Endpoint `/api/v1/discover` (sans redémarrer)

```bash
# Sonde les adresses 1 à 8 (défaut) :
curl http://localhost:8000/api/v1/discover

# Sonde une plage étendue :
curl "http://localhost:8000/api/v1/discover?range_start=1&range_end=16"
```

Réponse exemple :
```json
{
  "found": [1, 2, 3],
  "count": 3,
  "duration_ms": 1847,
  "range": "0x01–0x08"
}
```

### Vérification post-ajout

```bash
# Vérifier que le nouveau BMS est bien polléé :
curl http://localhost:8000/api/v1/system/status | python3 -m json.tool

# Voir les snapshots en temps réel :
curl http://localhost:8000/api/v1/bms/3/status
```

---

## Dépannage

### Le BMS ne répond pas

```bash
# Vérifier le port série
ls -la /dev/ttyUSB*
# Vérifier les permissions
groups dalybms          # doit inclure dialout
# Test communication directe
python3 -c "
import serial
s = serial.Serial('/dev/ttyUSB1', 9600, timeout=1)
print('OK:', s.name)
s.close()
"
```

### L'API ne démarre pas

```bash
journalctl -u dalybms-api -n 50 --no-pager
# Tester en premier plan
cd /opt/dalybms
source venv/bin/activate
python daly_api.py
```

### InfluxDB non connecté

```bash
# Vérifier le token
curl -s http://localhost:8086/api/v2/buckets \
  -H "Authorization: Token $INFLUX_TOKEN" | python3 -m json.tool

# Recréer les buckets
source /opt/dalybms/venv/bin/activate
python3 -c "from daly_influx import InfluxSetup; InfluxSetup().run()"
```

### Dashboard vide / "OFF"

- Vérifier que `dalybms-api` tourne : `systemctl status dalybms-api`
- Vérifier nginx : `nginx -t && systemctl reload nginx`
- Ouvrir les DevTools → Console → erreur WebSocket ?
- Tester le WebSocket : `curl --include --no-buffer -H "Connection: Upgrade" -H "Upgrade: websocket" http://localhost/ws/bms/stream`

### Alertes non reçues

```bash
# Vérifier le journal SQLite
sqlite3 /data/dalybms/alerts.db "SELECT * FROM alert_events ORDER BY id DESC LIMIT 10;"

# Tester Telegram manuellement
curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}&text=Test+DalyBMS"
```

---

## Structure des fichiers

```
Daly-BMS/
├── daly_protocol.py          # D1 — Protocole UART Daly
├── daly_write.py             # D2 — Commandes écriture BMS
├── daly_api.py               # D3 — API REST + WebSocket + bridges
├── daly_mqtt.py              # D4 — Publication MQTT
├── daly_influx.py            # D5 — Écriture InfluxDB
├── daly_alerts.py            # D6 — Moteur d'alertes
├── dashboard/
│   ├── src/
│   │   ├── App.jsx           # SPA React — 8 pages de monitoring
│   │   └── main.jsx
│   ├── package.json
│   └── vite.config.js        # Proxy /api → :8000, /ws → ws://:8000
│
├── docker-compose.infra.yml  # Stack Docker infra (Phase 1)
├── Makefile                  # make up/down/logs/status/pull/reset
├── docker/
│   ├── mosquitto/
│   │   └── mosquitto.conf    # MQTT 1883 + WebSocket 9001
│   └── grafana/
│       └── provisioning/
│           ├── datasources/influxdb.yml   # Datasource auto-configurée
│           └── dashboards/provider.yml    # Chargement auto dashboard JSON
│
├── .env.example              # Template config Python (scripts natifs)
├── .env.docker.example       # Template config Docker (credentials infra)
├── daly_bms_grafana.json     # Dashboard Grafana (monté dans Grafana Docker)
├── install.sh                # Script d'installation complet
├── update.sh                 # Mise à jour rapide
├── backup.sh                 # Sauvegarde données/config
├── requirements.txt
├── requirements-test.txt
└── test_suite.py
```

---

## Interfaces web

| Service | URL | Description |
|---------|-----|-------------|
| **Dashboard** | `http://dalybms.local/` | Interface React temps réel |
| **API docs** | `http://dalybms.local/docs` | Swagger UI interactif |
| **API health** | `http://dalybms.local/health` | Status JSON |
| **Grafana** | `http://dalybms.local:3001` | Graphiques historiques (Docker) |
| **InfluxDB** | `http://dalybms.local:8086` | Console admin InfluxDB (Docker) |
| **Node-RED** | `http://dalybms.local:1880` | Éditeur de flows (Docker) |
| **MQTT** | `dalybms.local:1883` | Broker Mosquitto (Docker) |

---

*Installation Santuario — Badalucco (Ligurie) — Système photovoltaïque autonome*

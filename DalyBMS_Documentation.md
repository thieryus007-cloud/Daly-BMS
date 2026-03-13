# DalyBMS Interface — Documentation Complète
## Installation Santuario — Badalucco (Ligurie, IT)

**Version** : 1.0  
**Dernière mise à jour** : Mars 2026  
**Plateforme cible** : Raspberry Pi CM5 — Debian Bookworm  
**Auteur** : Santuario Project

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture système](#2-architecture-système)
3. [Matériel requis](#3-matériel-requis)
4. [Installation](#4-installation)
5. [Configuration](#5-configuration)
6. [Modules Python](#6-modules-python)
7. [API REST](#7-api-rest)
8. [WebSocket & SSE](#8-websocket--sse)
9. [MQTT](#9-mqtt)
10. [InfluxDB & Grafana](#10-influxdb--grafana)
11. [Alertes](#11-alertes)
12. [Bridge Venus OS](#12-bridge-venus-os)
13. [Interface Web](#13-interface-web)
14. [Services systemd](#14-services-systemd)
15. [Nginx](#15-nginx)
16. [Spécificités installation Santuario](#16-spécificités-installation-santuario)
17. [Dépannage](#17-dépannage)
18. [Tests & validation](#18-tests--validation)
19. [Référence variables d'environnement](#19-référence-variables-denvironnement)

---

## 1. Vue d'ensemble

DalyBMS Interface est une alternative open-source au logiciel PC Master fourni par Daly, conçue pour fonctionner sur un Raspberry Pi CM5 connecté en UART/RS485 à deux BMS Daly Smart 16S LiFePO4.

### Objectifs

- Monitoring temps réel des deux packs LiFePO4 (320Ah + 360Ah)
- Surveillance renforcée des cellules #8 et #16 (déséquilibre connu)
- Historisation InfluxDB + dashboards Grafana
- Alertes Telegram/Email avec gestion d'hysteresis
- Bridge vers Venus OS (NanoPi EasySolar II) via dbus-mqtt-devices
- Interface web React accessible sur le réseau local

### Remplacement du PC Master Daly

| Fonctionnalité | PC Master | DalyBMS Interface |
|---|---|---|
| Lecture SOC / tension / courant | ✓ | ✓ |
| Tensions cellules individuelles | ✓ | ✓ |
| Températures NTC | ✓ | ✓ |
| Contrôle MOSFET CHG/DSG | ✓ | ✓ |
| Calibration SOC | ✓ | ✓ |
| Configuration protections | ✓ | ✓ |
| Historique time-series | ✗ | ✓ InfluxDB |
| Alertes automatiques | ✗ | ✓ Telegram + Email |
| Bridge Venus OS | ✗ | ✓ dbus-mqtt-devices |
| Accès réseau local | ✗ | ✓ API REST + WebSocket |
| Multi-BMS simultané | ✗ | ✓ dual-BMS |
| Fonctionne sans Windows | ✗ | ✓ Linux natif |

---

## 2. Architecture système

```
┌─────────────────────────────────────────────────────────────────┐
│                    RPi CM5 — DalyBMS Interface                  │
│                                                                 │
│  ┌─────────────┐    ┌──────────────────────────────────────┐   │
│  │  daly_uart  │    │         daly_api.py (FastAPI)        │   │
│  │  _service   │───▶│  REST /api/v1/  WebSocket /ws/       │   │
│  │  (D1+D2)    │    │  SSE /api/v1/bms/{id}/sse            │   │
│  └──────┬──────┘    └──────────────┬───────────────────────┘   │
│         │                          │                            │
│  ┌──────▼──────┐    ┌──────────────▼───────────────────────┐   │
│  │  BMS 0x01   │    │            Mosquitto :1883            │   │
│  │  Pack 320Ah │    │       santuario/bms/{id}/{name}/      │   │
│  │  BMS 0x02   │    └──────┬───────────┬────────────────────┘   │
│  │  Pack 360Ah │           │           │                        │
│  └─────────────┘    ┌──────▼──────┐  ┌▼───────────────────┐   │
│         │           │ daly_influx │  │   daly_venus.py    │   │
│  UART/RS485         │  InfluxDB   │  │  Bridge NanoPi     │   │
│  /dev/ttyUSB1       │  + Grafana  │  │  MQTT → dbus       │   │
│                     └─────────────┘  └────────────────────┘   │
│                                                                 │
│  ┌─────────────────┐   ┌────────────────────────────────────┐  │
│  │  daly_alerts.py │   │         Nginx :80                  │  │
│  │  Telegram/Email │   │  /api/ → FastAPI :8000             │  │
│  │  SQLite journal │   │  /ws/  → WebSocket                 │  │
│  └─────────────────┘   │  /     → React SPA                 │  │
│                         │  /grafana/ → Grafana :3000        │  │
│                         └────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │ UART RS485                        │ MQTT :1883
         ▼                                   ▼
┌─────────────────┐              ┌───────────────────────┐
│  Daly Smart BMS │              │  NanoPi — EasySolar   │
│  BMS 0x01 320Ah │              │  Venus OS + VRM       │
│  BMS 0x02 360Ah │              │  dbus-mqtt-devices    │
└─────────────────┘              └───────────────────────┘
```

### Principe de communication UART

Le bus RS485 est partagé entre les deux BMS. La communication est **séquentielle half-duplex** : un seul BMS interrogé à la fois via un asyncio.Lock. Le protocole Daly est binaire (start byte `0xA5`, checksum 8 bits, timeout 500ms, retry 3 tentatives).

---

## 3. Matériel requis

### Obligatoire

| Composant | Spécification | Usage |
|---|---|---|
| Raspberry Pi CM5 | 4GB RAM minimum | Plateforme principale |
| Adaptateur USB/RS485 | FT232R ou CH340 | Connexion BMS |
| Câble RS485 | 2 fils torsadés blindés | Bus BMS |

### Installation Santuario — matériel existant

| Composant | Détail |
|---|---|
| EasySolar II 48/5000/70 | NanoPi interne, Venus OS 3.70, VE.CAN |
| MPPT 250/100 intégré | Géré par NanoPi |
| MPPT 150/35 | 3 panneaux orientation différente |
| BMS Daly Smart 320Ah | Address 0x01, CAN02, 16S LiFePO4 |
| BMS Daly Smart 360Ah | Address 0x02, CAN01, 16S LiFePO4 |
| SmartShunt 300A | SOC de référence (680Ah) |
| 2× ET112 | Compteurs énergie RS485 |
| CWT-SI PR-300 | Capteur irradiance Modbus RTU |
| 2× DEYE SUN-M200G4 | Micro-onduleurs AC-couplés |

### Câblage BMS → RPi CM5

```
BMS 0x01 (320Ah)  ──┐
                     ├── A+ ──── Adaptateur USB/RS485 ──── /dev/ttyUSB1
BMS 0x02 (360Ah)  ──┘── B-
```

Les deux BMS partagent le même bus RS485. Résistance de terminaison 120Ω à chaque extrémité.

---

## 4. Installation

### Prérequis système

```bash
# Vérifier Python
python3 --version   # >= 3.11 requis

# Vérifier le port série
ls -la /dev/ttyUSB*

# Ajouter l'utilisateur courant au groupe dialout
sudo usermod -aG dialout $USER
```

### Installation automatique

```bash
# Cloner les sources
git clone https://github.com/santuario/dalybms-interface.git
cd dalybms-interface

# Lancer l'installation (root requis)
sudo ./install.sh install
```

L'installateur exécute dans l'ordre : dépendances système, InfluxDB 2.x, Mosquitto, Grafana, création de l'utilisateur `dalybms`, environnement Python virtuel, sources Python, `.env`, services systemd, Nginx, logrotate.

### Installation manuelle (développement)

```bash
# Environnement virtuel
python3 -m venv venv
source venv/bin/activate

# Dépendances
pip install fastapi uvicorn[standard] pyserial serial-asyncio \
            aiomqtt influxdb-client[async] httpx pydantic

# Démarrage développement
uvicorn daly_api:app --reload --port 8000
```

### Vérification post-installation

```bash
# État de tous les services
sudo ./install.sh status

# Test port UART
sudo ./install.sh check-uart

# Test bridge Venus OS
cd /opt/dalybms
venv/bin/python daly_venus.py check
```

---

## 5. Configuration

Le fichier de configuration principal est `/opt/dalybms/.env`. Il est créé automatiquement par l'installateur et doit être édité avant le premier démarrage.

```bash
sudo nano /opt/dalybms/.env
```

Les sections clés à configurer obligatoirement :

```ini
# Port UART — vérifier avec ls /dev/ttyUSB*
DALY_PORT=/dev/ttyUSB1

# Token InfluxDB — obtenu via http://localhost:8086
INFLUX_TOKEN=CHANGE_ME

# Portal ID Venus OS NanoPi
VENUS_PORTAL_ID=c0619ab9929a

# IP NanoPi sur le réseau local
NANOPI_MQTT_HOST=192.168.1.120

# Telegram (optionnel)
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```

Après modification, redémarrer les services concernés :

```bash
sudo systemctl restart dalybms.target
```

---

## 6. Modules Python

### D1 — daly_protocol.py

Module de communication UART bas niveau. Implémente le protocole binaire Daly complet.

**Classes principales :**

`DalyPort` — gestion du port série async avec lock exclusif.

```python
async with DalyPort("/dev/ttyUSB1") as port:
    frame = await port.send_receive(bms_id=0x01, cmd=Cmd.SOC)
```

`DalyBms` — interface haut niveau par BMS.

```python
bms = DalyBms(port, bms_id=0x01)
soc   = await bms.get_soc()           # SocData
cells = await bms.get_cell_voltages() # CellVoltages
snap  = await bms.get_snapshot()      # BmsSnapshot complet
```

`DalyBusManager` — gestionnaire multi-BMS sur bus partagé.

```python
mgr = DalyBusManager("/dev/ttyUSB1", [0x01, 0x02])
await mgr.poll_loop(callback, interval=1.0)
```

**Commandes supportées (enum Cmd) :**

| Valeur | Commande | Description |
|---|---|---|
| `0x90` | SOC | Tension pack, courant, SOC |
| `0x91` | MIN_MAX_CELL_V | Cellule min/max |
| `0x92` | MIN_MAX_TEMP | Température min/max |
| `0x93` | MOS_STATUS | État MOSFET + cycles |
| `0x94` | STATUS_INFO | Cellules, sondes, alarmes |
| `0x95` | CELL_VOLTAGES | 16 tensions individuelles |
| `0x96` | TEMPERATURES | Sondes NTC |
| `0x97` | BALANCE_STATUS | Masque balancing |
| `0x98` | FAILURE_FLAGS | Flags alarmes hardware |

**Dataclasses retournées :**

```python
@dataclass
class SocData:
    pack_voltage: float    # V
    pack_current: float    # A (+ charge, - décharge)
    soc: float             # %

@dataclass
class CellVoltages:
    voltages: list[int]    # mV × 16
    min_v: int             # mV
    max_v: int             # mV
    delta: int             # mV
    min_num: int           # numéro cellule 1-based
    max_num: int           # numéro cellule 1-based

@dataclass
class BmsSnapshot:
    # Agrège toutes les dataclasses ci-dessus
    bms_id: int
    soc_data: SocData
    cell_voltages: CellVoltages
    temperatures: Temperatures
    mos_status: MosStatus
    balance_status: BalanceStatus
    failure_flags: FailureFlags
    timestamp: float
```

### D2 — daly_write.py

Couche d'écriture avec file de commandes séquencée. Toutes les commandes d'écriture passent par une `asyncio.Queue` FIFO pour garantir la cohérence sur le bus RS485.

**Commandes disponibles :**

```python
writer = DalyWriter(port, bms_id=0x01)

# MOSFET
await writer.set_charge_mos(True)
await writer.set_discharge_mos(True)

# SOC
await writer.set_soc(75.0)
await writer.force_full()
await writer.force_empty()

# Protections tension cellule
await writer.set_ovp_cell(3.65)   # OVP = 3.65V
await writer.set_uvp_cell(2.80)   # UVP = 2.80V

# Protections tension pack
await writer.set_ovp_pack(58.4)
await writer.set_uvp_pack(44.8)

# Protections courant
await writer.set_ocp_charge(70.0)
await writer.set_ocp_discharge(100.0)
await writer.set_scp(200.0)

# Thermiques
await writer.set_otp_charge(45.0)
await writer.set_utp_charge(0.0)

# Balancing
await writer.set_balance_enabled(True)
await writer.set_balance_trigger_voltage(3.40)
await writer.set_balance_trigger_delta(10)

# Pack
await writer.set_capacity(320.0)
await writer.set_cell_count(16)

# Profil complet
await writer.apply_profile(PROFILE_SANTUARIO_320AH)

# Reset
await writer.reset()
```

**Profils préconfigurés Santuario :**

```python
PROFILE_SANTUARIO_320AH = {
    "ovp_cell_v":        3.65,
    "uvp_cell_v":        2.80,
    "ovp_pack_v":        58.4,
    "uvp_pack_v":        44.8,
    "ocp_chg_a":         70.0,
    "ocp_dsg_a":         100.0,
    "scp_a":             200.0,
    "otp_chg_c":         45.0,
    "utp_chg_c":         0.0,
    "otp_dsg_c":         60.0,
    "utp_dsg_c":        -10.0,
    "balance_enabled":   True,
    "balance_v":         3.40,
    "balance_delta_mv":  10,
    "capacity_ah":       320.0,
    "cell_count":        16,
    "sensor_count":      4,
}
```

### D3 — daly_api.py

API REST FastAPI avec WebSocket et SSE. Démarre le poll_loop, maintient un ring buffer de 3600 points (1h à 1s).

### D4 — daly_mqtt.py

Publication MQTT structurée vers Mosquitto local + bridge optionnel vers NanoPi.

### D5 — daly_influx.py

Persistance InfluxDB avec batch writer et downsampling automatique (1 minute).

### D6 — daly_alerts.py

Moteur d'alertes avec règles configurables, hysteresis, snooze, journal SQLite, notifications Telegram/Email.

### D7 — Interface Web React

SPA React avec 8 pages : Dashboard, Cellules, Températures, Alarmes, Contrôle, Configuration, Dual BMS, Statistiques.

### D9 — daly_venus.py

Bridge MQTT ↔ Venus OS via dbus-mqtt-devices. Traduit les snapshots BMS en paths `com.victronenergy.battery`.

---

## 7. API REST

Base URL : `http://dalybms.local/api/v1`

Documentation interactive : `http://dalybms.local/docs`

### Endpoints de lecture

| Méthode | Path | Description |
|---|---|---|
| GET | `/system/status` | État global, connectivité |
| GET | `/bms/{id}/status` | Snapshot complet temps réel |
| GET | `/bms/{id}/cells` | Tensions + min/max/delta/balancing |
| GET | `/bms/{id}/temperatures` | Sondes NTC |
| GET | `/bms/{id}/alarms` | Flags alarmes actifs |
| GET | `/bms/{id}/mos` | État MOSFET + cycles |
| GET | `/bms/{id}/history` | Ring buffer (param: `duration=1h`) |
| GET | `/bms/{id}/history/summary` | Stats min/max/avg |
| GET | `/bms/compare` | Vue comparative dual-BMS |
| GET | `/bms/{id}/export/csv` | Export CSV streaming |

### Endpoints de contrôle

| Méthode | Path | Body | Description |
|---|---|---|---|
| POST | `/bms/{id}/mos` | `{"charge":true,"discharge":true}` | Contrôle MOSFET |
| POST | `/bms/{id}/soc` | `{"soc":75.0}` | Calibration SOC |
| POST | `/bms/{id}/soc/full` | — | Forcer SOC 100% |
| POST | `/bms/{id}/soc/empty` | — | Forcer SOC 0% |
| POST | `/bms/{id}/reset` | `{"confirm":"CONFIRM_RESET"}` | Reset BMS |
| POST | `/bms/{id}/config/ovp/cell` | `{"voltage":3.65}` | OVP cellule |
| POST | `/bms/{id}/config/uvp/cell` | `{"voltage":2.80}` | UVP cellule |
| POST | `/bms/{id}/config/ovp/pack` | `{"voltage":58.4}` | OVP pack |
| POST | `/bms/{id}/config/uvp/pack` | `{"voltage":44.8}` | UVP pack |
| POST | `/bms/{id}/config/ocp/charge` | `{"current":70.0}` | OCP charge |
| POST | `/bms/{id}/config/ocp/discharge` | `{"current":100.0}` | OCP décharge |
| POST | `/bms/{id}/config/scp` | `{"current":200.0}` | Court-circuit |
| POST | `/bms/{id}/config/balancing` | voir schéma | Config balancing |
| POST | `/bms/{id}/config/pack` | voir schéma | Paramètres pack |
| POST | `/bms/{id}/config/full` | voir schéma | Profil complet |
| POST | `/bms/{id}/config/preset/{name}` | — | Preset Santuario |

### Endpoints alertes

| Méthode | Path | Description |
|---|---|---|
| GET | `/alerts/active` | Alertes actuellement actives |
| GET | `/alerts/history` | Journal SQLite (params: bms_id, limit, offset) |
| GET | `/alerts/counters` | Compteurs par règle |
| GET | `/alerts/rules` | Liste règles configurées |
| GET | `/alerts/states` | État de toutes les règles |
| POST | `/alerts/snooze/{bms_id}/{rule}` | Snooze `{"duration_s":3600}` |
| DELETE | `/alerts/snooze/{bms_id}/{rule}` | Annuler snooze |

### Exemples curl

```bash
# État système
curl http://dalybms.local/api/v1/system/status | jq

# Snapshot BMS 1
curl http://dalybms.local/api/v1/bms/1/status | jq .soc

# Tensions cellules
curl http://dalybms.local/api/v1/bms/1/cells | jq .cell_voltages

# Contrôle MOSFET
curl -X POST http://dalybms.local/api/v1/bms/1/mos \
     -H "Content-Type: application/json" \
     -d '{"charge":true,"discharge":true}'

# Calibration SOC
curl -X POST http://dalybms.local/api/v1/bms/1/soc \
     -H "Content-Type: application/json" \
     -d '{"soc":85.0}'

# Appliquer profil Santuario
curl -X POST http://dalybms.local/api/v1/bms/1/config/preset/santuario_320ah

# Snooze alerte 1h
curl -X POST http://dalybms.local/api/v1/alerts/snooze/1/cell_delta_high \
     -H "Content-Type: application/json" \
     -d '{"duration_s":3600}'

# Export CSV
curl http://dalybms.local/api/v1/bms/1/export/csv?duration=24h > bms1_24h.csv
```

---

## 8. WebSocket & SSE

### WebSocket — stream temps réel

```javascript
// Tous les BMS
const ws = new WebSocket("ws://dalybms.local/ws/bms/stream");

// Un seul BMS
const ws = new WebSocket("ws://dalybms.local/ws/bms/1/stream");

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log(data.soc, data.pack_voltage);
};
```

Le serveur envoie un message JSON par cycle de poll (1s). Format identique à `/bms/{id}/status`.

### SSE — Server-Sent Events

Alternative WebSocket pour clients ne supportant pas WS :

```javascript
const es = new EventSource("http://dalybms.local/api/v1/bms/1/sse");
es.onmessage = (event) => {
    const data = JSON.parse(event.data);
};
```

---

## 9. MQTT

### Structure des topics

```
santuario/bms/{bms_id}/{bms_name}/{subtopic}
```

Exemple pour BMS 1 (Pack 320Ah) :

```
santuario/bms/1/pack_320ah/soc
santuario/bms/1/pack_320ah/pack_voltage
santuario/bms/1/pack_320ah/pack_current
santuario/bms/1/pack_320ah/power
santuario/bms/1/pack_320ah/cell_min_v
santuario/bms/1/pack_320ah/cell_max_v
santuario/bms/1/pack_320ah/cell_delta
santuario/bms/1/pack_320ah/temp_max
santuario/bms/1/pack_320ah/temp_min
santuario/bms/1/pack_320ah/charge_mos
santuario/bms/1/pack_320ah/discharge_mos
santuario/bms/1/pack_320ah/bms_cycles
santuario/bms/1/pack_320ah/remaining_capacity
santuario/bms/1/pack_320ah/any_alarm
```

**Topics JSON :**

```
santuario/bms/1/pack_320ah/cell_voltages   → [3310, 3311, ..., 3310]  (mV × 16)
santuario/bms/1/pack_320ah/temperatures    → [28.5, 29.1, 27.8, 28.3]
santuario/bms/1/pack_320ah/balancing       → [0, 0, ..., 0]
santuario/bms/1/pack_320ah/alarms          → {"cell_ovp":false,...}
santuario/bms/1/pack_320ah/status          → objet complet compatible dbus-mqtt-devices
```

**Topics cellules individuelles :**

```
santuario/bms/1/pack_320ah/cells/cell_01  → 3310
santuario/bms/1/pack_320ah/cells/cell_08  → 3355  (cellule surveillée)
santuario/bms/1/pack_320ah/cells/cell_16  → 3348  (cellule surveillée)
```

**Topics système :**

```
santuario/bms/system/online               → 1 (LWT : 0 si déconnecté)
santuario/bms/system/status               → {"bms_ids":[1,2],"connected":true}
```

### QoS

| Topic | QoS | Retain |
|---|---|---|
| Métriques continues | 0 | false |
| Alarmes / statut | 1 | true |
| LWT système | 1 | true |

### Abonnement Node-RED (NanoPi)

Sur le NanoPi, un flow Node-RED peut souscrire aux topics bridgés :

```
santuario/bms/1/pack_320ah/soc
santuario/bms/1/pack_320ah/cell_delta
santuario/bms/1/pack_320ah/alarms
```

Le bridge MQTT RPi CM5 → NanoPi republique automatiquement les topics essentiels si `MQTT_BRIDGE_ENABLED=true`.

---

## 10. InfluxDB & Grafana

### Measurements InfluxDB

| Measurement | Fréquence | Rétention |
|---|---|---|
| `bms_status` | 1s | 30j |
| `bms_cells` | 1s | 30j |
| `bms_temperatures` | 1s | 30j |
| `bms_alarms` | 1s | 30j |
| `bms_events` | À l'événement | 30j |
| `bms_balancing` | 1s | 30j |
| `bms_status` (downsampled) | 1min | 365j |

### Tags communs

```
bms_id=1
bms_name=pack_320ah
installation=santuario
```

### Requêtes Flux utiles

```flux
// SOC BMS 1 — dernière valeur
from(bucket: "daly_bms")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "bms_status")
  |> filter(fn: (r) => r.bms_id == "1")
  |> filter(fn: (r) => r._field == "soc")
  |> last()

// Delta cellule — moyenne 1 heure
from(bucket: "daly_bms")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "bms_cells")
  |> filter(fn: (r) => r.bms_id == "1")
  |> filter(fn: (r) => r._field == "cell_delta")
  |> aggregateWindow(every: 1m, fn: mean)

// Cellule #8 — historique 24h
from(bucket: "daly_bms")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "bms_cells")
  |> filter(fn: (r) => r.bms_id == "1")
  |> filter(fn: (r) => r._field == "cell_08")
  |> aggregateWindow(every: 5m, fn: mean)

// Événements alarmes
from(bucket: "daly_bms")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "bms_events")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
```

### Dashboards Grafana (D8)

Six dashboards préconfigurés, importables via `http://dalybms.local/grafana/` → Dashboards → Import JSON.

| Dashboard | Contenu |
|---|---|
| Overview | SOC gauge, tension, courant, puissance, delta, température |
| Cellules | 16 tensions individuelles, range chart, delta historique |
| Températures | 4 sondes NTC, gauge max |
| Alarmes | Flags time-series, journal événements |
| Dual BMS | Comparaison BMS1 vs BMS2 |
| Énergie | Énergie journalière chargée/déchargée, cycles |

### Configuration datasource Grafana

Aller dans `Configuration > Data sources > Add data source > InfluxDB` :

```
URL:           http://localhost:8086
Query Language: Flux
Organization:  santuario
Token:         <INFLUX_TOKEN>
Default Bucket: daly_bms
```

---

## 11. Alertes

### Règles préconfigurées

| Règle | Seuil déclenchement | Seuil effacement | Sévérité | Cooldown |
|---|---|---|---|---|
| `cell_voltage_high` | cell_max > 3.60V | cell_max < 3.55V | CRITICAL | 60s |
| `cell_voltage_low` | cell_min < 2.90V | cell_min > 2.95V | CRITICAL | 60s |
| `cell_delta_high` | delta > 100mV | delta < 80mV | WARNING | 600s |
| `soc_low` | SOC < 20% | SOC > 25% | WARNING | 900s |
| `soc_critical` | SOC < 10% | SOC > 12% | CRITICAL | 300s |
| `temperature_high` | temp_max > 45°C | temp_max < 40°C | WARNING | 300s |
| `current_high` | courant > 80A | courant < 70A | WARNING | 120s |
| `charge_mos_off` | CHG MOS = OFF | CHG MOS = ON | CRITICAL | 120s |
| `discharge_mos_off` | DSG MOS = OFF | DSG MOS = ON | CRITICAL | 120s |
| `hw_cell_ovp` | flag OVP hardware | flag effacé | CRITICAL | 60s |
| `hw_cell_uvp` | flag UVP hardware | flag effacé | CRITICAL | 60s |
| `hw_chg_ocp` | flag OCP charge | flag effacé | CRITICAL | 60s |
| `hw_dsg_ocp` | flag OCP décharge | flag effacé | CRITICAL | 60s |

### Fonctionnement hysteresis

Chaque règle a deux seuils distincts : `trigger_fn` (déclenchement) et `clear_fn` (effacement). Une alarme déclenchée reste active jusqu'à ce que `clear_fn` retourne `True`, évitant les oscillations.

### Configuration Telegram

```ini
TELEGRAM_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=-1001234567890
```

Pour obtenir le `chat_id` d'un groupe : ajouter le bot au groupe, envoyer un message, appeler `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### Configuration Email

```ini
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=votre@gmail.com
SMTP_PASS=mot_de_passe_application
SMTP_FROM=daly-bms@santuario.local
SMTP_TO=destinataire@email.com
```

### Format notification Telegram

```
🚨 DalyBMS — CRITICAL
━━━━━━━━━━━━━━━━━━━━━
🔋 BMS : Pack 320Ah
📋 Règle : cell_voltage_high
📝 Description : Tension cellule max > 3.6V
📊 Valeur : 3.612V (cellule #8)
🔔 Statut : 🟥 DÉCLENCHÉ
🕐 Horodatage : 14/03/2026 10:23:45
━━━━━━━━━━━━━━━━━━━━━
Santuario — Badalucco
```

### Snooze via API

```bash
# Snooze cell_delta_high sur BMS 1 pendant 4h
curl -X POST http://dalybms.local/api/v1/alerts/snooze/1/cell_delta_high \
     -H "Content-Type: application/json" \
     -d '{"duration_s":14400}'

# Annuler le snooze
curl -X DELETE http://dalybms.local/api/v1/alerts/snooze/1/cell_delta_high
```

---

## 12. Bridge Venus OS

### Architecture

Le RPi CM5 publie les données BMS vers le broker Mosquitto du NanoPi (port 1883) au format `dbus-mqtt-devices`. Venus OS crée automatiquement les services `com.victronenergy.battery` sur le dbus.

### Instances dbus

| Service | Instance | BMS |
|---|---|---|
| `com.victronenergy.battery` | 10 | BMS 0x01 — Pack 320Ah |
| `com.victronenergy.battery` | 11 | BMS 0x02 — Pack 360Ah |
| `com.victronenergy.meteo` | 20 | CWT-SI PR-300 irradiance |

### Paths publiés (com.victronenergy.battery)

| Path dbus | Unité | Source |
|---|---|---|
| `/Dc/0/Voltage` | V | pack_voltage |
| `/Dc/0/Current` | A | pack_current |
| `/Dc/0/Power` | W | power |
| `/Dc/0/Temperature` | °C | temp_max |
| `/Soc` | % | soc |
| `/Capacity` | Ah | config (320 ou 360) |
| `/ConsumedAmphours` | Ah | calculé |
| `/TimeToGo` | s | calculé si décharge |
| `/Info/MaxChargeVoltage` | V | 3.55V × 16 = 56.8V |
| `/Info/MaxChargeCurrent` | A | 70A si CHG MOS ON, 0 sinon |
| `/Info/MaxDischargeCurrent` | A | 100A si DSG MOS ON, 0 sinon |
| `/Info/BatteryLowVoltage` | V | 2.80V × 16 = 44.8V |
| `/Io/AllowToCharge` | 0/1 | charge_mos |
| `/Io/AllowToDischarge` | 0/1 | discharge_mos |
| `/System/MinCellVoltage` | V | cell_min_v / 1000 |
| `/System/MaxCellVoltage` | V | cell_max_v / 1000 |
| `/Alarms/Alarm` | 0/1 | any_alarm |
| `/Alarms/HighVoltage` | 0/1 | cell_ovp ou pack_ovp |
| `/Alarms/LowVoltage` | 0/1 | cell_uvp ou pack_uvp |

### Découverte du Portal ID

```bash
# Découverte automatique via MQTT
cd /opt/dalybms
venv/bin/python -c "
import asyncio
from daly_venus import discover_portal_id
pid = asyncio.run(discover_portal_id())
print(f'Portal ID : {pid}')
"
```

### Test commissioning

```bash
cd /opt/dalybms
venv/bin/python daly_venus.py check
```

Vérifie la connectivité NanoPi, liste les services dbus actifs, publie un test battery et indique de vérifier dans VRM > Services > battery.

### Vérification dans Venus OS

Connecter un terminal SSH au NanoPi :

```bash
# Lister les services battery
dbus -y com.victronenergy.battery.10 / GetValue
dbus -y com.victronenergy.battery.10 /Soc GetValue
dbus -y com.victronenergy.battery.10 /Dc/0/Voltage GetValue
```

---

## 13. Interface Web

Accès : `http://dalybms.local`

### Pages disponibles

| Page | Contenu |
|---|---|
| Dashboard | Gauge SOC, tension/courant/puissance, sparklines, MOS status |
| Cellules | Grille 16 cellules, SVG range chart, historique #8/#16 |
| Températures | 4 sondes NTC avec sparklines et indicateur statut |
| Alarmes | Flags hardware, journal événements |
| Contrôle | MOSFET CHG/DSG (double confirmation), calibration SOC |
| Configuration | Protections tension/courant/thermiques, balancing, pack |
| Dual BMS | Vue comparative BMS1 vs BMS2 |
| Statistiques | Énergie journalière 7j, santé pack, historique SOC |

### Connexion WebSocket

L'interface utilise WebSocket (`/ws/bms/stream`) pour les données temps réel. Si la connexion est perdue, reconnexion automatique toutes les 5s.

### Build production (React + Vite)

```bash
cd frontend
npm install
npm run build
# Les fichiers sont dans frontend/dist/
# Nginx les sert depuis /opt/dalybms/frontend/dist/
```

---

## 14. Services systemd

### Démarrage / arrêt

```bash
# Démarrer tous les services
sudo systemctl start dalybms.target

# Arrêter tous les services
sudo systemctl stop dalybms.target

# Redémarrer un service spécifique
sudo systemctl restart dalybms-api

# Voir les logs en temps réel
sudo journalctl -u dalybms-api -f
sudo journalctl -u dalybms-mqtt -f
sudo journalctl -u dalybms-venus -f

# Logs des 100 dernières lignes
sudo journalctl -u dalybms-api -n 100

# Tous les services DalyBMS
sudo journalctl -u "dalybms-*" -f
```

### Liste des services

| Service | Rôle | Dépendances |
|---|---|---|
| `dalybms-api` | FastAPI + poll_loop UART | mosquitto |
| `dalybms-mqtt` | Publication MQTT | mosquitto, dalybms-api |
| `dalybms-influx` | Écriture InfluxDB | influxdb, dalybms-api |
| `dalybms-alerts` | Moteur alertes | dalybms-api |
| `dalybms-venus` | Bridge Venus OS | mosquitto |
| `dalybms.target` | Groupe cible | tous les services |

### Vérification état

```bash
# État succinct tous les services
for svc in api mqtt influx alerts venus; do
    echo "── dalybms-$svc"
    systemctl is-active dalybms-$svc
done
```

---

## 15. Nginx

Nginx est le point d'entrée unique sur le port 80. Il route vers :

- `/api/` → FastAPI :8000 (REST)
- `/ws/` → FastAPI :8000 (WebSocket, upgrade HTTP)
- `/api/v1/bms/*/sse` → FastAPI :8000 (SSE, no buffering)
- `/docs`, `/redoc` → FastAPI :8000
- `/` → fichiers statiques React (`/opt/dalybms/frontend/dist/`)
- `/grafana/` → Grafana :3000

### Test configuration Nginx

```bash
sudo nginx -t
sudo nginx -s reload
```

### Logs Nginx

```bash
sudo tail -f /var/log/nginx/dalybms_access.log
sudo tail -f /var/log/nginx/dalybms_error.log
```

---

## 16. Spécificités installation Santuario

### Configuration BMS

| Paramètre | BMS 1 (320Ah) | BMS 2 (360Ah) |
|---|---|---|
| Adresse Daly | 0x01 | 0x02 |
| Instance Venus OS | 10 | 11 |
| Instance dbus (NanoPi) | `battery/3` | `battery/4` |
| Capacité | 320Ah | 360Ah |
| Cellules | 16S LiFePO4 | 16S LiFePO4 |
| Tension nominale | 51.2V | 51.2V |

### Cellules #8 et #16 — surveillance renforcée

Le pack 320Ah (BMS 0x01) présente un déséquilibre connu sur les cellules #8 et #16 qui atteignent OVP (3.62V+) pendant l'absorption tandis que les autres cellules restent à ~3.33V (delta ~293mV).

**Mitigation active :**
- Tension d'absorption réduite à 56.0–56.8V dans VEConfigure (3.50–3.55V/cellule)
- Seuil alerte logicielle `cell_delta_high` à 100mV (vs 80mV clearing)
- SVG range chart avec cellules #8 et #16 en rouge dans l'interface web

**Réparation planifiée :**
- Cellule #16 : remplacement physique faisable
- Cellule #8 : difficultés d'accès physique

### Tensions de référence

| Paramètre | Valeur | Calcul |
|---|---|---|
| Absorption | 56.8V | 3.55V × 16 |
| Float | 54.4V | 3.40V × 16 |
| CVL (MaxChargeVoltage) | 56.8V | identique absorption |
| OVP cellule hardware | 3.65V | → 58.4V pack |
| UVP cellule hardware | 2.80V | → 44.8V pack |
| Alerte logicielle cell_max | 3.60V | en dessous OVP hardware |

### SmartShunt 300A

Configuration recommandée :
- Capacité : 680Ah (320 + 360)
- Tension chargée : 56.8V
- Courant de queue : 1% = 6.8A
- Peukert : 1.05
- Efficacité de charge : 99%

Le SmartShunt est la référence SOC authoritative. Le SOC Daly est cosmétique.

### Irradiance CWT-SI PR-300

Le capteur irradiance est publié vers Venus OS via `com.victronenergy.meteo` (instance 20). Si le NanoPi gère déjà le capteur directement via serial-starter, désactiver la publication meteo dans `daly_venus.py` en mettant `INST_METEO=0`.

---

## 17. Dépannage

### Service dalybms-api ne démarre pas

```bash
sudo journalctl -u dalybms-api -n 50
```

Causes fréquentes :
- Port UART incorrect : vérifier `DALY_PORT` dans `.env`
- Module Python manquant : relancer `sudo ./install.sh update`
- Token InfluxDB non configuré : éditer `.env`

### BMS ne répond pas

```bash
# Vérifier que le port série existe
ls -la /dev/ttyUSB*

# Tester la communication directe
python3 -c "
import serial
s = serial.Serial('/dev/ttyUSB1', 9600, timeout=1)
# Commande SOC BMS 0x01
frame = bytes([0xA5, 0x01, 0x90, 0x08, 0x00]*4+[0x00])
cs = sum(frame[:12]) & 0xFF
frame = bytes([0xA5,0x01,0x90,0x08,0,0,0,0,0,0,0,0,cs])
s.write(frame)
resp = s.read(13)
print(resp.hex())
"
```

Si aucune réponse : vérifier câblage A+/B-, alimentation BMS, adresse BMS.

### Valeurs aberrantes dans Grafana

Vérifier la chaîne complète :

```bash
# MQTT — dernière valeur SOC
mosquitto_sub -h localhost -t "santuario/bms/1/pack_320ah/soc" -C 1

# InfluxDB — dernière valeur
influx query '
from(bucket:"daly_bms")
  |> range(start:-5m)
  |> filter(fn:(r) => r._measurement=="bms_status" and r.bms_id=="1" and r._field=="soc")
  |> last()
'
```

### Venus OS ne voit pas les batteries

```bash
# Vérifier la connexion broker NanoPi
mosquitto_pub -h 192.168.1.120 -t test/ping -m ping

# Vérifier le portal ID
cd /opt/dalybms
venv/bin/python daly_venus.py check

# Sur NanoPi — vérifier dbus
ssh root@192.168.1.120
dbus-send --system --print-reply --dest=com.victronenergy.battery.10 / org.freedesktop.DBus.Introspectable.Introspect
```

### Alertes Telegram non reçues

```bash
# Tester le token Telegram
curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe"

# Tester l'envoi direct
curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
     -d "chat_id=${TELEGRAM_CHAT_ID}&text=Test+DalyBMS"
```

### Logs verbeux pour diagnostic

Modifier temporairement dans `.env` :

```ini
# Ajouter dans la commande ExecStart de dalybms-api.service
# --log-level debug
```

Ou directement :

```bash
cd /opt/dalybms
DALY_PORT=/dev/ttyUSB1 venv/bin/python -c "
import logging, asyncio
logging.basicConfig(level=logging.DEBUG)
from daly_protocol import DalyBusManager
mgr = DalyBusManager('/dev/ttyUSB1', [0x01, 0x02])
async def cb(s): print(s)
asyncio.run(mgr.poll_loop(cb, 2.0))
"
```

---

## 18. Tests & validation

### Exécution tests offline (sans RPi CM5)

```bash
# Installation dépendances test
pip install pytest pytest-asyncio pytest-cov httpx

# Tous les tests offline
pytest test_suite.py -v

# Tests avec couverture
pytest test_suite.py --cov=. --cov-report=html -v
```

### Tests intégration (Mosquitto local requis)

```bash
pytest test_suite.py -m integration -v
```

### Tests hardware (RPi CM5 + BMS réels)

```bash
# À exécuter sur le RPi CM5 une fois reçu
pytest test_suite.py -m hardware --uart=/dev/ttyUSB1 --run-hardware -v

# Un test spécifique
pytest test_suite.py -m hardware -k "test_bms1_cell_count" --run-hardware -v
```

### Checklist commissioning RPi CM5

Exécuter dans l'ordre lors de la réception du kit :

```
[ ] 1. Flash image Debian Bookworm sur eMMC CM5
[ ] 2. sudo ./install.sh install
[ ] 3. Configurer /opt/dalybms/.env
[ ] 4. Configurer InfluxDB (http://localhost:8086)
[ ] 5. sudo ./install.sh check-uart → port détecté
[ ] 6. pytest test_suite.py -m hardware --run-hardware
[ ] 7. sudo systemctl start dalybms.target
[ ] 8. curl http://localhost:8000/api/v1/system/status
[ ] 9. Vérifier Grafana http://localhost:3000
[10] 10. venv/bin/python daly_venus.py check
[11] 11. Vérifier Venus OS : Services > battery/10 et battery/11
[12] 12. Tester alerte Telegram (déclencher manuellement via snooze=0)
```

---

## 19. Référence variables d'environnement

### UART / BMS

| Variable | Défaut | Description |
|---|---|---|
| `DALY_PORT` | `/dev/ttyUSB1` | Port série RS485 |
| `DALY_BAUD` | `9600` | Vitesse UART |
| `DALY_POLL_INTERVAL` | `1.0` | Intervalle poll (secondes) |
| `DALY_CELL_COUNT` | `16` | Nombre de cellules en série |
| `DALY_SENSOR_COUNT` | `4` | Nombre de sondes NTC |
| `BMS1_CAPACITY_AH` | `320` | Capacité nominale BMS 1 |
| `BMS2_CAPACITY_AH` | `360` | Capacité nominale BMS 2 |

### MQTT local

| Variable | Défaut | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | Broker Mosquitto RPi CM5 |
| `MQTT_PORT` | `1883` | Port broker |
| `MQTT_PREFIX` | `santuario/bms` | Préfixe topics |
| `MQTT_INTERVAL` | `5.0` | Intervalle publication |
| `MQTT_QOS_DATA` | `0` | QoS métriques |
| `MQTT_QOS_ALARM` | `1` | QoS alarmes |

### Venus OS Bridge

| Variable | Défaut | Description |
|---|---|---|
| `NANOPI_MQTT_HOST` | `192.168.1.120` | IP NanoPi |
| `NANOPI_MQTT_PORT` | `1883` | Port broker NanoPi |
| `VENUS_PORTAL_ID` | `c0619ab9929a` | Portal ID VRM |
| `VENUS_BMS1_INSTANCE` | `10` | Instance dbus BMS 1 |
| `VENUS_BMS2_INSTANCE` | `11` | Instance dbus BMS 2 |
| `VENUS_METEO_INSTANCE` | `20` | Instance dbus meteo |
| `VENUS_PUBLISH_INTERVAL` | `5.0` | Intervalle publication Venus |

### InfluxDB

| Variable | Défaut | Description |
|---|---|---|
| `INFLUX_URL` | `http://localhost:8086` | URL InfluxDB |
| `INFLUX_TOKEN` | — | **Obligatoire** — token API |
| `INFLUX_ORG` | `santuario` | Organisation |
| `INFLUX_BUCKET` | `daly_bms` | Bucket full-res (30j) |
| `INFLUX_BUCKET_DS` | `daly_bms_1m` | Bucket downsampled (365j) |
| `INFLUX_BATCH_SIZE` | `50` | Taille batch |
| `INFLUX_BATCH_INTERVAL` | `5` | Flush interval (secondes) |
| `INFLUX_RETENTION_DAYS` | `30` | Rétention full-res |

### API

| Variable | Défaut | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Port FastAPI |
| `API_WORKERS` | `1` | Workers Uvicorn |
| `API_KEY` | — | Clé auth (vide = désactivé) |

### Alertes

| Variable | Défaut | Description |
|---|---|---|
| `ALERT_DB_PATH` | `/data/dalybms/alerts.db` | Journal SQLite |
| `TELEGRAM_TOKEN` | — | Token bot Telegram |
| `TELEGRAM_CHAT_ID` | — | Chat ID Telegram |
| `SMTP_HOST` | — | Serveur SMTP |
| `SMTP_PORT` | `587` | Port SMTP |
| `SMTP_USER` | — | Utilisateur SMTP |
| `SMTP_PASS` | — | Mot de passe SMTP |
| `ALERT_CELL_OVP_V` | `3.60` | Seuil alerte surtension cellule |
| `ALERT_CELL_DELTA_MV` | `100` | Seuil alerte déséquilibre |
| `ALERT_SOC_LOW` | `20.0` | Seuil alerte SOC faible |
| `ALERT_SOC_CRITICAL` | `10.0` | Seuil alerte SOC critique |
| `ALERT_TEMP_HIGH_C` | `45.0` | Seuil alerte température |

---

*Documentation générée pour le projet DalyBMS Interface — Installation Santuario, Badalucco (Ligurie, IT)*  
*Livrables D1–D12 — Version 1.0*

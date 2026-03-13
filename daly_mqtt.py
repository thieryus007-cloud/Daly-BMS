"""
daly_mqtt.py — D4 : Intégration MQTT
Publication topics structurés, QoS, retain, reconnexion, bridge NanoPi.
Dépend de : daly_protocol.py (D1), daly_api.py (D3 — AppState)
Installation Santuario — Badalucco
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiomqtt
from aiomqtt import Client, MqttError

from daly_protocol import BmsSnapshot, snapshot_to_dict

log = logging.getLogger("daly.mqtt")

# ─── Configuration ────────────────────────────────────────────────────────────
MQTT_HOST        = os.getenv("MQTT_HOST",        "localhost")
MQTT_PORT        = int(os.getenv("MQTT_PORT",    "1883"))
MQTT_USER        = os.getenv("MQTT_USER",        "")
MQTT_PASS        = os.getenv("MQTT_PASS",        "")
MQTT_PREFIX      = os.getenv("MQTT_PREFIX",      "santuario/bms")
MQTT_CLIENT_ID   = os.getenv("MQTT_CLIENT_ID",  "daly-bms-service")
MQTT_QOS_DATA    = int(os.getenv("MQTT_QOS_DATA",  "0"))   # métriques continues
MQTT_QOS_ALARM   = int(os.getenv("MQTT_QOS_ALARM", "1"))   # alarmes critiques
MQTT_QOS_STATUS  = int(os.getenv("MQTT_QOS_STATUS","1"))   # état MOS
PUBLISH_INTERVAL = float(os.getenv("MQTT_INTERVAL", "5.0"))

# Bridge NanoPi (optionnel)
BRIDGE_ENABLED   = os.getenv("MQTT_BRIDGE_ENABLED", "false").lower() == "true"
BRIDGE_HOST      = os.getenv("MQTT_BRIDGE_HOST",  "192.168.1.120")  # IP NanoPi
BRIDGE_PORT      = int(os.getenv("MQTT_BRIDGE_PORT", "1883"))
BRIDGE_PREFIX    = os.getenv("MQTT_BRIDGE_PREFIX", "santuario/bms")

# Noms des BMS pour les topics — dynamique selon DALY_ADDRESSES
def _load_bms_names() -> dict[int, str]:
    """Construit le dict {bms_id: nom} depuis DALY_ADDRESSES + MQTT_BMS{N}_NAME."""
    raw = os.getenv("DALY_ADDRESSES", "0x01,0x02")
    ids = sorted({int(x.strip(), 0) for x in raw.split(",") if x.strip()})
    return {bid: os.getenv(f"MQTT_BMS{bid}_NAME", f"bms{bid:02d}") for bid in ids}

BMS_NAMES = _load_bms_names()


# ─── Structure de topic ───────────────────────────────────────────────────────
def topic(bms_id: int, subtopic: str) -> str:
    """
    Construit un topic MQTT structuré.
    Format : {prefix}/{bms_id}/{bms_name}/{subtopic}
    Exemple : santuario/bms/1/pack_320ah/soc
    """
    name = BMS_NAMES.get(bms_id, f"bms{bms_id}")
    return f"{MQTT_PREFIX}/{bms_id}/{name}/{subtopic}"


def topic_system(subtopic: str) -> str:
    """Topics système (non liés à un BMS spécifique)."""
    return f"{MQTT_PREFIX}/system/{subtopic}"


# ─── Définition des topics publiés ────────────────────────────────────────────
#
# Chaque entrée : (subtopic, snapshot_key_or_fn, qos, retain)
# snapshot_key_or_fn : clé dans le dict snapshot, ou callable(snap) → valeur
#
TOPICS_SCALAR = [
    # subtopic              clé snapshot        qos              retain
    ("soc",                 "soc",              MQTT_QOS_DATA,   True),
    ("voltage",             "pack_voltage",     MQTT_QOS_DATA,   True),
    ("current",             "pack_current",     MQTT_QOS_DATA,   False),
    ("power",               "power",            MQTT_QOS_DATA,   False),
    ("cell_min_v",          "cell_min_v",       MQTT_QOS_DATA,   False),
    ("cell_max_v",          "cell_max_v",       MQTT_QOS_DATA,   False),
    ("cell_delta",          "cell_delta",       MQTT_QOS_DATA,   True),
    ("temp_max",            "temp_max",         MQTT_QOS_DATA,   False),
    ("temp_min",            "temp_min",         MQTT_QOS_DATA,   False),
    ("charge_mos",          "charge_mos",       MQTT_QOS_STATUS, True),
    ("discharge_mos",       "discharge_mos",    MQTT_QOS_STATUS, True),
    ("bms_cycles",          "bms_cycles",       MQTT_QOS_DATA,   True),
    ("remaining_capacity",  "remaining_capacity", MQTT_QOS_DATA, False),
    ("any_alarm",           "any_alarm",        MQTT_QOS_ALARM,  True),
]

TOPICS_ALARM_FLAGS = [
    "alarm_cell_ovp", "alarm_cell_uvp", "alarm_pack_ovp", "alarm_pack_uvp",
    "alarm_chg_otp",  "alarm_chg_ocp",  "alarm_dsg_ocp",  "alarm_scp",
    "alarm_cell_delta",
]


# ─── Payload helpers ──────────────────────────────────────────────────────────
def _to_payload(value: Any) -> str:
    """Convertit une valeur Python en payload MQTT string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    if value is None:
        return ""
    return str(value)


def _cell_voltages_payload(snap: dict, cell_count: int = 16) -> str:
    """Array JSON des tensions cellules pour topic cell_voltages."""
    cells = [snap.get(f"cell_{i:02d}") for i in range(1, cell_count + 1)]
    return json.dumps([v for v in cells if v is not None])


def _temperatures_payload(snap: dict, sensor_count: int = 4) -> str:
    """Array JSON des températures sondes."""
    temps = [snap.get(f"temp_{i:02d}") for i in range(1, sensor_count + 1)]
    return json.dumps([t for t in temps if t is not None])


def _alarm_payload(snap: dict) -> str:
    """JSON compact de tous les flags d'alarme."""
    return json.dumps({
        key.removeprefix("alarm_"): snap.get(key, False)
        for key in TOPICS_ALARM_FLAGS
    })


def _balancing_payload(snap: dict) -> str:
    """JSON de l'état de balancing par cellule."""
    mask = snap.get("balancing_mask", [])
    return json.dumps(mask)


def _status_payload(snap: dict) -> str:
    """JSON résumé complet pour topic status (compatible dbus-mqtt-devices)."""
    return json.dumps({
        "soc":            snap.get("soc"),
        "voltage":        snap.get("pack_voltage"),
        "current":        snap.get("pack_current"),
        "power":          snap.get("power"),
        "charge_mos":     snap.get("charge_mos"),
        "discharge_mos":  snap.get("discharge_mos"),
        "cell_delta_mv":  snap.get("cell_delta"),
        "any_alarm":      snap.get("any_alarm"),
        "cycles":         snap.get("bms_cycles"),
        "ts":             snap.get("timestamp"),
    })


# ─── Classe principale publisher ──────────────────────────────────────────────
class DalyMqttPublisher:
    """
    Publisher MQTT asynchrone pour les données BMS Daly.

    Fonctionnement :
    - Reçoit les snapshots via update(bms_id, snap_dict)
    - Publie à intervalle régulier sur tous les topics configurés
    - Publie immédiatement sur changement d'alarme (QoS 1)
    - Gère la reconnexion automatique avec backoff exponentiel
    - Optionnel : bridge vers Mosquitto NanoPi
    """

    def __init__(self,
                 host: str = MQTT_HOST,
                 port: int = MQTT_PORT,
                 username: str = MQTT_USER,
                 password: str = MQTT_PASS,
                 prefix: str = MQTT_PREFIX,
                 publish_interval: float = PUBLISH_INTERVAL,
                 cell_count: int = 16,
                 sensor_count: int = 4):
        self.host             = host
        self.port             = port
        self.username         = username
        self.password         = password
        self.prefix           = prefix
        self.publish_interval = publish_interval
        self.cell_count       = cell_count
        self.sensor_count     = sensor_count

        self._snapshots: dict[int, dict]     = {}
        self._prev_alarms: dict[int, dict]   = {}
        self._running    = False
        self._task: Optional[asyncio.Task]   = None
        self._bridge_task: Optional[asyncio.Task] = None
        self._publish_queue: asyncio.Queue   = asyncio.Queue()

    # ── Interface publique ────────────────────────────────────────────────────

    def update(self, bms_id: int, snap: dict) -> None:
        """
        Met à jour le snapshot d'un BMS.
        Appelé par le poll_loop à chaque cycle (depuis daly_api.py ou standalone).
        Détecte les changements d'alarme et déclenche une publication immédiate.
        """
        self._snapshots[bms_id] = snap

        # Détection changement alarme → publication prioritaire
        prev = self._prev_alarms.get(bms_id, {})
        curr = {k: snap.get(k, False) for k in TOPICS_ALARM_FLAGS + ["any_alarm"]}
        if curr != prev:
            self._prev_alarms[bms_id] = curr
            self._publish_queue.put_nowait(("alarm", bms_id, snap))
            log.warning(f"[BMS{bms_id}] Changement alarme détecté — publication prioritaire")

    def start(self):
        """Démarre les tâches asyncio de publication."""
        self._running = True
        self._task = asyncio.create_task(self._run_publisher(), name="mqtt-publisher")
        if BRIDGE_ENABLED:
            self._bridge_task = asyncio.create_task(
                self._run_bridge(), name="mqtt-bridge"
            )
        log.info(f"DalyMqttPublisher démarré — broker {self.host}:{self.port}")

    async def stop(self):
        """Arrête proprement les tâches MQTT."""
        self._running = False
        for task in (self._task, self._bridge_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("DalyMqttPublisher arrêté")

    # ── Boucle principale de publication ─────────────────────────────────────

    async def _run_publisher(self):
        backoff = 1.0
        while self._running:
            try:
                async with self._make_client(self.host, self.port,
                                             self.username, self.password,
                                             f"{MQTT_CLIENT_ID}-pub") as client:
                    log.info(f"MQTT connecté : {self.host}:{self.port}")
                    backoff = 1.0
                    await self._publish_online(client)
                    await self._loop(client)
            except MqttError as exc:
                log.warning(f"MQTT déconnecté : {exc} — reconnexion dans {backoff}s")
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(f"MQTT erreur inattendue : {exc}", exc_info=True)
                await asyncio.sleep(backoff)

    async def _loop(self, client: Client):
        """Boucle interne : publication périodique + traitement file prioritaire."""
        last_publish = 0.0
        while self._running:
            # Publication immédiate sur événement prioritaire (alarme)
            while not self._publish_queue.empty():
                try:
                    kind, bms_id, snap = self._publish_queue.get_nowait()
                    if kind == "alarm":
                        await self._publish_alarms(client, bms_id, snap)
                except asyncio.QueueEmpty:
                    break

            # Publication périodique de toutes les métriques
            now = time.monotonic()
            if now - last_publish >= self.publish_interval:
                for bms_id, snap in list(self._snapshots.items()):
                    await self._publish_all(client, bms_id, snap)
                await self._publish_system_status(client)
                last_publish = now

            await asyncio.sleep(0.1)

    # ── Méthodes de publication ───────────────────────────────────────────────

    async def _publish_online(self, client: Client):
        """LWT inverse — publication online au démarrage."""
        await client.publish(
            topic_system("online"),
            payload="true",
            qos=1,
            retain=True,
        )

    async def _publish_all(self, client: Client, bms_id: int, snap: dict):
        """Publication complète de toutes les métriques d'un BMS."""
        # Scalaires individuels
        for subtopic, key, qos, retain in TOPICS_SCALAR:
            value = snap.get(key)
            if value is not None:
                await client.publish(
                    topic(bms_id, subtopic),
                    payload=_to_payload(value),
                    qos=qos,
                    retain=retain,
                )

        # Tensions cellules (JSON array)
        await client.publish(
            topic(bms_id, "cell_voltages"),
            payload=_cell_voltages_payload(snap, self.cell_count),
            qos=MQTT_QOS_DATA,
            retain=False,
        )

        # Températures (JSON array)
        await client.publish(
            topic(bms_id, "temperatures"),
            payload=_temperatures_payload(snap, self.sensor_count),
            qos=MQTT_QOS_DATA,
            retain=False,
        )

        # Balancing mask (JSON array bool)
        await client.publish(
            topic(bms_id, "balancing"),
            payload=_balancing_payload(snap),
            qos=MQTT_QOS_DATA,
            retain=False,
        )

        # Alarmes (JSON compact — retain pour nouveaux subscribers)
        await client.publish(
            topic(bms_id, "alarms"),
            payload=_alarm_payload(snap),
            qos=MQTT_QOS_ALARM,
            retain=True,
        )

        # Statut JSON complet (compatible dbus-mqtt-devices NanoPi)
        await client.publish(
            topic(bms_id, "status"),
            payload=_status_payload(snap),
            qos=MQTT_QOS_STATUS,
            retain=True,
        )

        # Tensions individuelles par cellule (topics séparés)
        for i in range(1, self.cell_count + 1):
            v = snap.get(f"cell_{i:02d}")
            if v is not None:
                await client.publish(
                    topic(bms_id, f"cells/cell_{i:02d}"),
                    payload=_to_payload(v),
                    qos=MQTT_QOS_DATA,
                    retain=False,
                )

        log.debug(f"[BMS{bms_id}] Topics publiés")

    async def _publish_alarms(self, client: Client, bms_id: int, snap: dict):
        """Publication prioritaire immédiate des alarmes (QoS 1, retain)."""
        await client.publish(
            topic(bms_id, "alarms"),
            payload=_alarm_payload(snap),
            qos=MQTT_QOS_ALARM,
            retain=True,
        )
        await client.publish(
            topic(bms_id, "any_alarm"),
            payload=_to_payload(snap.get("any_alarm", False)),
            qos=MQTT_QOS_ALARM,
            retain=True,
        )
        log.warning(f"[BMS{bms_id}] Alarmes publiées (prioritaire)")

    async def _publish_system_status(self, client: Client):
        """Topics système : état global, timestamp, nombre de BMS actifs."""
        payload = json.dumps({
            "active_bms": list(self._snapshots.keys()),
            "any_alarm": any(
                s.get("any_alarm", False) for s in self._snapshots.values()
            ),
            "ts": time.time(),
        })
        await client.publish(
            topic_system("status"),
            payload=payload,
            qos=MQTT_QOS_STATUS,
            retain=True,
        )

    # ── Bridge NanoPi ─────────────────────────────────────────────────────────

    async def _run_bridge(self):
        """
        Bridge MQTT local → NanoPi Mosquitto.
        Republication des topics essentiels sur le broker NanoPi
        pour intégration avec Node-RED et dbus-mqtt-devices.
        Topics bridgés : status, alarms, any_alarm, soc, cell_voltages.
        """
        backoff = 2.0
        while self._running:
            try:
                async with self._make_client(BRIDGE_HOST, BRIDGE_PORT,
                                             self.username, self.password,
                                             f"{MQTT_CLIENT_ID}-bridge") as bridge:
                    log.info(f"Bridge MQTT connecté : {BRIDGE_HOST}:{BRIDGE_PORT}")
                    backoff = 2.0

                    while self._running:
                        for bms_id, snap in list(self._snapshots.items()):
                            for subtopic, payload, qos, retain in [
                                ("status",       _status_payload(snap),      1, True),
                                ("alarms",       _alarm_payload(snap),       1, True),
                                ("any_alarm",    _to_payload(snap.get("any_alarm")), 1, True),
                                ("soc",          _to_payload(snap.get("soc")),       0, True),
                                ("voltage",      _to_payload(snap.get("pack_voltage")), 0, True),
                                ("current",      _to_payload(snap.get("pack_current")), 0, False),
                                ("cell_voltages", _cell_voltages_payload(snap, self.cell_count), 0, False),
                                ("cell_delta",   _to_payload(snap.get("cell_delta")), 0, False),
                                ("temp_max",     _to_payload(snap.get("temp_max")), 0, False),
                            ]:
                                bridge_topic = f"{BRIDGE_PREFIX}/{bms_id}/{BMS_NAMES.get(bms_id, f'bms{bms_id}')}/{subtopic}"
                                await bridge.publish(bridge_topic, payload=payload,
                                                     qos=qos, retain=retain)
                        await asyncio.sleep(self.publish_interval)

            except MqttError as exc:
                log.warning(f"Bridge MQTT déconnecté : {exc} — reconnexion dans {backoff}s")
                await asyncio.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                break

    # ── Helper client ─────────────────────────────────────────────────────────

    def _make_client(self, host: str, port: int,
                     username: str, password: str,
                     client_id: str) -> Client:
        kwargs: dict = {
            "hostname":  host,
            "port":      port,
            "identifier": client_id,
            "will": aiomqtt.Will(
                topic=topic_system("online"),
                payload="false",
                qos=1,
                retain=True,
            ),
            "keepalive": 30,
        }
        if username:
            kwargs["username"] = username
            kwargs["password"] = password
        return Client(**kwargs)


# ─── Intégration avec AppState (daly_api.py) ─────────────────────────────────
class MqttBridge:
    """
    Pont entre le poll_loop de DalyWriteManager et DalyMqttPublisher.
    S'injecte comme callback dans poll_loop pour recevoir les snapshots.

    Usage dans daly_api.py lifespan :
        mqtt = MqttBridge(); mqtt.start()
        # remplacer _on_snapshot par :
        await state.manager.poll_loop(mqtt.on_snapshot, POLL_INTERVAL)
    """

    def __init__(self, publisher: Optional[DalyMqttPublisher] = None):
        self.publisher = publisher or DalyMqttPublisher()

    def start(self):
        self.publisher.start()

    async def stop(self):
        await self.publisher.stop()

    async def on_snapshot(self, snapshots: dict):
        """Callback compatible avec poll_loop — reçoit dict[int, BmsSnapshot]."""
        from daly_protocol import snapshot_to_dict
        for bms_id, snap in snapshots.items():
            d = snap if isinstance(snap, dict) else snapshot_to_dict(snap)
            self.publisher.update(bms_id, d)


# ─── Référence complète des topics publiés ────────────────────────────────────
TOPIC_REFERENCE = """
TOPICS MQTT — DalyBMS Interface
Préfixe : santuario/bms/{id}/{name}/

MÉTRIQUES SCALAIRES (valeur string, ex: "54.32")
  soc                   — State of Charge en % (retain=true)
  voltage               — Tension pack en V (retain=true)
  current               — Courant en A (positif=charge)
  power                 — Puissance en W
  cell_min_v            — Tension cellule minimale en mV
  cell_max_v            — Tension cellule maximale en mV
  cell_delta            — Delta min/max cellules en mV (retain=true)
  temp_max              — Température maximale sondes °C
  temp_min              — Température minimale sondes °C
  charge_mos            — État MOSFET charge true/false (retain=true)
  discharge_mos         — État MOSFET décharge true/false (retain=true)
  bms_cycles            — Compteur cycles (retain=true)
  remaining_capacity    — Capacité restante Ah
  any_alarm             — Alarme active true/false (retain=true, QoS 1)

TABLEAUX JSON
  cell_voltages         — [3300, 3310, ..., 3305] — 16 valeurs mV
  temperatures          — [25.0, 26.5, ...] — valeurs °C par sonde
  balancing             — [0, 0, 1, 0, ...] — 1 si cellule en balancing

OBJETS JSON
  alarms                — {cell_ovp: false, cell_uvp: false, ...} (retain, QoS 1)
  status                — résumé complet (retain, QoS 1) — compatible dbus-mqtt

CELLULES INDIVIDUELLES
  cells/cell_01         — tension cellule 1 en mV
  cells/cell_16         — tension cellule 16 en mV

SYSTÈME
  santuario/bms/system/online   — true/false (LWT, retain)
  santuario/bms/system/status   — {active_bms, any_alarm, ts} (retain)
"""


# ─── Point d'entrée standalone ────────────────────────────────────────────────
async def _demo():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    from daly_write import DalyWriteManager

    publisher = DalyMqttPublisher()
    bridge    = MqttBridge(publisher)
    bridge.start()

    async with DalyWriteManager(
        os.getenv("DALY_PORT", "/dev/ttyUSB1"), [0x01, 0x02]
    ) as mgr:
        log.info("Démarrage poll_loop → MQTT...")
        await mgr.poll_loop(bridge.on_snapshot, PUBLISH_INTERVAL)


if __name__ == "__main__":
    asyncio.run(_demo())
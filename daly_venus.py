"""
daly_venus.py — D9 : Bridge MQTT ↔ Venus OS / NanoPi  [DÉPRÉCIÉ]

⚠  Ce module est déprécié depuis l'installation de dbus-mqtt-battery (mr-manuel)
   sur le NanoPi. Il n'est plus lancé par défaut.

   Approche remplacée par :
     - daly_mqtt.py  : publie {MQTT_PREFIX}/{bms_id}/venus (JSON complet)
     - NanoPi        : dbus-mqtt-battery-41/42 subscribe à ce topic
                       et expose com.victronenergy.battery sur le D-Bus local

   Avantages de la nouvelle approche :
     - Meilleure visibilité VRM (graphiques, historique, alarmes Victron)
     - DVCC : l'MPPT respecte les limites CCL/CVL transmises
     - Time-To-Go calculé par le driver
     - Keepalive géré côté driver, pas dans notre code
     - Maintenance réduite (driver communautaire maintenu)

   Ce fichier est conservé pour référence. Ne pas activer en production
   simultanément avec dbus-mqtt-battery (instances D-Bus conflictuelles).

Publication des données BMS Daly vers Venus OS via dbus-mqtt-devices.
Services virtuels dbus : com.victronenergy.battery (×2), com.victronenergy.meteo.
Abonné MQTT du NanoPi (port 1883) — RPi CM5 = satellite pur, NanoPi = GX master.
Installation Santuario — Badalucco
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiomqtt

log = logging.getLogger("daly.venus")

# ─── Configuration ────────────────────────────────────────────────────────────
# MQTT NanoPi (broker Venus OS)
NANOPI_HOST      = os.getenv("NANOPI_MQTT_HOST",   "192.168.1.120")
NANOPI_PORT      = int(os.getenv("NANOPI_MQTT_PORT", "1883"))
NANOPI_USER      = os.getenv("NANOPI_MQTT_USER",   "")
NANOPI_PASS      = os.getenv("NANOPI_MQTT_PASS",   "")
NANOPI_CLIENT_ID = os.getenv("NANOPI_CLIENT_ID",   "dalybms-venus-bridge")

# Portal ID Venus OS — récupéré via MQTT N/+/system/0/Serial
PORTAL_ID        = os.getenv("VENUS_PORTAL_ID",    "c0619ab9929a")

# Instances dbus (doivent être uniques sur le GX)
# com.victronenergy.battery
INST_BMS1        = int(os.getenv("VENUS_BMS1_INSTANCE", "10"))   # Pack 320Ah
INST_BMS2        = int(os.getenv("VENUS_BMS2_INSTANCE", "11"))   # Pack 360Ah
# com.victronenergy.meteo (irradiance — si non géré par NanoPi directement)
INST_METEO       = int(os.getenv("VENUS_METEO_INSTANCE", "20"))

# Topics source (broker local RPi CM5)
LOCAL_MQTT_HOST  = os.getenv("LOCAL_MQTT_HOST",    "localhost")
LOCAL_MQTT_PORT  = int(os.getenv("LOCAL_MQTT_PORT", "1883"))
LOCAL_PREFIX     = os.getenv("MQTT_PREFIX",         "santuario/bms")

# Intervalles
PUBLISH_INTERVAL = float(os.getenv("VENUS_PUBLISH_INTERVAL", "5.0"))
KEEPALIVE_INTERVAL = float(os.getenv("VENUS_KEEPALIVE_INTERVAL", "55.0"))

# Noms BMS
BMS_NAMES = {
    1: os.getenv("BMS1_PRODUCT_NAME", "Daly LiFePO4 320Ah"),
    2: os.getenv("BMS2_PRODUCT_NAME", "Daly LiFePO4 360Ah"),
}
BMS_CAPACITY = {
    1: float(os.getenv("BMS1_CAPACITY_AH", "320")),
    2: float(os.getenv("BMS2_CAPACITY_AH", "360")),
}


# ─── Helpers topic dbus-mqtt-devices ─────────────────────────────────────────
def _W(portal: str, service: str, instance: int, path: str) -> str:
    """
    Topic d'écriture dbus-mqtt-devices.
    Format : W/{portalId}/{service}/{instance}/{path}
    """
    return f"W/{portal}/{service}/{instance}/{path}"


def _N(portal: str, service: str, instance: int, path: str) -> str:
    """Topic de lecture (N = notification)."""
    return f"N/{portal}/{service}/{instance}/{path}"


def _val(v) -> str:
    """Sérialise une valeur au format dbus-mqtt-devices : {"value": v}"""
    return json.dumps({"value": v})


# ─── Mapping dbus paths com.victronenergy.battery ────────────────────────────
# Référence : https://github.com/victronenergy/venus/wiki/dbus
#
# Paths obligatoires pour reconnaissance par Venus OS :
#   /Dc/0/Voltage          V       tension pack
#   /Dc/0/Current          A       courant (+ = charge, - = décharge)
#   /Dc/0/Power            W       puissance
#   /Dc/0/Temperature      °C      température max
#   /Soc                   %       état de charge
#   /Capacity              Ah      capacité nominale
#   /ConsumedAmphours      Ah      Ah consommés depuis plein
#   /TimeToGo              s       temps restant estimé
#   /Info/BatteryLowVoltage V      seuil UVP
#   /Info/MaxChargeCurrent  A      courant max charge
#   /Info/MaxDischargeCurrent A    courant max décharge
#   /Info/MaxChargeVoltage  V      tension max charge (CVL)
#   /Io/AllowToCharge       0/1    autorisation charge
#   /Io/AllowToDischarge    0/1    autorisation décharge
#   /System/NrOfCellsPerBattery int
#   /System/NrOfModulesOnline   int
#   /System/NrOfModulesOffline  int
#   /System/MinCellVoltage  V      tension cellule min
#   /System/MaxCellVoltage  V      tension cellule max
#   /System/MinCellTemperature °C
#   /System/MaxCellTemperature °C
#   /Alarms/Alarm           0/1    alarme générale
#   /Alarms/LowVoltage      0/1
#   /Alarms/HighVoltage     0/1
#   /Alarms/LowTemperature  0/1
#   /Alarms/HighTemperature 0/1
#   /Alarms/LowSoc          0/1
#   /ProductName            str
#   /FirmwareVersion        str
#   /HardwareVersion        str
#   /Connected              1

def build_battery_paths(snap: dict, capacity_ah: float,
                        product_name: str) -> dict[str, object]:
    """
    Construit le dictionnaire path → valeur pour com.victronenergy.battery
    à partir d'un snapshot BMS Daly.
    """
    soc     = snap.get("soc", 0)
    volt    = snap.get("pack_voltage", 0)
    curr    = snap.get("pack_current", 0)       # + = charge, - = décharge
    power   = snap.get("power", 0)
    temp_max= snap.get("temp_max", 25)
    temp_min= snap.get("temp_min", 25)
    cap_rem = snap.get("remaining_capacity", 0)
    cell_min= (snap.get("cell_min_v") or 0) / 1000   # mV → V
    cell_max= (snap.get("cell_max_v") or 0) / 1000
    chg_mos = snap.get("charge_mos", True)
    dsg_mos = snap.get("discharge_mos", True)
    alarms  = snap.get("alarms", {})
    n_cells = snap.get("n_cells", 16)

    # Estimation time-to-go (s) — si en décharge uniquement
    ttg = None
    if curr < -0.5 and cap_rem > 0:
        ttg = int((cap_rem / abs(curr)) * 3600)

    # Ah consommés depuis plein
    consumed = max(0, capacity_ah - cap_rem)

    # CVL (Charge Voltage Limit) = 3.55V/cell × 16 = 56.8V
    cvl  = 3.55 * n_cells
    # CCL (Charge Current Limit)
    ccl  = 70.0 if chg_mos else 0.0
    # DCL (Discharge Current Limit)
    dcl  = 100.0 if dsg_mos else 0.0
    # Low voltage warning
    uvp  = 2.80 * n_cells

    return {
        # Mesures DC
        "/Dc/0/Voltage":                volt,
        "/Dc/0/Current":                curr,
        "/Dc/0/Power":                  power,
        "/Dc/0/Temperature":            temp_max,

        # SOC & énergie
        "/Soc":                         soc,
        "/Capacity":                    capacity_ah,
        "/ConsumedAmphours":            round(consumed, 2),
        "/TimeToGo":                    ttg,

        # Limites charger / BMS
        "/Info/BatteryLowVoltage":      round(uvp, 2),
        "/Info/MaxChargeCurrent":       ccl,
        "/Info/MaxDischargeCurrent":    dcl,
        "/Info/MaxChargeVoltage":       round(cvl, 2),

        # Permissions MOS
        "/Io/AllowToCharge":            1 if chg_mos else 0,
        "/Io/AllowToDischarge":         1 if dsg_mos else 0,

        # Cellules
        "/System/NrOfCellsPerBattery":  n_cells,
        "/System/NrOfModulesOnline":    1,
        "/System/NrOfModulesOffline":   0,
        "/System/MinCellVoltage":       round(cell_min, 4),
        "/System/MaxCellVoltage":       round(cell_max, 4),
        "/System/MinCellTemperature":   temp_min,
        "/System/MaxCellTemperature":   temp_max,

        # Alarmes
        "/Alarms/Alarm":                1 if snap.get("any_alarm") else 0,
        "/Alarms/LowVoltage":           1 if alarms.get("cell_uvp") or alarms.get("pack_uvp") else 0,
        "/Alarms/HighVoltage":          1 if alarms.get("cell_ovp") or alarms.get("pack_ovp") else 0,
        "/Alarms/LowTemperature":       0,
        "/Alarms/HighTemperature":      1 if alarms.get("chg_otp") else 0,
        "/Alarms/LowSoc":               1 if soc < 20 else 0,

        # Identité
        "/ProductName":                 product_name,
        "/FirmwareVersion":             "daly-bridge-1.0",
        "/HardwareVersion":             "Daly Smart BMS 16S",
        "/Connected":                   1,
    }


# ─── Mapping dbus paths com.victronenergy.meteo ───────────────────────────────
# Utilisé si l'irradiance CWT-SI est re-publiée depuis RPi CM5 vers Venus OS.
# Path principal : /Irradiance (W/m²)

def build_meteo_paths(irradiance_wm2: float) -> dict[str, object]:
    return {
        "/Irradiance":   round(irradiance_wm2, 1),
        "/ProductName":  "CWT-SI PR-300",
        "/Connected":    1,
    }


# ─── Classe principale : VenusBridge ─────────────────────────────────────────
class VenusBridge:
    """
    Abonné MQTT local (broker RPi CM5) + publisher MQTT NanoPi (Venus OS).

    Flux :
      1. Souscription aux topics santuario/bms/{id}/+/status (JSON complet)
      2. Traduction snapshot → paths dbus-mqtt-devices
      3. Publication W/{portalId}/battery/{instance}/{path} → NanoPi

    Keepalive :
      dbus-mqtt-devices exige une publication sur chaque path au moins
      toutes les 60s pour maintenir le service "Connected" sur le dbus.
      Un timer de 55s re-publie tous les paths connus.
    """

    def __init__(self):
        self._snapshots: dict[int, dict] = {}
        self._irradiance: float = 0.0
        self._last_pub:   dict[int, float] = {}
        self._running     = False

    # ── Topics source (broker local) ─────────────────────────────────────────

    def _local_topics(self) -> list[str]:
        """Topics MQTT locaux à souscrire."""
        return [
            f"{LOCAL_PREFIX}/+/+/status",         # JSON snapshot complet
            f"{LOCAL_PREFIX}/+/+/soc",             # SOC scalaire
            "santuario/meteo/irradiance",          # Irradiance CWT-SI
        ]

    def _parse_local_message(self, topic: str, payload: str):
        """Traite un message MQTT local et met à jour l'état interne."""
        parts = topic.split("/")
        try:
            # santuario/bms/{bms_id}/{bms_name}/status
            if len(parts) >= 5 and parts[-1] == "status":
                bms_id = int(parts[2])
                snap   = json.loads(payload)
                self._snapshots[bms_id] = snap
                log.debug(f"Snapshot BMS{bms_id} reçu — SOC={snap.get('soc')}%")

            # santuario/meteo/irradiance
            elif "meteo" in parts and "irradiance" in parts:
                self._irradiance = float(json.loads(payload).get("value",
                                        payload if payload.replace(".", "").isnumeric()
                                        else 0))
        except Exception as exc:
            log.debug(f"Parse message {topic} : {exc}")

    # ── Publication Venus OS ──────────────────────────────────────────────────

    async def _publish_battery(self, client: aiomqtt.Client,
                               bms_id: int, snap: dict):
        """Publie tous les paths com.victronenergy.battery pour un BMS."""
        instance     = INST_BMS1 if bms_id == 1 else INST_BMS2
        product_name = BMS_NAMES.get(bms_id, f"Daly BMS {bms_id}")
        capacity     = BMS_CAPACITY.get(bms_id, 320)

        paths = build_battery_paths(snap, capacity, product_name)

        for path, value in paths.items():
            if value is None:
                continue
            topic = _W(PORTAL_ID, "battery", instance, path.lstrip("/"))
            try:
                await client.publish(topic, _val(value), qos=0, retain=False)
            except Exception as exc:
                log.warning(f"Publish {topic} : {exc}")

        self._last_pub[bms_id] = time.time()
        log.info(f"[BMS{bms_id}] → Venus OS : {len(paths)} paths publiés "
                 f"(inst={instance})")

    async def _publish_meteo(self, client: aiomqtt.Client):
        """Publie com.victronenergy.meteo si irradiance disponible."""
        paths = build_meteo_paths(self._irradiance)
        for path, value in paths.items():
            topic = _W(PORTAL_ID, "meteo", INST_METEO, path.lstrip("/"))
            try:
                await client.publish(topic, _val(value), qos=0, retain=False)
            except Exception as exc:
                log.warning(f"Publish meteo {topic} : {exc}")

    async def _publish_all(self, client: aiomqtt.Client):
        """Publication groupée de tous les BMS + meteo."""
        for bms_id, snap in self._snapshots.items():
            await self._publish_battery(client, bms_id, snap)
        if self._irradiance > 0:
            await self._publish_meteo(client)

    # ── Boucle principale ─────────────────────────────────────────────────────

    async def run(self):
        """
        Double boucle asyncio :
        - tâche subscriber  : lit les topics locaux
        - tâche publisher   : publie vers NanoPi sur timer
        Les deux partagent l'état interne via self._snapshots.
        """
        self._running = True
        log.info(f"VenusBridge démarrage — NanoPi={NANOPI_HOST}:{NANOPI_PORT} "
                 f"portal={PORTAL_ID}")

        while self._running:
            try:
                await asyncio.gather(
                    self._subscriber_loop(),
                    self._publisher_loop(),
                )
            except Exception as exc:
                log.error(f"VenusBridge erreur : {exc} — retry 10s", exc_info=True)
                await asyncio.sleep(10)

    async def _subscriber_loop(self):
        """Connexion au broker local et souscription aux topics source."""
        async with aiomqtt.Client(
            hostname=LOCAL_MQTT_HOST,
            port=LOCAL_MQTT_PORT,
            identifier=f"{NANOPI_CLIENT_ID}-sub",
            keepalive=30,
        ) as client:
            for topic in self._local_topics():
                await client.subscribe(topic, qos=0)
            log.info(f"Subscriber local connecté — topics: {self._local_topics()}")
            async for msg in client.messages:
                if not self._running:
                    break
                self._parse_local_message(
                    str(msg.topic),
                    msg.payload.decode("utf-8", errors="replace"),
                )

    async def _publisher_loop(self):
        """Connexion au broker NanoPi et publication périodique."""
        nanopi_cfg = dict(
            hostname  = NANOPI_HOST,
            port      = NANOPI_PORT,
            identifier= f"{NANOPI_CLIENT_ID}-pub",
            keepalive = 30,
        )
        if NANOPI_USER:
            nanopi_cfg["username"] = NANOPI_USER
            nanopi_cfg["password"] = NANOPI_PASS

        async with aiomqtt.Client(**nanopi_cfg) as client:
            log.info(f"Publisher NanoPi connecté — {NANOPI_HOST}:{NANOPI_PORT}")
            # Annonce initiale connected=1 pour chaque service
            await self._announce_services(client)

            while self._running:
                await asyncio.sleep(PUBLISH_INTERVAL)
                if self._snapshots:
                    await self._publish_all(client)
                else:
                    log.debug("Aucun snapshot disponible — attente...")

    async def _announce_services(self, client: aiomqtt.Client):
        """
        Publie /Connected=1 pour chaque service dbus au démarrage.
        Requis par dbus-mqtt-devices pour créer l'objet dbus.
        """
        for bms_id in [1, 2]:
            inst = INST_BMS1 if bms_id == 1 else INST_BMS2
            name = BMS_NAMES.get(bms_id, f"Daly BMS {bms_id}")
            for path, val in [
                ("Connected",    1),
                ("ProductName",  name),
                ("FirmwareVersion", "daly-bridge-1.0"),
            ]:
                topic = _W(PORTAL_ID, "battery", inst, path)
                await client.publish(topic, _val(val), qos=1, retain=False)
        log.info("Services dbus annoncés (Connected=1)")

    async def stop(self):
        self._running = False


# ─── Publication directe depuis AlertBridge / MqttBridge ─────────────────────
class VenusPublisher:
    """
    Variante simplifiée pour injection directe depuis daly_api.py.
    Usage :
        venus = VenusPublisher()
        await venus.start()
        # dans on_snapshot :
        await venus.on_snapshot(snapshots)
        await venus.stop()
    """

    def __init__(self):
        self._client:   Optional[aiomqtt.Client] = None
        self._cm        = None
        self._connected = False
        self._snapshots: dict[int, dict] = {}
        self._task:     Optional[asyncio.Task] = None
        self._running   = False

    async def start(self):
        self._running = True
        self._task    = asyncio.create_task(self._connect_loop(), name="venus-pub")
        log.info("VenusPublisher démarré")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connect_loop(self):
        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname  = NANOPI_HOST,
                    port      = NANOPI_PORT,
                    identifier= NANOPI_CLIENT_ID,
                    keepalive = 30,
                ) as client:
                    self._client    = client
                    self._connected = True
                    log.info(f"VenusPublisher connecté → {NANOPI_HOST}")
                    # Annonce
                    for bms_id in [1, 2]:
                        inst = INST_BMS1 if bms_id == 1 else INST_BMS2
                        await client.publish(
                            _W(PORTAL_ID, "battery", inst, "Connected"),
                            _val(1), qos=1,
                        )
                    # Keepalive loop
                    while self._running:
                        await asyncio.sleep(1)
            except Exception as exc:
                self._connected = False
                log.warning(f"VenusPublisher déconnecté : {exc} — retry 15s")
                await asyncio.sleep(15)

    async def on_snapshot(self, snapshots: dict):
        """Appelé depuis poll_loop — publie vers NanoPi si connecté."""
        from daly_protocol import snapshot_to_dict
        if not self._connected or not self._client:
            return
        for bms_id, snap in snapshots.items():
            d    = snap if isinstance(snap, dict) else snapshot_to_dict(snap)
            inst = INST_BMS1 if bms_id == 1 else INST_BMS2
            cap  = BMS_CAPACITY.get(bms_id, 320)
            name = BMS_NAMES.get(bms_id, f"Daly BMS {bms_id}")
            paths = build_battery_paths(d, cap, name)
            for path, value in paths.items():
                if value is None:
                    continue
                try:
                    await self._client.publish(
                        _W(PORTAL_ID, "battery", inst, path.lstrip("/")),
                        _val(value), qos=0,
                    )
                except Exception as exc:
                    log.debug(f"Publish {path} : {exc}")


# ─── Utilitaires diagnostic ───────────────────────────────────────────────────
async def discover_portal_id(host: str = NANOPI_HOST,
                             port: int = NANOPI_PORT,
                             timeout: float = 10.0) -> Optional[str]:
    """
    Découverte automatique du Portal ID Venus OS.
    Souscrit à N/+/system/0/Serial et retourne la valeur dès réception.
    """
    found: Optional[str] = None

    async def _probe():
        nonlocal found
        async with aiomqtt.Client(
            hostname  = host,
            port      = port,
            identifier= "daly-discover",
            keepalive = 15,
        ) as client:
            await client.subscribe("N/+/system/0/Serial", qos=0)
            async for msg in client.messages:
                parts = str(msg.topic).split("/")
                if len(parts) >= 2:
                    found = parts[1]
                    log.info(f"Portal ID découvert : {found}")
                break

    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Découverte Portal ID timeout — vérifier connexion NanoPi")
    return found


async def list_dbus_services(host: str = NANOPI_HOST,
                             port: int  = NANOPI_PORT,
                             portal_id: str = PORTAL_ID,
                             timeout: float = 5.0) -> list[str]:
    """
    Liste les services dbus actifs sur Venus OS via MQTT.
    Publie keepalive N/{portal}/+/# et collecte les topics.
    """
    topics_seen: set[str] = set()

    async def _scan():
        async with aiomqtt.Client(
            hostname  = host,
            port      = port,
            identifier= "daly-scan",
            keepalive = 10,
        ) as client:
            await client.subscribe(f"N/{portal_id}/#", qos=0)
            deadline = time.time() + timeout
            async for msg in client.messages:
                parts = str(msg.topic).split("/")
                if len(parts) >= 3:
                    topics_seen.add(f"{parts[1]}/{parts[2]}")
                if time.time() > deadline:
                    break

    try:
        await asyncio.wait_for(_scan(), timeout=timeout + 1)
    except asyncio.TimeoutError:
        pass
    return sorted(topics_seen)


# ─── Script de vérification / commissioning ──────────────────────────────────
async def commissioning_check():
    """
    Vérifie la connectivité et publie un test vers Venus OS.
    Sortie structurée pour diagnostic.
    """
    log.info("=== D9 Commissioning Check ===")

    # 1. Découverte Portal ID
    pid = await discover_portal_id()
    if not pid:
        log.error("ÉCHEC : Portal ID non trouvé — NanoPi inaccessible")
        return False
    log.info(f"Portal ID : {pid}")

    # 2. Liste services existants
    services = await list_dbus_services(portal_id=pid)
    log.info(f"Services dbus actifs ({len(services)}) :")
    for svc in services:
        log.info(f"  {svc}")

    # 3. Test publication battery instance 10
    async with aiomqtt.Client(
        hostname  = NANOPI_HOST,
        port      = NANOPI_PORT,
        identifier= "daly-test",
        keepalive = 10,
    ) as client:
        test_paths = {
            "Connected":    1,
            "ProductName":  "DalyBMS-Test",
            "Dc/0/Voltage": 53.2,
            "Dc/0/Current": 10.0,
            "Soc":          75.0,
        }
        for path, val in test_paths.items():
            topic = _W(pid, "battery", INST_BMS1, path)
            await client.publish(topic, _val(val), qos=1)
            log.info(f"  Publié : {topic} = {val}")
        await asyncio.sleep(2)

    log.info("=== Commissioning OK — Vérifier VRM : Services > battery ===")
    return True


# ─── Service systemd standalone ───────────────────────────────────────────────
async def _main():
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        await commissioning_check()
        return

    bridge = VenusBridge()
    try:
        await bridge.run()
    except KeyboardInterrupt:
        log.info("Arrêt VenusBridge")
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(_main())
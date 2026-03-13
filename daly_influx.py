"""
daly_influx.py — D5 : Persistance InfluxDB Time-Series
Schéma measurements, tags, batch write, downsampling, rétention.
Dépend de : daly_protocol.py (D1)
Installation Santuario — Badalucco
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import ASYNCHRONOUS, SYNCHRONOUS
from influxdb_client.client.exceptions import InfluxDBError
from influxdb_client.domain.bucket import Bucket

log = logging.getLogger("daly.influx")

# ─── Configuration ────────────────────────────────────────────────────────────
INFLUX_URL        = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN      = os.getenv("INFLUX_TOKEN",  "")
INFLUX_ORG        = os.getenv("INFLUX_ORG",   "santuario")
INFLUX_BUCKET     = os.getenv("INFLUX_BUCKET", "daly_bms")
INFLUX_BUCKET_DS  = os.getenv("INFLUX_BUCKET_DS", "daly_bms_1m")  # downsampled
BATCH_SIZE        = int(os.getenv("INFLUX_BATCH_SIZE", "50"))
BATCH_INTERVAL_S  = float(os.getenv("INFLUX_BATCH_INTERVAL", "5.0"))
RETENTION_DAYS    = int(os.getenv("INFLUX_RETENTION_DAYS", "30"))
WRITE_INTERVAL    = float(os.getenv("INFLUX_WRITE_INTERVAL", "1.0"))

BMS_NAMES = {
    0x01: os.getenv("INFLUX_BMS1_NAME", "pack_320ah"),
    0x02: os.getenv("INFLUX_BMS2_NAME", "pack_360ah"),
}

INSTALLATION = os.getenv("INFLUX_INSTALLATION", "santuario")

# ─── Tags communs ─────────────────────────────────────────────────────────────
def _base_tags(bms_id: int) -> dict:
    return {
        "bms_id":       str(bms_id),
        "bms_name":     BMS_NAMES.get(bms_id, f"bms{bms_id}"),
        "installation": INSTALLATION,
    }


# ─── Constructeurs de Points InfluxDB ────────────────────────────────────────
def _point_status(snap: dict, bms_id: int) -> Optional[Point]:
    """
    Measurement : bms_status
    Tags        : bms_id, bms_name, installation
    Fields      : soc, voltage, current, power, remaining_capacity,
                  bms_cycles, charge_mos, discharge_mos, any_alarm
    """
    ts = snap.get("timestamp")
    if ts is None:
        return None
    p = Point("bms_status").time(int(ts * 1e9), WritePrecision.NANOSECONDS)
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    fields = {
        "soc":                snap.get("soc"),
        "voltage":            snap.get("pack_voltage"),
        "current":            snap.get("pack_current"),
        "power":              snap.get("power"),
        "remaining_capacity": snap.get("remaining_capacity"),
        "bms_cycles":         snap.get("bms_cycles"),
        "charge_mos":         int(snap.get("charge_mos", False)),
        "discharge_mos":      int(snap.get("discharge_mos", False)),
        "any_alarm":          int(snap.get("any_alarm", False)),
    }
    for k, v in fields.items():
        if v is not None:
            p = p.field(k, float(v) if isinstance(v, (int, float)) else v)
    return p


def _point_cells(snap: dict, bms_id: int, cell_count: int = 16) -> Optional[Point]:
    """
    Measurement : bms_cells
    Tags        : bms_id, bms_name, installation
    Fields      : cell_01 … cell_16 (mV), cell_min, cell_max, cell_avg, cell_delta
    """
    ts = snap.get("timestamp")
    if ts is None:
        return None
    p = Point("bms_cells").time(int(ts * 1e9), WritePrecision.NANOSECONDS)
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    has_data = False
    for i in range(1, cell_count + 1):
        v = snap.get(f"cell_{i:02d}")
        if v is not None:
            p = p.field(f"cell_{i:02d}", float(v))
            has_data = True
    for key in ("cell_min_v", "cell_max_v", "cell_avg", "cell_delta"):
        v = snap.get(key)
        if v is not None:
            p = p.field(key, float(v))
            has_data = True
    return p if has_data else None


def _point_temperatures(snap: dict, bms_id: int, sensor_count: int = 4) -> Optional[Point]:
    """
    Measurement : bms_temperatures
    Tags        : bms_id, bms_name, installation
    Fields      : sensor_01 … sensor_N (°C), temp_min, temp_max
    """
    ts = snap.get("timestamp")
    if ts is None:
        return None
    p = Point("bms_temperatures").time(int(ts * 1e9), WritePrecision.NANOSECONDS)
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    has_data = False
    for i in range(1, sensor_count + 1):
        v = snap.get(f"temp_{i:02d}")
        if v is not None:
            p = p.field(f"sensor_{i:02d}", float(v))
            has_data = True
    for key in ("temp_min", "temp_max"):
        v = snap.get(key)
        if v is not None:
            p = p.field(key, float(v))
            has_data = True
    return p if has_data else None


def _point_alarms(snap: dict, bms_id: int) -> Optional[Point]:
    """
    Measurement : bms_alarms
    Tags        : bms_id, bms_name, installation
    Fields      : chaque flag de protection en int (0/1)
    """
    ts = snap.get("timestamp")
    if ts is None:
        return None
    p = Point("bms_alarms").time(int(ts * 1e9), WritePrecision.NANOSECONDS)
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    alarm_keys = [
        "alarm_cell_ovp", "alarm_cell_uvp", "alarm_pack_ovp", "alarm_pack_uvp",
        "alarm_chg_otp",  "alarm_chg_ocp",  "alarm_dsg_ocp",  "alarm_scp",
        "alarm_cell_delta", "any_alarm",
    ]
    for key in alarm_keys:
        v = snap.get(key)
        if v is not None:
            p = p.field(key.removeprefix("alarm_"), int(bool(v)))
    return p


def _point_event(bms_id: int, event_type: str,
                 event_value: float, trigger_count: int = 1) -> Point:
    """
    Measurement : bms_events
    Tags        : bms_id, bms_name, installation, event_type
    Fields      : event_value, trigger_count
    Utilisé pour journaliser les transitions d'alarme et commandes critiques.
    """
    p = (Point("bms_events")
         .time(int(time.time() * 1e9), WritePrecision.NANOSECONDS)
         .tag("event_type", event_type)
         .field("event_value", float(event_value))
         .field("trigger_count", int(trigger_count)))
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    return p


def _point_balancing(snap: dict, bms_id: int) -> Optional[Point]:
    """
    Measurement : bms_balancing
    Tags        : bms_id, bms_name, installation
    Fields      : cell_01_bal … cell_16_bal (0/1), active_count
    """
    ts = snap.get("timestamp")
    mask = snap.get("balancing_mask", [])
    if ts is None or not mask:
        return None
    p = Point("bms_balancing").time(int(ts * 1e9), WritePrecision.NANOSECONDS)
    for tag, val in _base_tags(bms_id).items():
        p = p.tag(tag, val)
    active = 0
    for i, b in enumerate(mask[:16], 1):
        p = p.field(f"cell_{i:02d}_bal", int(b))
        active += int(b)
    p = p.field("active_count", active)
    return p


# ─── Batch writer ─────────────────────────────────────────────────────────────
class InfluxBatchWriter:
    """
    Buffer de Points avec flush automatique sur taille ou délai.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, write_api, bucket: str = INFLUX_BUCKET,
                 org: str = INFLUX_ORG,
                 batch_size: int = BATCH_SIZE,
                 batch_interval: float = BATCH_INTERVAL_S):
        self._api           = write_api
        self._bucket        = bucket
        self._org           = org
        self._batch_size    = batch_size
        self._batch_interval = batch_interval
        self._buffer: list[Point] = []
        self._lock          = asyncio.Lock()
        self._last_flush    = time.monotonic()
        self._task: Optional[asyncio.Task] = None
        self._written_total = 0
        self._errors_total  = 0

    def start(self):
        self._task = asyncio.create_task(self._flush_loop(), name="influx-flush")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()
        log.info(f"InfluxBatchWriter arrêté — {self._written_total} points écrits, "
                 f"{self._errors_total} erreurs")

    async def add(self, points: list[Point]):
        """Ajoute des points au buffer. Flush automatique si batch_size atteint."""
        async with self._lock:
            self._buffer.extend(p for p in points if p is not None)
            if len(self._buffer) >= self._batch_size:
                await self._flush_locked()

    async def add_event(self, point: Point):
        """Ajout prioritaire d'un événement — flush immédiat."""
        async with self._lock:
            self._buffer.append(point)
            await self._flush_locked()

    async def _flush_loop(self):
        """Flush périodique sur délai même si batch_size non atteint."""
        while True:
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - self._last_flush
            if elapsed >= self._batch_interval:
                async with self._lock:
                    if self._buffer:
                        await self._flush_locked()

    async def _flush(self):
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self):
        """Doit être appelé avec self._lock acquis."""
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.monotonic()
        try:
            self._api.write(
                bucket=self._bucket,
                org=self._org,
                record=batch,
                write_precision=WritePrecision.NANOSECONDS,
            )
            self._written_total += len(batch)
            log.debug(f"InfluxDB flush : {len(batch)} points → {self._bucket}")
        except InfluxDBError as exc:
            self._errors_total += len(batch)
            log.error(f"InfluxDB erreur écriture : {exc} — {len(batch)} points perdus")
        except Exception as exc:
            self._errors_total += len(batch)
            log.error(f"InfluxDB erreur inattendue : {exc}", exc_info=True)

    @property
    def stats(self) -> dict:
        return {
            "buffer_size":    len(self._buffer),
            "written_total":  self._written_total,
            "errors_total":   self._errors_total,
        }


# ─── Classe principale ────────────────────────────────────────────────────────
class DalyInfluxWriter:
    """
    Écrit les snapshots BMS dans InfluxDB.
    Gère le batch write, la détection de transitions d'alarme,
    et l'écriture d'événements horodatés.

    Usage :
        async with DalyInfluxWriter() as writer:
            writer.update(bms_id, snap_dict)
    """

    def __init__(self,
                 url: str = INFLUX_URL,
                 token: str = INFLUX_TOKEN,
                 org: str = INFLUX_ORG,
                 bucket: str = INFLUX_BUCKET,
                 cell_count: int = 16,
                 sensor_count: int = 4):
        self.url          = url
        self.token        = token
        self.org          = org
        self.bucket       = bucket
        self.cell_count   = cell_count
        self.sensor_count = sensor_count

        self._client: Optional[InfluxDBClient]    = None
        self._writer: Optional[InfluxBatchWriter] = None
        self._prev_alarms: dict[int, dict]        = {}
        self._alarm_counters: dict[int, dict]     = {}

    async def __aenter__(self):
        self._client = InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=10_000,
        )
        write_api = self._client.write_api(write_options=SYNCHRONOUS)
        self._writer = InfluxBatchWriter(write_api, self.bucket, self.org)
        self._writer.start()
        log.info(f"DalyInfluxWriter connecté : {self.url} → bucket={self.bucket}")
        return self

    async def __aexit__(self, *args):
        if self._writer:
            await self._writer.stop()
        if self._client:
            self._client.close()
        log.info("DalyInfluxWriter fermé")

    def update(self, bms_id: int, snap: dict):
        """
        Traite un snapshot et l'écrit dans InfluxDB.
        Appelé depuis le poll_loop ou MqttBridge.
        """
        asyncio.create_task(self._write_snapshot(bms_id, snap))

    async def _write_snapshot(self, bms_id: int, snap: dict):
        points = [
            _point_status(snap, bms_id),
            _point_cells(snap, bms_id, self.cell_count),
            _point_temperatures(snap, bms_id, self.sensor_count),
            _point_alarms(snap, bms_id),
            _point_balancing(snap, bms_id),
        ]
        await self._writer.add([p for p in points if p is not None])
        await self._detect_alarm_events(bms_id, snap)

    async def _detect_alarm_events(self, bms_id: int, snap: dict):
        """
        Compare les flags d'alarme avec l'état précédent.
        Écrit un événement dans bms_events à chaque transition 0→1 ou 1→0.
        """
        alarm_keys = [
            "alarm_cell_ovp", "alarm_cell_uvp", "alarm_pack_ovp", "alarm_pack_uvp",
            "alarm_chg_otp",  "alarm_chg_ocp",  "alarm_dsg_ocp",  "alarm_scp",
            "alarm_cell_delta",
        ]
        prev = self._prev_alarms.get(bms_id, {})
        counters = self._alarm_counters.setdefault(bms_id, {k: 0 for k in alarm_keys})

        for key in alarm_keys:
            curr_val = bool(snap.get(key, False))
            prev_val = bool(prev.get(key, False))

            if curr_val and not prev_val:
                # Transition 0 → 1 : alarme déclenchée
                counters[key] = counters.get(key, 0) + 1
                event = _point_event(
                    bms_id=bms_id,
                    event_type=f"{key}_triggered",
                    event_value=snap.get("pack_voltage", 0.0),
                    trigger_count=counters[key],
                )
                await self._writer.add_event(event)
                log.warning(f"[BMS{bms_id}] Événement InfluxDB : {key} DÉCLENCHÉ "
                            f"(#{counters[key]})")

            elif not curr_val and prev_val:
                # Transition 1 → 0 : alarme effacée
                event = _point_event(
                    bms_id=bms_id,
                    event_type=f"{key}_cleared",
                    event_value=snap.get("pack_voltage", 0.0),
                    trigger_count=counters.get(key, 0),
                )
                await self._writer.add_event(event)
                log.info(f"[BMS{bms_id}] Événement InfluxDB : {key} EFFACÉ")

        self._prev_alarms[bms_id] = {k: bool(snap.get(k, False)) for k in alarm_keys}

    async def write_command_event(self, bms_id: int, command: str, value: float):
        """
        Journalise une commande manuelle (reset, calibration SOC, MOS forcé).
        Appelé depuis daly_api.py sur les routes POST critiques.
        """
        event = _point_event(bms_id, f"cmd_{command}", value)
        await self._writer.add_event(event)
        log.info(f"[BMS{bms_id}] Événement commande : {command} = {value}")

    @property
    def stats(self) -> dict:
        return self._writer.stats if self._writer else {}


# ─── Setup InfluxDB (bucket, org, tâches Flux) ────────────────────────────────
class InfluxSetup:
    """
    Crée les buckets, l'organisation et les tâches de downsampling Flux.
    À exécuter une seule fois lors de l'installation initiale.
    """

    def __init__(self, url: str = INFLUX_URL, token: str = INFLUX_TOKEN,
                 org: str = INFLUX_ORG):
        self.url   = url
        self.token = token
        self.org   = org

    def run(self):
        with InfluxDBClient(url=self.url, token=self.token, org=self.org) as client:
            self._ensure_buckets(client)
            self._create_downsampling_task(client)
        log.info("InfluxDB setup terminé")

    def _ensure_buckets(self, client: InfluxDBClient):
        buckets_api = client.buckets_api()
        existing    = {b.name: b for b in buckets_api.find_buckets().buckets}

        # Bucket principal — rétention 30 jours
        if INFLUX_BUCKET not in existing:
            buckets_api.create_bucket(
                bucket_name=INFLUX_BUCKET,
                org=self.org,
                retention_rules=[{
                    "type": "expire",
                    "everySeconds": RETENTION_DAYS * 86400,
                }]
            )
            log.info(f"Bucket créé : {INFLUX_BUCKET} (rétention {RETENTION_DAYS}j)")
        else:
            log.info(f"Bucket existant : {INFLUX_BUCKET}")

        # Bucket downsampled — rétention 365 jours
        if INFLUX_BUCKET_DS not in existing:
            buckets_api.create_bucket(
                bucket_name=INFLUX_BUCKET_DS,
                org=self.org,
                retention_rules=[{
                    "type": "expire",
                    "everySeconds": 365 * 86400,
                }]
            )
            log.info(f"Bucket downsampled créé : {INFLUX_BUCKET_DS} (rétention 365j)")
        else:
            log.info(f"Bucket downsampled existant : {INFLUX_BUCKET_DS}")

    def _create_downsampling_task(self, client: InfluxDBClient):
        """
        Tâche Flux de downsampling 1min :
        agrège mean/min/max des métriques clés du bucket principal
        vers le bucket downsampled toutes les minutes.
        """
        flux_script = f"""
option task = {{
  name: "daly_bms_downsample_1m",
  every: 1m,
  offset: 10s
}}

from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement =~ /^bms_/)
  |> filter(fn: (r) =>
      r._field == "soc"           or
      r._field == "voltage"       or
      r._field == "current"       or
      r._field == "power"         or
      r._field == "cell_delta"    or
      r._field == "temp_max"      or
      r._field == "cell_min_v"    or
      r._field == "cell_max_v"    or
      r._field == "any_alarm"
  )
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> to(bucket: "{INFLUX_BUCKET_DS}", org: "{self.org}")
"""
        tasks_api   = client.tasks_api()
        existing    = tasks_api.find_tasks(name="daly_bms_downsample_1m")
        if existing:
            log.info("Tâche Flux downsampling déjà existante")
            return
        tasks_api.create_task_with_flux_script(flux_script)
        log.info("Tâche Flux downsampling 1min créée")


# ─── Intégration avec AppState (daly_api.py) ─────────────────────────────────
class InfluxBridge:
    """
    Pont entre poll_loop et DalyInfluxWriter.
    Même pattern que MqttBridge pour cohérence d'architecture.

    Usage dans daly_api.py lifespan :
        influx = InfluxBridge(); await influx.start()
        # dans _on_snapshot :
        await influx.on_snapshot(snapshots)
    """

    def __init__(self, writer: Optional[DalyInfluxWriter] = None,
                 cell_count: int = 16, sensor_count: int = 4):
        self._writer = writer or DalyInfluxWriter(
            cell_count=cell_count, sensor_count=sensor_count
        )
        self._ctx = None

    async def start(self):
        self._ctx = self._writer
        await self._writer.__aenter__()
        log.info("InfluxBridge démarré")

    async def stop(self):
        if self._ctx:
            await self._writer.__aexit__(None, None, None)
        log.info("InfluxBridge arrêté")

    async def on_snapshot(self, snapshots: dict):
        """Callback compatible poll_loop — reçoit dict[int, BmsSnapshot]."""
        from daly_protocol import snapshot_to_dict
        for bms_id, snap in snapshots.items():
            d = snap if isinstance(snap, dict) else snapshot_to_dict(snap)
            self._writer.update(bms_id, d)

    @property
    def writer(self) -> DalyInfluxWriter:
        return self._writer


# ─── Script de setup standalone ───────────────────────────────────────────────
async def _demo():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )

    # Setup initial
    log.info("Initialisation InfluxDB...")
    setup = InfluxSetup()
    setup.run()

    # Test écriture avec données simulées
    from daly_write import DalyWriteManager

    bridge = InfluxBridge()
    await bridge.start()

    async with DalyWriteManager(
        os.getenv("DALY_PORT", "/dev/ttyUSB1"), [0x01, 0x02]
    ) as mgr:
        log.info("Démarrage poll_loop → InfluxDB...")
        await mgr.poll_loop(bridge.on_snapshot, WRITE_INTERVAL)

    await bridge.stop()


if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
        InfluxSetup().run()
    else:
        asyncio.run(_demo())
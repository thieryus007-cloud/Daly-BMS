"""
daly_api.py — D3 : API REST FastAPI + WebSocket
Endpoints GET/POST + stream temps réel pour interface web et intégrations externes.
Dépend de : daly_protocol.py (D1), daly_write.py (D2)
Installation Santuario — Badalucco
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import (
    Depends, FastAPI, HTTPException, Query,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from daly_protocol import BmsSnapshot, DalyBusManager, snapshot_to_dict, log_snapshot
from daly_write import (
    CommandQueue, DalyWriteManager, DalyWriter, WriteResult,
    PROFILE_SANTUARIO_320AH, PROFILE_SANTUARIO_360AH, Limits,
)

log = logging.getLogger("daly.api")

# ─── Configuration ────────────────────────────────────────────────────────────
API_KEY          = os.getenv("DALY_API_KEY", "")           # vide = pas d'auth
UART_PORT        = os.getenv("DALY_PORT", "/dev/ttyUSB1")
POLL_INTERVAL    = float(os.getenv("DALY_POLL_INTERVAL", "1.0"))
RING_BUFFER_SIZE    = int(os.getenv("DALY_RING_SIZE", "3600"))  # 1h à 1s/point
CELL_COUNT          = int(os.getenv("DALY_CELL_COUNT", "16"))
DALY_AUTO_DISCOVER  = os.getenv("DALY_AUTO_DISCOVER", "0") == "1"

def _parse_bms_ids() -> list[int]:
    """Parse DALY_ADDRESSES=0x01,0x02,0x03 en liste d'entiers triés."""
    raw = os.getenv("DALY_ADDRESSES", "0x01,0x02")
    return sorted({int(x.strip(), 0) for x in raw.split(",") if x.strip()})

BMS_IDS = _parse_bms_ids()   # liste des adresses configurées (défaut : [1, 2])
SENSOR_COUNT     = int(os.getenv("DALY_SENSOR_COUNT", "4"))

# Bridges optionnels — activés via variables d'environnement
MQTT_ENABLED   = os.getenv("MQTT_ENABLED",   "0") == "1"
INFLUX_ENABLED = bool(os.getenv("INFLUX_TOKEN", "").strip())

# Point d'entrée uvicorn (python -m daly_api)
_API_HOST = os.getenv("API_HOST", "0.0.0.0")
_API_PORT = int(os.getenv("API_PORT", "8000"))

# ─── État global partagé ─────────────────────────────────────────────────────
class AppState:
    manager: Optional[DalyWriteManager] = None
    snapshots: dict[int, dict]          = {}
    ring: dict[int, deque]              = {}      # initialisé dynamiquement dans lifespan
    ws_clients: set[WebSocket]          = set()
    poll_task: Optional[asyncio.Task]   = None

state = AppState()


# ─── Lifecycle ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    log.info("Démarrage DalyBMS API — connexion UART...")

    # ── Découverte automatique des BMS (optionnel) ─────────────────────────────
    active_ids = BMS_IDS
    if DALY_AUTO_DISCOVER:
        log.info("Auto-découverte activée — sonde du bus RS485…")
        from daly_protocol import DalyBusManager as _BM
        discovered = await _BM.discover(UART_PORT)
        if discovered:
            active_ids = discovered
            log.info(f"Auto-découverte : {len(active_ids)} BMS → {[hex(x) for x in active_ids]}")
        else:
            log.warning("Auto-découverte : aucun BMS trouvé, utilisation de DALY_ADDRESSES")

    # ── Initialisation ring buffers (un par BMS actif) ─────────────────────────
    for bid in active_ids:
        state.ring[bid] = deque(maxlen=RING_BUFFER_SIZE)

    state.manager = DalyWriteManager(
        UART_PORT, active_ids, cell_count=CELL_COUNT, sensor_count=SENSOR_COUNT
    )
    await state.manager.__aenter__()

    # ── Bridges ────────────────────────────────────────────────────────────────
    _bridges: list = []   # bridges avec interface on_snapshot / stop

    # AlertBridge — toujours actif, enregistre aussi les routes /api/v1/alerts/*
    try:
        from daly_alerts import AlertBridge, register_alert_routes
        _alert = AlertBridge()
        _alert.start()
        register_alert_routes(app, _alert)
        _bridges.append(_alert)
        log.info("AlertBridge démarré")
    except Exception as exc:
        log.warning(f"AlertBridge non démarré : {exc}")

    # MqttBridge — si MQTT_ENABLED=1
    if MQTT_ENABLED:
        try:
            from daly_mqtt import MqttBridge
            _mqtt = MqttBridge()
            _mqtt.start()
            _bridges.append(_mqtt)
            log.info("MqttBridge démarré")
        except Exception as exc:
            log.warning(f"MqttBridge non démarré : {exc}")

    # InfluxBridge — si INFLUX_TOKEN configuré
    if INFLUX_ENABLED:
        try:
            from daly_influx import InfluxBridge
            _influx = InfluxBridge(cell_count=CELL_COUNT, sensor_count=SENSOR_COUNT)
            await _influx.start()
            _bridges.append(_influx)
            log.info("InfluxBridge démarré")
        except Exception as exc:
            log.warning(f"InfluxBridge non démarré : {exc}")

    # ── Polling ────────────────────────────────────────────────────────────────
    async def _on_snapshot(snaps: dict[int, BmsSnapshot]):
        for bid, snap in snaps.items():
            d = snapshot_to_dict(snap)
            state.snapshots[bid] = d
            # Crée le ring buffer à la volée si un nouveau BMS répond
            if bid not in state.ring:
                state.ring[bid] = deque(maxlen=RING_BUFFER_SIZE)
            state.ring[bid].append(d)
        # Broadcast WebSocket
        if state.ws_clients:
            payload = json.dumps({"type": "snapshot", "data": state.snapshots})
            dead = set()
            for ws in set(state.ws_clients):   # copie pour éviter RuntimeError si modification concurrente
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            state.ws_clients -= dead
        # Bridges
        for bridge in _bridges:
            await bridge.on_snapshot(snaps)

    state.poll_task = asyncio.create_task(
        state.manager.poll_loop(_on_snapshot, POLL_INTERVAL),
        name="daly-poll"
    )
    log.info("Polling BMS démarré")

    yield

    log.info("Arrêt DalyBMS API...")
    if state.poll_task:
        state.poll_task.cancel()
    for bridge in reversed(_bridges):
        await bridge.stop()
    await state.manager.__aexit__(None, None, None)


# ─── Application FastAPI ──────────────────────────────────────────────────────
app = FastAPI(
    title="DalyBMS Interface API",
    description="API REST + WebSocket pour monitoring et configuration des BMS Daly Smart — Santuario",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Authentification optionnelle ─────────────────────────────────────────────
async def check_api_key(x_api_key: Optional[str] = None):
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalide ou manquante (header X-API-Key)"
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _resolve_bms_id(bms_id: int) -> int:
    """Valide l'ID BMS de l'URL (0x01..0xFF) contre la liste des BMS actifs."""
    active = state.manager.bms_ids if state.manager else BMS_IDS
    if bms_id not in active:
        raise HTTPException(
            status_code=404,
            detail=f"BMS {bms_id} non configuré — actifs : {active}",
        )
    return bms_id

def _get_snapshot(bms_id: int) -> dict:
    snap = state.snapshots.get(bms_id)
    if not snap:
        raise HTTPException(status_code=503, detail=f"BMS {bms_id} — données non encore disponibles")
    return snap

def _get_writer(bms_id: int) -> DalyWriter:
    if not state.manager:
        raise HTTPException(status_code=503, detail="Manager non initialisé")
    return state.manager.writer(bms_id)

def _write_result_to_response(result: WriteResult) -> dict:
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "Commande échouée")
    return {
        "success": result.success,
        "bms_id": result.bms_id,
        "cmd": result.cmd,
        "verified": result.verified,
        "timestamp": result.timestamp,
    }


# ─── Modèles Pydantic ─────────────────────────────────────────────────────────
class MosCommand(BaseModel):
    chg: Optional[bool] = Field(None, description="Activer/désactiver MOSFET charge")
    dsg: Optional[bool] = Field(None, description="Activer/désactiver MOSFET décharge")

class SocCommand(BaseModel):
    value: float = Field(..., ge=0.0, le=100.0, description="Valeur SOC cible en %")

class ResetCommand(BaseModel):
    confirm: str = Field(..., description="Doit valoir exactement 'CONFIRM_RESET'")

class ProtectionVoltageCell(BaseModel):
    trigger_v: float = Field(..., description="Tension de déclenchement en V")
    recovery_v: Optional[float] = Field(None, description="Tension de récupération en V (optionnel)")

class ProtectionVoltagePack(BaseModel):
    trigger_v: float = Field(..., description="Tension pack de déclenchement en V")
    recovery_v: Optional[float] = Field(None, description="Tension de récupération en V (optionnel)")

class ProtectionCurrent(BaseModel):
    current_a: float = Field(..., ge=Limits.CURRENT_MIN, le=Limits.CURRENT_MAX,
                             description="Seuil de courant en A")
    delay_ms: int    = Field(1000, ge=0, le=30000, description="Délai avant déclenchement en ms")

class ProtectionScp(BaseModel):
    current_a: float = Field(..., ge=Limits.CURRENT_MIN, le=Limits.CURRENT_MAX)
    delay_us: int    = Field(200, ge=0, le=5000, description="Délai en µs")

class ProtectionTemp(BaseModel):
    temp_c: float        = Field(..., ge=Limits.TEMP_MIN, le=Limits.TEMP_MAX, description="°C")
    recovery_c: Optional[float] = Field(None, description="Temp. de récupération °C (optionnel)")

class BalancingConfig(BaseModel):
    enabled: Optional[bool]        = None
    trigger_voltage_v: Optional[float] = Field(None, ge=Limits.BALANCE_V_MIN, le=Limits.BALANCE_V_MAX)
    trigger_delta_mv: Optional[int]    = Field(None, ge=Limits.BALANCE_DELTA_MIN, le=Limits.BALANCE_DELTA_MAX)
    always_on: Optional[bool]      = None

class PackConfig(BaseModel):
    capacity_ah: Optional[int]  = Field(None, ge=Limits.CAPACITY_MIN, le=Limits.CAPACITY_MAX)
    cell_count: Optional[int]   = Field(None, ge=Limits.CELL_COUNT_MIN, le=Limits.CELL_COUNT_MAX)
    sensor_count: Optional[int] = Field(None, ge=Limits.SENSOR_COUNT_MIN, le=Limits.SENSOR_COUNT_MAX)
    chemistry: Optional[str]    = Field(None, pattern="^(LiFePO4|LiIon|LTO)$")

class FullConfig(BaseModel):
    ovp_cell_v: Optional[float]        = None
    uvp_cell_v: Optional[float]        = None
    ovp_pack_v: Optional[float]        = None
    uvp_pack_v: Optional[float]        = None
    ocp_chg_a: Optional[float]         = None
    ocp_dsg_a: Optional[float]         = None
    scp_a: Optional[float]             = None
    otp_chg_c: Optional[float]         = None
    utp_chg_c: Optional[float]         = None
    otp_dsg_c: Optional[float]         = None
    utp_dsg_c: Optional[float]         = None
    balance_en: Optional[bool]         = None
    balance_v: Optional[float]         = None
    balance_delta_mv: Optional[int]    = None
    balance_always: Optional[bool]     = None
    capacity_ah: Optional[int]         = None
    cell_count: Optional[int]          = None
    sensor_count: Optional[int]        = None
    chemistry: Optional[str]           = None


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES GET — Lecture / Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/system/status", tags=["Système"])
async def system_status():
    """État global du système — connectivité de tous les BMS actifs, état du polling."""
    return {
        "poll_running": state.poll_task is not None and not state.poll_task.done(),
        "poll_interval_s": POLL_INTERVAL,
        "bms": {
            str(bid): {
                "connected": bid in state.snapshots,
                "last_update": state.snapshots.get(bid, {}).get("timestamp"),
                "any_alarm": state.snapshots.get(bid, {}).get("any_alarm", False),
                "soc": state.snapshots.get(bid, {}).get("soc"),
            }
            for bid in BMS_IDS
        },
        "ws_clients": len(state.ws_clients),
        "ring_buffer_size": RING_BUFFER_SIZE,
    }


@app.get("/api/v1/config", tags=["Système"])
async def get_config():
    """
    Configuration active — liste des BMS et leurs noms.
    Utilisé par le dashboard pour construire dynamiquement ses vues.
    """
    active = state.manager.bms_ids if state.manager else BMS_IDS
    names  = {bid: os.getenv(f"BMS{bid}_NAME", f"BMS {bid}") for bid in active}
    return {
        "bms_ids":      active,
        "bms_names":    names,
        "cell_count":   CELL_COUNT,
        "sensor_count": SENSOR_COUNT,
    }


@app.get("/api/v1/discover", tags=["Système"])
async def discover_bms(
    range_start: int = Query(1,   ge=1, le=255, description="Adresse de début (défaut 1)"),
    range_end:   int = Query(8,   ge=1, le=255, description="Adresse de fin incluse (défaut 8)"),
    _=Depends(check_api_key),
):
    """
    Lance une découverte live des BMS sur le bus RS485.
    Sonde chaque adresse de range_start à range_end et retourne celles qui répondent.
    Durée typique : ~200ms × (range_end - range_start + 1).
    """
    if range_start > range_end:
        raise HTTPException(status_code=400, detail="range_start doit être ≤ range_end")
    from daly_protocol import DalyBusManager
    t0    = time.monotonic()
    found = await DalyBusManager.discover(
        UART_PORT, probe_range=range(range_start, range_end + 1)
    )
    return {
        "found":       found,
        "count":       len(found),
        "duration_ms": round((time.monotonic() - t0) * 1000),
        "range":       f"0x{range_start:02X}–0x{range_end:02X}",
    }


@app.get("/api/v1/bms/{bms_id}/status", tags=["Monitoring"])
async def bms_status(bms_id: int, _=Depends(check_api_key)):
    """
    Snapshot complet temps réel d'un BMS.
    Retourne toutes les métriques agrégées : SOC, tension, courant,
    état MOS, cellules min/max, températures, alarmes.
    """
    bid = _resolve_bms_id(bms_id)
    return _get_snapshot(bid)


@app.get("/api/v1/bms/{bms_id}/cells", tags=["Monitoring"])
async def bms_cells(bms_id: int, _=Depends(check_api_key)):
    """
    Tensions individuelles de toutes les cellules.
    Inclut : valeur mV par cellule, min, max, moyenne, delta, état balancing.
    """
    bid = _resolve_bms_id(bms_id)
    snap = _get_snapshot(bid)
    cells = {f"cell_{i+1:02d}": snap.get(f"cell_{i+1:02d}") for i in range(CELL_COUNT)}
    return {
        "bms_id": bid,
        "timestamp": snap.get("timestamp"),
        "cells": cells,
        "cell_min_v": snap.get("cell_min_v"),
        "cell_min_num": snap.get("cell_min_num"),
        "cell_max_v": snap.get("cell_max_v"),
        "cell_max_num": snap.get("cell_max_num"),
        "cell_avg": snap.get("cell_avg"),
        "cell_delta": snap.get("cell_delta"),
        "balancing_mask": snap.get("balancing_mask", []),
    }


@app.get("/api/v1/bms/{bms_id}/temperatures", tags=["Monitoring"])
async def bms_temperatures(bms_id: int, _=Depends(check_api_key)):
    """Valeurs de toutes les sondes NTC en °C."""
    bid = _resolve_bms_id(bms_id)
    snap = _get_snapshot(bid)
    sensors = {f"sensor_{i+1:02d}": snap.get(f"temp_{i+1:02d}") for i in range(SENSOR_COUNT)}
    return {
        "bms_id": bid,
        "timestamp": snap.get("timestamp"),
        "sensors": sensors,
        "temp_min": snap.get("temp_min"),
        "temp_max": snap.get("temp_max"),
    }


@app.get("/api/v1/bms/{bms_id}/alarms", tags=["Monitoring"])
async def bms_alarms(bms_id: int, _=Depends(check_api_key)):
    """Flags de protection actifs. any_alarm=True si au moins un flag levé."""
    bid = _resolve_bms_id(bms_id)
    snap = _get_snapshot(bid)
    alarm_keys = [
        "alarm_cell_ovp", "alarm_cell_uvp", "alarm_pack_ovp", "alarm_pack_uvp",
        "alarm_chg_otp",  "alarm_chg_ocp",  "alarm_dsg_ocp",  "alarm_scp",
        "alarm_cell_delta", "any_alarm",
    ]
    return {
        "bms_id": bid,
        "timestamp": snap.get("timestamp"),
        "any_alarm": snap.get("any_alarm", False),
        "flags": {k.removeprefix("alarm_"): snap.get(k, False) for k in alarm_keys},
    }


@app.get("/api/v1/bms/{bms_id}/mos", tags=["Monitoring"])
async def bms_mos(bms_id: int, _=Depends(check_api_key)):
    """État des MOSFET CHG/DSG et mode de fonctionnement."""
    bid = _resolve_bms_id(bms_id)
    snap = _get_snapshot(bid)
    return {
        "bms_id": bid,
        "timestamp": snap.get("timestamp"),
        "charge_mos": snap.get("charge_mos"),
        "discharge_mos": snap.get("discharge_mos"),
        "bms_cycles": snap.get("bms_cycles"),
        "remaining_capacity_ah": snap.get("remaining_capacity"),
    }


@app.get("/api/v1/bms/{bms_id}/history", tags=["Monitoring"])
async def bms_history(
    bms_id: int,
    duration: str = Query("1h", pattern="^[0-9]+(s|m|h)$",
                          description="Fenêtre temporelle : ex. 60s, 30m, 1h"),
    fields: Optional[str] = Query(None,
                                  description="Champs à inclure, séparés par virgule (défaut: tous)"),
    _=Depends(check_api_key),
):
    """
    Historique en mémoire depuis le ring buffer.
    Résolution native = intervalle de polling (1s par défaut).
    Fenêtre max = RING_BUFFER_SIZE × poll_interval.
    """
    bid = _resolve_bms_id(bms_id)
    ring = state.ring.get(bid, deque())

    # Calcul de la fenêtre temporelle
    unit   = duration[-1]
    amount = int(duration[:-1])
    multipliers = {"s": 1, "m": 60, "h": 3600}
    window_s = amount * multipliers[unit]

    cutoff = time.time() - window_s
    filtered = [p for p in ring if p.get("timestamp", 0) >= cutoff]

    # Filtrage des champs
    if fields:
        wanted = set(fields.split(",")) | {"timestamp", "bms_id"}
        filtered = [{k: v for k, v in p.items() if k in wanted} for p in filtered]

    return {
        "bms_id": bid,
        "duration": duration,
        "points": len(filtered),
        "data": filtered,
    }


@app.get("/api/v1/bms/{bms_id}/history/summary", tags=["Monitoring"])
async def bms_history_summary(bms_id: int, _=Depends(check_api_key)):
    """
    Résumé statistique sur le ring buffer complet :
    min/max/average pour SOC, tension, courant, delta cellule.
    """
    bid = _resolve_bms_id(bms_id)
    ring = list(state.ring.get(bid, deque()))
    if not ring:
        raise HTTPException(status_code=503, detail="Ring buffer vide")

    def _stats(key: str) -> dict:
        vals = [p[key] for p in ring if p.get(key) is not None]
        if not vals:
            return {"min": None, "max": None, "avg": None, "count": 0}
        return {
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "avg": round(sum(vals) / len(vals), 3),
            "count": len(vals),
        }

    return {
        "bms_id": bid,
        "points_in_buffer": len(ring),
        "oldest_ts": ring[0].get("timestamp") if ring else None,
        "newest_ts": ring[-1].get("timestamp") if ring else None,
        "soc":          _stats("soc"),
        "pack_voltage": _stats("pack_voltage"),
        "pack_current": _stats("pack_current"),
        "cell_delta":   _stats("cell_delta"),
        "temp_max":     _stats("temp_max"),
    }


@app.get("/api/v1/bms/compare", tags=["Monitoring"])
async def bms_compare(_=Depends(check_api_key)):
    """Vue comparative de tous les BMS actifs — métriques clés côte à côte."""
    result = {}
    for bid in BMS_IDS:
        snap = state.snapshots.get(bid)
        if snap:
            result[str(bid)] = {
                "soc": snap.get("soc"),
                "pack_voltage": snap.get("pack_voltage"),
                "pack_current": snap.get("pack_current"),
                "power": snap.get("power"),
                "cell_delta": snap.get("cell_delta"),
                "cell_min_v": snap.get("cell_min_v"),
                "cell_max_v": snap.get("cell_max_v"),
                "temp_max": snap.get("temp_max"),
                "charge_mos": snap.get("charge_mos"),
                "discharge_mos": snap.get("discharge_mos"),
                "any_alarm": snap.get("any_alarm"),
                "bms_cycles": snap.get("bms_cycles"),
            }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES POST — Commandes et Configuration
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/bms/{bms_id}/mos", tags=["Contrôle"])
async def bms_set_mos(bms_id: int, cmd: MosCommand, _=Depends(check_api_key)):
    """
    Contrôle des MOSFET CHG et/ou DSG.
    Chaque champ est optionnel — envoyer uniquement ce que l'on souhaite modifier.
    Vérification post-écriture par relecture de l'état MOS.
    """
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    results = []
    if cmd.chg is not None:
        r = await writer.set_charge_mos(cmd.chg)
        results.append(_write_result_to_response(r))
    if cmd.dsg is not None:
        r = await writer.set_discharge_mos(cmd.dsg)
        results.append(_write_result_to_response(r))
    if not results:
        raise HTTPException(status_code=422, detail="Au moins un champ requis : chg ou dsg")
    return {"results": results}


@app.post("/api/v1/bms/{bms_id}/soc", tags=["Contrôle"])
async def bms_set_soc(bms_id: int, cmd: SocCommand, _=Depends(check_api_key)):
    """
    Calibration SOC.
    value : float entre 0.0 et 100.0 (%).
    Vérification post-écriture par relecture SOC data.
    """
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    result = await writer.set_soc(cmd.value)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/soc/full", tags=["Contrôle"])
async def bms_set_soc_full(bms_id: int, _=Depends(check_api_key)):
    """Déclare le pack comme plein (SOC = 100%)."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).force_full()
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/soc/empty", tags=["Contrôle"])
async def bms_set_soc_empty(bms_id: int, _=Depends(check_api_key)):
    """Déclare le pack comme vide (SOC = 0%)."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).force_empty()
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/reset", tags=["Contrôle"])
async def bms_reset(bms_id: int, cmd: ResetCommand, _=Depends(check_api_key)):
    """
    Reset BMS.
    Requiert confirm='CONFIRM_RESET' dans le body pour éviter les resets accidentels.
    Le BMS redémarre en ~3s — pas de vérification possible.
    """
    if cmd.confirm != "CONFIRM_RESET":
        raise HTTPException(status_code=422,
                            detail="Valeur de confirmation invalide — attendu : 'CONFIRM_RESET'")
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).reset()
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/ovp/cell", tags=["Configuration"])
async def bms_set_ovp_cell(bms_id: int, cfg: ProtectionVoltageCell, _=Depends(check_api_key)):
    """Over Voltage Protection cellule — seuil et récupération en V."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_ovp_cell(cfg.trigger_v, cfg.recovery_v)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/uvp/cell", tags=["Configuration"])
async def bms_set_uvp_cell(bms_id: int, cfg: ProtectionVoltageCell, _=Depends(check_api_key)):
    """Under Voltage Protection cellule — seuil et récupération en V."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_uvp_cell(cfg.trigger_v, cfg.recovery_v)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/ovp/pack", tags=["Configuration"])
async def bms_set_ovp_pack(bms_id: int, cfg: ProtectionVoltagePack, _=Depends(check_api_key)):
    """Over Voltage Protection pack total — seuil et récupération en V."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_ovp_pack(cfg.trigger_v, cfg.recovery_v)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/uvp/pack", tags=["Configuration"])
async def bms_set_uvp_pack(bms_id: int, cfg: ProtectionVoltagePack, _=Depends(check_api_key)):
    """Under Voltage Protection pack total — seuil et récupération en V."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_uvp_pack(cfg.trigger_v, cfg.recovery_v)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/ocp/charge", tags=["Configuration"])
async def bms_set_ocp_charge(bms_id: int, cfg: ProtectionCurrent, _=Depends(check_api_key)):
    """Over Current Protection charge — seuil en A et délai en ms."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_ocp_charge(cfg.current_a, cfg.delay_ms)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/ocp/discharge", tags=["Configuration"])
async def bms_set_ocp_discharge(bms_id: int, cfg: ProtectionCurrent, _=Depends(check_api_key)):
    """Over Current Protection décharge — seuil en A et délai en ms."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_ocp_discharge(cfg.current_a, cfg.delay_ms)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/scp", tags=["Configuration"])
async def bms_set_scp(bms_id: int, cfg: ProtectionScp, _=Depends(check_api_key)):
    """Short Circuit Protection — seuil en A et délai en µs."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_scp(cfg.current_a, cfg.delay_us)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/otp/charge", tags=["Configuration"])
async def bms_set_otp_charge(bms_id: int, cfg: ProtectionTemp, _=Depends(check_api_key)):
    """Over Temperature Protection charge — seuil et récupération en °C."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_otp_charge(cfg.temp_c, cfg.recovery_c)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/utp/charge", tags=["Configuration"])
async def bms_set_utp_charge(bms_id: int, cfg: ProtectionTemp, _=Depends(check_api_key)):
    """Under Temperature Protection charge (blocage charge par froid)."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_utp_charge(cfg.temp_c, cfg.recovery_c)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/otp/discharge", tags=["Configuration"])
async def bms_set_otp_discharge(bms_id: int, cfg: ProtectionTemp, _=Depends(check_api_key)):
    """Over Temperature Protection décharge."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_otp_discharge(cfg.temp_c, cfg.recovery_c)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/utp/discharge", tags=["Configuration"])
async def bms_set_utp_discharge(bms_id: int, cfg: ProtectionTemp, _=Depends(check_api_key)):
    """Under Temperature Protection décharge."""
    bid = _resolve_bms_id(bms_id)
    result = await _get_writer(bid).set_utp_discharge(cfg.temp_c, cfg.recovery_c)
    return _write_result_to_response(result)


@app.post("/api/v1/bms/{bms_id}/config/balancing", tags=["Configuration"])
async def bms_set_balancing(bms_id: int, cfg: BalancingConfig, _=Depends(check_api_key)):
    """
    Configuration complète du balancing.
    Tous les champs sont optionnels — seuls les champs présents sont écrits.
    """
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    results = []
    if cfg.enabled is not None:
        results.append(_write_result_to_response(await writer.set_balance_enabled(cfg.enabled)))
    if cfg.trigger_voltage_v is not None:
        results.append(_write_result_to_response(await writer.set_balance_trigger_voltage(cfg.trigger_voltage_v)))
    if cfg.trigger_delta_mv is not None:
        results.append(_write_result_to_response(await writer.set_balance_trigger_delta(cfg.trigger_delta_mv)))
    if cfg.always_on is not None:
        results.append(_write_result_to_response(await writer.set_balance_mode(cfg.always_on)))
    if not results:
        raise HTTPException(status_code=422, detail="Au moins un champ de balancing requis")
    return {"results": results}


@app.post("/api/v1/bms/{bms_id}/config/pack", tags=["Configuration"])
async def bms_set_pack_config(bms_id: int, cfg: PackConfig, _=Depends(check_api_key)):
    """
    Paramètres du pack : capacité, nombre de cellules, sondes, chimie.
    Tous les champs sont optionnels.
    """
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    results = []
    if cfg.capacity_ah is not None:
        results.append(_write_result_to_response(await writer.set_capacity(cfg.capacity_ah)))
    if cfg.cell_count is not None:
        results.append(_write_result_to_response(await writer.set_cell_count(cfg.cell_count)))
    if cfg.sensor_count is not None:
        results.append(_write_result_to_response(await writer.set_sensor_count(cfg.sensor_count)))
    if cfg.chemistry is not None:
        results.append(_write_result_to_response(await writer.set_chemistry(cfg.chemistry)))
    if not results:
        raise HTTPException(status_code=422, detail="Au moins un champ de configuration pack requis")
    return {"results": results}


@app.post("/api/v1/bms/{bms_id}/config/full", tags=["Configuration"])
async def bms_apply_full_config(bms_id: int, cfg: FullConfig, _=Depends(check_api_key)):
    """
    Application d'un profil de configuration complet en une seule requête.
    Seuls les champs fournis sont écrits. Les champs null sont ignorés.
    Traitement séquentiel via CommandQueue — s'arrête à la première erreur.
    """
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    profile = {k: v for k, v in cfg.model_dump().items() if v is not None}
    if not profile:
        raise HTTPException(status_code=422, detail="Aucun paramètre fourni")
    results = await writer.apply_profile(profile)
    return {
        "total": len(results),
        "success": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "results": [_write_result_to_response(r) for r in results],
    }


@app.post("/api/v1/bms/{bms_id}/config/preset/{preset_name}", tags=["Configuration"])
async def bms_apply_preset(bms_id: int, preset_name: str, _=Depends(check_api_key)):
    """
    Applique un profil préconfiguré Santuario.
    Presets disponibles : 'santuario_320ah', 'santuario_360ah'
    """
    presets = {
        "santuario_320ah": PROFILE_SANTUARIO_320AH,
        "santuario_360ah": PROFILE_SANTUARIO_360AH,
    }
    if preset_name not in presets:
        raise HTTPException(status_code=404,
                            detail=f"Preset inconnu. Disponibles : {list(presets.keys())}")
    bid = _resolve_bms_id(bms_id)
    writer = _get_writer(bid)
    results = await writer.apply_profile(presets[preset_name])
    return {
        "preset": preset_name,
        "total": len(results),
        "success": sum(1 for r in results if r.success),
        "results": [_write_result_to_response(r) for r in results],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET — Flux temps réel
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/bms/stream")
async def ws_stream_all(websocket: WebSocket):
    """
    WebSocket — flux temps réel des deux BMS.
    Pousse un message JSON à chaque cycle de polling.
    Format : {"type": "snapshot", "data": {1: {...}, 2: {...}}}
    """
    await websocket.accept()
    state.ws_clients.add(websocket)
    log.info(f"WebSocket connecté — clients actifs : {len(state.ws_clients)}")
    try:
        # Envoi immédiat du dernier snapshot connu
        if state.snapshots:
            await websocket.send_text(
                json.dumps({"type": "snapshot", "data": state.snapshots})
            )
        # Maintien de la connexion — le push est géré par le poll_loop
        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        log.info("WebSocket déconnecté normalement")
    except Exception as exc:
        log.warning(f"WebSocket erreur : {exc}")
    finally:
        state.ws_clients.discard(websocket)


@app.websocket("/ws/bms/{bms_id}/stream")
async def ws_stream_single(websocket: WebSocket, bms_id: int):
    """
    WebSocket — flux temps réel d'un seul BMS.
    Format : {"type": "snapshot", "bms_id": 1, "data": {...}}
    """
    bid = _resolve_bms_id(bms_id)
    await websocket.accept()
    log.info(f"WebSocket BMS{bid} connecté")
    try:
        if bid in state.snapshots:
            await websocket.send_text(
                json.dumps({"type": "snapshot", "bms_id": bid, "data": state.snapshots[bid]})
            )
        prev_ts = None
        while True:
            snap = state.snapshots.get(bid)
            if snap and snap.get("timestamp") != prev_ts:
                prev_ts = snap.get("timestamp")
                await websocket.send_text(
                    json.dumps({"type": "snapshot", "bms_id": bid, "data": snap})
                )
            await asyncio.sleep(POLL_INTERVAL)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning(f"WebSocket BMS{bid} erreur : {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# SSE — Server-Sent Events (alternative légère au WebSocket)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/bms/{bms_id}/sse", tags=["Monitoring"])
async def bms_sse(bms_id: int):
    """
    Server-Sent Events — alternative WebSocket pour clients légers.
    Compatible navigateur sans bibliothèque JS (EventSource API native).
    """
    bid = _resolve_bms_id(bms_id)

    async def _event_generator():
        prev_ts = None
        while True:
            snap = state.snapshots.get(bid)
            if snap and snap.get("timestamp") != prev_ts:
                prev_ts = snap.get("timestamp")
                data = json.dumps({"bms_id": bid, "data": snap})
                yield f"event: snapshot\ndata: {data}\n\n"
            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# EXPORT CSV
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/bms/{bms_id}/export/csv", tags=["Export"])
async def bms_export_csv(
    bms_id: int,
    duration: str = Query("1h", pattern="^[0-9]+(s|m|h)$"),
    _=Depends(check_api_key),
):
    """
    Export CSV du ring buffer sur la fenêtre temporelle demandée.
    En-tête automatique avec toutes les colonnes présentes.
    """
    bid = _resolve_bms_id(bms_id)
    ring = state.ring.get(bid, deque())

    unit   = duration[-1]
    amount = int(duration[:-1])
    multipliers = {"s": 1, "m": 60, "h": 3600}
    cutoff = time.time() - amount * multipliers[unit]
    data   = [p for p in ring if p.get("timestamp", 0) >= cutoff]

    if not data:
        raise HTTPException(status_code=404, detail="Aucune donnée dans la fenêtre demandée")

    keys = list(data[0].keys())

    async def _csv_generator():
        yield ",".join(keys) + "\n"
        for row in data:
            yield ",".join(str(row.get(k, "")) for k in keys) + "\n"

    filename = f"bms{bid}_{duration}_{int(time.time())}.csv"
    return StreamingResponse(
        _csv_generator(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─── Point d'entrée ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "daly_api:app",
        host=_API_HOST,
        port=_API_PORT,
        reload=False,
        log_level="info",
    )
"""
daly_write.py — D2 : Couche d'Écriture Commandes et Paramètres
File de commandes séquencée, validation, vérification post-écriture.
Dépend de : daly_protocol.py (D1)
Installation Santuario — Badalucco
"""

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Coroutine, Optional

from daly_protocol import (
    Cmd, DalyBms, DalyBusManager, DalyPort,
    START_BYTE, HOST_ADDR, FRAME_DATA_LEN,
    _checksum, _validate_response,
    SocData, MosStatus, CellVoltages,
)

log = logging.getLogger("daly.write")

# ─── Codes de commandes d'écriture ────────────────────────────────────────────
class WriteCmd(IntEnum):
    # Contrôle MOS (réutilisés depuis D1 pour cohérence)
    SET_DISCHARGE_MOS   = 0xD9
    SET_CHARGE_MOS      = 0xDA

    # Calibration SOC
    SET_SOC             = 0x21

    # Reset
    RESET               = 0x00

    # Paramètres de protection cellule
    SET_OVP_CELL        = 0x24  # Over Voltage Protection cellule
    SET_UVP_CELL        = 0x25  # Under Voltage Protection cellule

    # Paramètres de protection pack
    SET_OVP_PACK        = 0x26  # Over Voltage Protection pack
    SET_UVP_PACK        = 0x27  # Under Voltage Protection pack

    # Courants de protection
    SET_OCP_CHG         = 0x28  # Over Current Protection charge
    SET_OCP_DSG         = 0x29  # Over Current Protection décharge
    SET_SCP             = 0x2A  # Short Circuit Protection

    # Protection thermique charge
    SET_OTP_CHG         = 0x2B  # Over Temperature charge
    SET_OTP_CHG_R       = 0x2C  # Recovery Over Temperature charge
    SET_UTP_CHG         = 0x2D  # Under Temperature charge
    SET_UTP_CHG_R       = 0x2E  # Recovery Under Temperature charge

    # Protection thermique décharge
    SET_OTP_DSG         = 0x2F  # Over Temperature décharge
    SET_OTP_DSG_R       = 0x30  # Recovery Over Temperature décharge
    SET_UTP_DSG         = 0x31  # Under Temperature décharge
    SET_UTP_DSG_R       = 0x32  # Recovery Under Temperature décharge

    # Balancing
    SET_BALANCE_EN      = 0x33  # Activation balancing
    SET_BALANCE_V       = 0x34  # Tension déclenchement balancing
    SET_BALANCE_DELTA   = 0x35  # Delta déclenchement balancing
    SET_BALANCE_MODE    = 0x36  # Mode : charge seulement / toujours

    # Paramètres pack
    SET_CAPACITY        = 0x37  # Capacité nominale Ah
    SET_CELL_COUNT      = 0x38  # Nombre de cellules en série
    SET_SENSOR_COUNT    = 0x39  # Nombre de sondes NTC
    SET_CHEMISTRY       = 0x3A  # Chimie (LiFePO4=0, Li-Ion=1, LTO=2)


# ─── Constantes de validation ─────────────────────────────────────────────────
class Limits:
    # Tensions cellule LiFePO4 (V)
    CELL_V_MIN          = 2.50
    CELL_V_MAX          = 3.80
    CELL_OVP_MIN        = 3.40
    CELL_OVP_MAX        = 3.75
    CELL_UVP_MIN        = 2.50
    CELL_UVP_MAX        = 3.20

    # Tensions pack 16S (V)
    PACK_V_MIN          = 40.0
    PACK_V_MAX          = 61.0

    # Courants (A)
    CURRENT_MIN         = 1.0
    CURRENT_MAX         = 500.0

    # Températures (°C)
    TEMP_MIN            = -40
    TEMP_MAX            = 100

    # Balancing
    BALANCE_V_MIN       = 3.30   # V — tension mini de déclenchement
    BALANCE_V_MAX       = 3.65
    BALANCE_DELTA_MIN   = 5      # mV
    BALANCE_DELTA_MAX   = 500    # mV

    # Pack
    CAPACITY_MIN        = 10     # Ah
    CAPACITY_MAX        = 2000   # Ah
    CELL_COUNT_MIN      = 3
    CELL_COUNT_MAX      = 24
    SENSOR_COUNT_MIN    = 1
    SENSOR_COUNT_MAX    = 8


# ─── Résultat d'une commande ──────────────────────────────────────────────────
@dataclass
class WriteResult:
    success: bool
    bms_id: int
    cmd: str
    value: Any
    verified: bool = False
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def __str__(self):
        status = "✓ OK" if self.success else "✗ ECHEC"
        verif  = " [vérifié]" if self.verified else ""
        err    = f" — {self.error}" if self.error else ""
        return f"[BMS{self.bms_id}] {status} {self.cmd} = {self.value}{verif}{err}"


# ─── Entrée de file de commandes ──────────────────────────────────────────────
@dataclass
class CommandEntry:
    coro: Coroutine
    future: asyncio.Future
    description: str


# ─── File de commandes séquencée ─────────────────────────────────────────────
class CommandQueue:
    """
    File FIFO pour séquencer les commandes d'écriture.
    Garantit qu'aucune commande d'écriture ne s'exécute en parallèle
    sur le bus UART, même depuis plusieurs coroutines concurrentes.
    Doit être démarré via start() et arrêté via stop().
    """
    def __init__(self):
        self._queue: asyncio.Queue[CommandEntry] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._worker(), name="daly-write-queue")
        log.info("CommandQueue démarrée")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("CommandQueue arrêtée")

    async def _worker(self):
        while self._running:
            try:
                entry = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                result = await entry.coro
                entry.future.set_result(result)
            except Exception as exc:
                log.error(f"CommandQueue — erreur exécution '{entry.description}' : {exc}")
                entry.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def submit(self, coro: Coroutine, description: str = "") -> WriteResult:
        """Soumet une coroutine à la file et attend son résultat."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        entry = CommandEntry(coro=coro, future=future, description=description)
        await self._queue.put(entry)
        log.debug(f"CommandQueue ← '{description}' (qsize={self._queue.qsize()})")
        return await future


# ─── Couche d'écriture principale ────────────────────────────────────────────
class DalyWriter:
    """
    Extension de DalyBms avec toutes les commandes d'écriture paramétrées.
    Utilise une CommandQueue partagée pour sérialiser les écritures.
    Chaque écriture est suivie d'une vérification par relecture (si possible).

    Usage typique :
        queue = CommandQueue(); queue.start()
        writer = DalyWriter(bus.bms(0x01), queue)
        result = await writer.set_ovp_cell(3.65)
    """
    def __init__(self, bms: DalyBms, queue: CommandQueue):
        self.bms   = bms
        self.queue = queue
        self.bms_id = bms.bms_id

    # ── Utilitaires internes ──────────────────────────────────────────────────
    def _v_to_raw(self, v: float, unit: str = "mV10") -> int:
        """
        Conversion tension → entier Daly.
        mV10 : résolution 1mV  → valeur × 1   (ex. 3650mV → 3650)
        V10  : résolution 0.1V → valeur × 10  (ex. 58.4V  → 584)
        V100 : résolution 0.01V                (ex. 3.650V → 3650 = identique mV10)
        """
        if unit == "mV10":
            return int(round(v))            # v en mV
        if unit == "V10":
            return int(round(v * 10))       # v en V
        if unit == "V100":
            return int(round(v * 100))      # v en V → centi-Volts
        raise ValueError(f"Unité inconnue : {unit}")

    def _pack_u16(self, value: int, offset: int = 0) -> bytes:
        """Construit un payload de 8 octets avec uint16 à l'offset donné."""
        payload = bytearray(8)
        struct.pack_into(">H", payload, offset, value & 0xFFFF)
        return bytes(payload)

    def _pack_u32(self, value: int, offset: int = 0) -> bytes:
        payload = bytearray(8)
        struct.pack_into(">I", payload, offset, value & 0xFFFFFFFF)
        return bytes(payload)

    def _pack_u8(self, value: int, offset: int = 0) -> bytes:
        payload = bytearray(8)
        payload[offset] = value & 0xFF
        return bytes(payload)

    async def _write(self, cmd: WriteCmd, payload: bytes,
                     description: str, verify_fn: Optional[Callable] = None) -> WriteResult:
        """
        Construit et envoie une trame d'écriture via la CommandQueue.
        verify_fn : coroutine sans argument qui retourne la valeur relue (ou None).
        """
        async def _execute() -> WriteResult:
            frame = bytes([START_BYTE, HOST_ADDR, int(cmd), FRAME_DATA_LEN]) + payload[:8]
            frame += bytes([_checksum(frame)])

            success = False
            error   = None
            for attempt in range(1, self.bms.retries + 1):
                async with self.bms.port._lock:
                    await self.bms.port.flush()
                    await self.bms.port.send_frame(frame)
                    ack = await self.bms.port.receive_frame(13)   # 4 header + 8 data + 1 CRC
                    if ack and _validate_response(ack, cmd, self.bms_id):
                        success = True
                        break
                log.warning(f"[BMS{self.bms_id}] {description} — tentative {attempt} sans ACK")
                await asyncio.sleep(0.2 * attempt)
            else:
                error = f"Aucun ACK après {self.bms.retries} tentatives"

            verified = False
            if success and verify_fn:
                await asyncio.sleep(0.15)   # délai avant relecture
                try:
                    verified = await verify_fn()
                except Exception as exc:
                    log.warning(f"[BMS{self.bms_id}] Vérification échouée : {exc}")

            result = WriteResult(
                success=success,
                bms_id=self.bms_id,
                cmd=description,
                value=payload.hex(" ").upper(),
                verified=verified,
                error=error,
            )
            log.info(str(result))
            return result

        return await self.queue.submit(_execute(), description)

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 1 — Contrôle MOS
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_charge_mos(self, enable: bool) -> WriteResult:
        """Active ou désactive le MOSFET de charge. Vérifié par relecture MOS status."""
        payload = self._pack_u8(0x01 if enable else 0x00)

        async def verify():
            mos = await self.bms.get_mos_status()
            if mos and mos.charge_mos == enable:
                return True
            log.warning(f"[BMS{self.bms_id}] CHG MOS vérification : attendu={enable}, lu={mos.charge_mos if mos else '?'}")
            return False

        return await self._write(WriteCmd.SET_CHARGE_MOS, payload,
                                 f"SET_CHARGE_MOS={'ON' if enable else 'OFF'}", verify)

    async def set_discharge_mos(self, enable: bool) -> WriteResult:
        """Active ou désactive le MOSFET de décharge. Vérifié par relecture MOS status."""
        payload = self._pack_u8(0x01 if enable else 0x00)

        async def verify():
            mos = await self.bms.get_mos_status()
            if mos and mos.discharge_mos == enable:
                return True
            log.warning(f"[BMS{self.bms_id}] DSG MOS vérification : attendu={enable}, lu={mos.discharge_mos if mos else '?'}")
            return False

        return await self._write(WriteCmd.SET_DISCHARGE_MOS, payload,
                                 f"SET_DISCHARGE_MOS={'ON' if enable else 'OFF'}", verify)

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 2 — Calibration SOC
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_soc(self, soc_percent: float) -> WriteResult:
        """
        Calibre le SOC à une valeur arbitraire [0.0 – 100.0 %].
        Encodage : uint16 big-endian, résolution 0.1% (valeur × 10) à l'offset 4.
        Vérifié par relecture SOC data.
        """
        if not 0.0 <= soc_percent <= 100.0:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_SOC", value=soc_percent,
                               error=f"SOC hors plage [0, 100] : {soc_percent}")
        raw = int(round(soc_percent * 10))
        payload = bytes(4) + struct.pack(">H", raw) + bytes(2)

        async def verify():
            soc_data = await self.bms.get_soc()
            if soc_data and abs(soc_data.soc - soc_percent) < 0.5:
                return True
            log.warning(f"[BMS{self.bms_id}] SOC vérification : attendu={soc_percent}%, lu={soc_data.soc if soc_data else '?'}%")
            return False

        return await self._write(WriteCmd.SET_SOC, payload,
                                 f"SET_SOC={soc_percent}%", verify)

    async def force_full(self) -> WriteResult:
        """Déclare le pack comme plein (SOC = 100%)."""
        return await self.set_soc(100.0)

    async def force_empty(self) -> WriteResult:
        """Déclare le pack comme vide (SOC = 0%)."""
        return await self.set_soc(0.0)

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 3 — Reset BMS
    # ═══════════════════════════════════════════════════════════════════════════

    async def reset(self) -> WriteResult:
        """
        Envoie la commande de reset BMS.
        Pas de vérification possible (reconnexion requise après reset).
        Le worker attend 3s pour laisser le BMS redémarrer.
        """
        async def _do_reset() -> WriteResult:
            frame = bytes([START_BYTE, HOST_ADDR, int(WriteCmd.RESET), FRAME_DATA_LEN]) + bytes(8)
            frame += bytes([_checksum(frame)])
            async with self.bms.port._lock:
                await self.bms.port.send_frame(frame)
            log.warning(f"[BMS{self.bms_id}] RESET envoyé — pause 3s pour redémarrage BMS")
            await asyncio.sleep(3.0)
            return WriteResult(success=True, bms_id=self.bms_id,
                               cmd="RESET", value="—", verified=False)

        return await self.queue.submit(_do_reset(), "RESET")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 4 — Protections tension cellule
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_ovp_cell(self, voltage_v: float,
                           recovery_v: Optional[float] = None) -> WriteResult:
        """
        Over Voltage Protection cellule.
        voltage_v  : seuil de déclenchement (V) — ex. 3.65
        recovery_v : seuil de récupération (V) — défaut : voltage_v - 0.05
        Encodage   : mV en uint16, offset 0 (trigger), offset 2 (recovery).
        """
        if not Limits.CELL_OVP_MIN <= voltage_v <= Limits.CELL_OVP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OVP_CELL", value=voltage_v,
                               error=f"OVP hors plage [{Limits.CELL_OVP_MIN}, {Limits.CELL_OVP_MAX}]V")
        recovery_v = recovery_v or round(voltage_v - 0.05, 3)
        raw_trigger  = self._v_to_raw(voltage_v * 1000)    # V → mV
        raw_recovery = self._v_to_raw(recovery_v * 1000)
        payload = struct.pack(">HH", raw_trigger, raw_recovery) + bytes(4)
        return await self._write(WriteCmd.SET_OVP_CELL, payload,
                                 f"SET_OVP_CELL={voltage_v}V (rec={recovery_v}V)")

    async def set_uvp_cell(self, voltage_v: float,
                           recovery_v: Optional[float] = None) -> WriteResult:
        """
        Under Voltage Protection cellule.
        voltage_v  : seuil de déclenchement (V) — ex. 2.80
        recovery_v : seuil de récupération (V) — défaut : voltage_v + 0.05
        """
        if not Limits.CELL_UVP_MIN <= voltage_v <= Limits.CELL_UVP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_UVP_CELL", value=voltage_v,
                               error=f"UVP hors plage [{Limits.CELL_UVP_MIN}, {Limits.CELL_UVP_MAX}]V")
        recovery_v = recovery_v or round(voltage_v + 0.05, 3)
        raw_trigger  = self._v_to_raw(voltage_v * 1000)
        raw_recovery = self._v_to_raw(recovery_v * 1000)
        payload = struct.pack(">HH", raw_trigger, raw_recovery) + bytes(4)
        return await self._write(WriteCmd.SET_UVP_CELL, payload,
                                 f"SET_UVP_CELL={voltage_v}V (rec={recovery_v}V)")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 5 — Protections tension pack
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_ovp_pack(self, voltage_v: float,
                           recovery_v: Optional[float] = None) -> WriteResult:
        """
        Over Voltage Protection pack total (ex. 58.4V pour 16S LiFePO4).
        Encodage : 0.1V résolution (uint16 = voltage_v × 10).
        """
        if not Limits.PACK_V_MIN <= voltage_v <= Limits.PACK_V_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OVP_PACK", value=voltage_v,
                               error=f"OVP pack hors plage [{Limits.PACK_V_MIN}, {Limits.PACK_V_MAX}]V")
        recovery_v = recovery_v or round(voltage_v - 0.5, 1)
        raw_t = self._v_to_raw(voltage_v, "V10")
        raw_r = self._v_to_raw(recovery_v, "V10")
        payload = struct.pack(">HH", raw_t, raw_r) + bytes(4)
        return await self._write(WriteCmd.SET_OVP_PACK, payload,
                                 f"SET_OVP_PACK={voltage_v}V (rec={recovery_v}V)")

    async def set_uvp_pack(self, voltage_v: float,
                           recovery_v: Optional[float] = None) -> WriteResult:
        """
        Under Voltage Protection pack total (ex. 44.8V pour 16S LiFePO4).
        """
        if not Limits.PACK_V_MIN <= voltage_v <= Limits.PACK_V_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_UVP_PACK", value=voltage_v,
                               error=f"UVP pack hors plage [{Limits.PACK_V_MIN}, {Limits.PACK_V_MAX}]V")
        recovery_v = recovery_v or round(voltage_v + 0.5, 1)
        raw_t = self._v_to_raw(voltage_v, "V10")
        raw_r = self._v_to_raw(recovery_v, "V10")
        payload = struct.pack(">HH", raw_t, raw_r) + bytes(4)
        return await self._write(WriteCmd.SET_UVP_PACK, payload,
                                 f"SET_UVP_PACK={voltage_v}V (rec={recovery_v}V)")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 6 — Protections courant
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_ocp_charge(self, current_a: float, delay_ms: int = 1000) -> WriteResult:
        """
        Over Current Protection en charge.
        current_a : seuil en A (entier, résolution 1A)
        delay_ms  : délai avant déclenchement en ms (uint16)
        """
        if not Limits.CURRENT_MIN <= current_a <= Limits.CURRENT_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OCP_CHG", value=current_a,
                               error=f"Courant hors plage [{Limits.CURRENT_MIN}, {Limits.CURRENT_MAX}]A")
        payload = struct.pack(">HH", int(current_a), delay_ms) + bytes(4)
        return await self._write(WriteCmd.SET_OCP_CHG, payload,
                                 f"SET_OCP_CHG={current_a}A delay={delay_ms}ms")

    async def set_ocp_discharge(self, current_a: float, delay_ms: int = 1000) -> WriteResult:
        """Over Current Protection en décharge."""
        if not Limits.CURRENT_MIN <= current_a <= Limits.CURRENT_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OCP_DSG", value=current_a,
                               error=f"Courant hors plage [{Limits.CURRENT_MIN}, {Limits.CURRENT_MAX}]A")
        payload = struct.pack(">HH", int(current_a), delay_ms) + bytes(4)
        return await self._write(WriteCmd.SET_OCP_DSG, payload,
                                 f"SET_OCP_DSG={current_a}A delay={delay_ms}ms")

    async def set_scp(self, current_a: float, delay_us: int = 200) -> WriteResult:
        """
        Short Circuit Protection.
        current_a : seuil de déclenchement en A
        delay_us  : délai en microsecondes (typiquement 100–500µs)
        """
        if not Limits.CURRENT_MIN <= current_a <= Limits.CURRENT_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_SCP", value=current_a,
                               error=f"Courant SCP hors plage [{Limits.CURRENT_MIN}, {Limits.CURRENT_MAX}]A")
        payload = struct.pack(">HH", int(current_a), delay_us) + bytes(4)
        return await self._write(WriteCmd.SET_SCP, payload,
                                 f"SET_SCP={current_a}A delay={delay_us}µs")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 7 — Protections thermiques charge
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_otp_charge(self, temp_c: float,
                             recovery_c: Optional[float] = None) -> WriteResult:
        """
        Over Temperature Protection en charge.
        Encodage : temp + 40 en uint8 (offset de 40°C).
        """
        if not Limits.TEMP_MIN <= temp_c <= Limits.TEMP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OTP_CHG", value=temp_c,
                               error=f"Temp hors plage [{Limits.TEMP_MIN}, {Limits.TEMP_MAX}]°C")
        recovery_c = recovery_c or (temp_c - 5.0)
        payload = self._pack_u8(int(temp_c) + 40, 0)
        payload = bytearray(payload)
        payload[1] = int(recovery_c) + 40
        return await self._write(WriteCmd.SET_OTP_CHG, bytes(payload),
                                 f"SET_OTP_CHG={temp_c}°C (rec={recovery_c}°C)")

    async def set_utp_charge(self, temp_c: float,
                             recovery_c: Optional[float] = None) -> WriteResult:
        """
        Under Temperature Protection en charge (protection froid — bloque la charge).
        """
        if not Limits.TEMP_MIN <= temp_c <= Limits.TEMP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_UTP_CHG", value=temp_c,
                               error=f"Temp hors plage [{Limits.TEMP_MIN}, {Limits.TEMP_MAX}]°C")
        recovery_c = recovery_c or (temp_c + 5.0)
        payload = bytearray(8)
        payload[0] = int(temp_c) + 40
        payload[1] = int(recovery_c) + 40
        return await self._write(WriteCmd.SET_UTP_CHG, bytes(payload),
                                 f"SET_UTP_CHG={temp_c}°C (rec={recovery_c}°C)")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 8 — Protections thermiques décharge
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_otp_discharge(self, temp_c: float,
                                recovery_c: Optional[float] = None) -> WriteResult:
        """Over Temperature Protection en décharge."""
        if not Limits.TEMP_MIN <= temp_c <= Limits.TEMP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_OTP_DSG", value=temp_c,
                               error=f"Temp hors plage [{Limits.TEMP_MIN}, {Limits.TEMP_MAX}]°C")
        recovery_c = recovery_c or (temp_c - 5.0)
        payload = bytearray(8)
        payload[0] = int(temp_c) + 40
        payload[1] = int(recovery_c) + 40
        return await self._write(WriteCmd.SET_OTP_DSG, bytes(payload),
                                 f"SET_OTP_DSG={temp_c}°C (rec={recovery_c}°C)")

    async def set_utp_discharge(self, temp_c: float,
                                recovery_c: Optional[float] = None) -> WriteResult:
        """Under Temperature Protection en décharge."""
        if not Limits.TEMP_MIN <= temp_c <= Limits.TEMP_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_UTP_DSG", value=temp_c,
                               error=f"Temp hors plage [{Limits.TEMP_MIN}, {Limits.TEMP_MAX}]°C")
        recovery_c = recovery_c or (temp_c + 5.0)
        payload = bytearray(8)
        payload[0] = int(temp_c) + 40
        payload[1] = int(recovery_c) + 40
        return await self._write(WriteCmd.SET_UTP_DSG, bytes(payload),
                                 f"SET_UTP_DSG={temp_c}°C (rec={recovery_c}°C)")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 9 — Balancing
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_balance_enabled(self, enable: bool) -> WriteResult:
        """Active ou désactive le système de balancing."""
        payload = self._pack_u8(0x01 if enable else 0x00)
        return await self._write(WriteCmd.SET_BALANCE_EN, payload,
                                 f"SET_BALANCE_EN={'ON' if enable else 'OFF'}")

    async def set_balance_trigger_voltage(self, voltage_v: float) -> WriteResult:
        """
        Tension cellule à partir de laquelle le balancing peut s'activer.
        Ex. 3.40V — en dessous de ce seuil, pas de balancing même si delta élevé.
        Encodage : mV en uint16.
        """
        if not Limits.BALANCE_V_MIN <= voltage_v <= Limits.BALANCE_V_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_BALANCE_V", value=voltage_v,
                               error=f"Tension balancing hors plage [{Limits.BALANCE_V_MIN}, {Limits.BALANCE_V_MAX}]V")
        raw = self._v_to_raw(voltage_v * 1000)
        payload = self._pack_u16(raw)
        return await self._write(WriteCmd.SET_BALANCE_V, payload,
                                 f"SET_BALANCE_TRIGGER_V={voltage_v}V")

    async def set_balance_trigger_delta(self, delta_mv: int) -> WriteResult:
        """
        Différentiel min entre cellule max et min pour déclencher le balancing.
        Ex. 10mV — en dessous de ce delta, pas de balancing.
        Encodage : mV en uint16.
        """
        if not Limits.BALANCE_DELTA_MIN <= delta_mv <= Limits.BALANCE_DELTA_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_BALANCE_DELTA", value=delta_mv,
                               error=f"Delta balancing hors plage [{Limits.BALANCE_DELTA_MIN}, {Limits.BALANCE_DELTA_MAX}]mV")
        payload = self._pack_u16(delta_mv)
        return await self._write(WriteCmd.SET_BALANCE_DELTA, payload,
                                 f"SET_BALANCE_DELTA={delta_mv}mV")

    async def set_balance_mode(self, always_on: bool) -> WriteResult:
        """
        Mode de balancing :
        always_on=False → balancing uniquement pendant la charge (recommandé LiFePO4)
        always_on=True  → balancing actif en permanence (charge + repos + décharge)
        """
        payload = self._pack_u8(0x01 if always_on else 0x00)
        mode_str = "TOUJOURS_ACTIF" if always_on else "CHARGE_SEULEMENT"
        return await self._write(WriteCmd.SET_BALANCE_MODE, payload,
                                 f"SET_BALANCE_MODE={mode_str}")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 10 — Paramètres du pack
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_capacity(self, capacity_ah: int) -> WriteResult:
        """
        Capacité nominale du pack en Ah.
        Encodage : uint32 big-endian en mAh (capacity_ah × 1000) à l'offset 0.
        """
        if not Limits.CAPACITY_MIN <= capacity_ah <= Limits.CAPACITY_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_CAPACITY", value=capacity_ah,
                               error=f"Capacité hors plage [{Limits.CAPACITY_MIN}, {Limits.CAPACITY_MAX}]Ah")
        raw_mah = capacity_ah * 1000
        payload = self._pack_u32(raw_mah)
        return await self._write(WriteCmd.SET_CAPACITY, payload,
                                 f"SET_CAPACITY={capacity_ah}Ah")

    async def set_cell_count(self, count: int) -> WriteResult:
        """Nombre de cellules en série (ex. 16 pour 16S)."""
        if not Limits.CELL_COUNT_MIN <= count <= Limits.CELL_COUNT_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_CELL_COUNT", value=count,
                               error=f"Nombre cellules hors plage [{Limits.CELL_COUNT_MIN}, {Limits.CELL_COUNT_MAX}]")
        payload = self._pack_u8(count)
        return await self._write(WriteCmd.SET_CELL_COUNT, payload,
                                 f"SET_CELL_COUNT={count}S")

    async def set_sensor_count(self, count: int) -> WriteResult:
        """Nombre de sondes NTC actives."""
        if not Limits.SENSOR_COUNT_MIN <= count <= Limits.SENSOR_COUNT_MAX:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_SENSOR_COUNT", value=count,
                               error=f"Nombre sondes hors plage [{Limits.SENSOR_COUNT_MIN}, {Limits.SENSOR_COUNT_MAX}]")
        payload = self._pack_u8(count)
        return await self._write(WriteCmd.SET_SENSOR_COUNT, payload,
                                 f"SET_SENSOR_COUNT={count}")

    async def set_chemistry(self, chemistry: str) -> WriteResult:
        """
        Chimie de la batterie.
        Valeurs acceptées : 'LiFePO4' (0), 'LiIon' (1), 'LTO' (2)
        """
        chem_map = {"LiFePO4": 0, "LiIon": 1, "LTO": 2}
        if chemistry not in chem_map:
            return WriteResult(success=False, bms_id=self.bms_id,
                               cmd="SET_CHEMISTRY", value=chemistry,
                               error=f"Chimie inconnue — valeurs : {list(chem_map.keys())}")
        payload = self._pack_u8(chem_map[chemistry])
        return await self._write(WriteCmd.SET_CHEMISTRY, payload,
                                 f"SET_CHEMISTRY={chemistry}")

    # ═══════════════════════════════════════════════════════════════════════════
    # GROUPE 11 — Application de profils complets
    # ═══════════════════════════════════════════════════════════════════════════

    async def apply_profile(self, profile: dict) -> list[WriteResult]:
        """
        Applique un profil de configuration complet depuis un dictionnaire.
        Toutes les commandes sont soumises séquentiellement via la CommandQueue.
        Retourne la liste de tous les WriteResult.

        Exemple de profil (config.yaml → dict) :
        {
          "ovp_cell_v": 3.65,  "uvp_cell_v": 2.80,
          "ovp_pack_v": 58.4,  "uvp_pack_v": 44.8,
          "ocp_chg_a": 70,     "ocp_dsg_a": 100,
          "otp_chg_c": 45,     "utp_chg_c": 0,
          "otp_dsg_c": 60,     "utp_dsg_c": -10,
          "balance_en": True,  "balance_v": 3.40, "balance_delta_mv": 10,
          "capacity_ah": 320,  "cell_count": 16,
          "sensor_count": 4,   "chemistry": "LiFePO4",
        }
        """
        results: list[WriteResult] = []
        handlers = [
            ("ovp_cell_v",      lambda v: self.set_ovp_cell(v)),
            ("uvp_cell_v",      lambda v: self.set_uvp_cell(v)),
            ("ovp_pack_v",      lambda v: self.set_ovp_pack(v)),
            ("uvp_pack_v",      lambda v: self.set_uvp_pack(v)),
            ("ocp_chg_a",       lambda v: self.set_ocp_charge(v)),
            ("ocp_dsg_a",       lambda v: self.set_ocp_discharge(v)),
            ("scp_a",           lambda v: self.set_scp(v)),
            ("otp_chg_c",       lambda v: self.set_otp_charge(v)),
            ("utp_chg_c",       lambda v: self.set_utp_charge(v)),
            ("otp_dsg_c",       lambda v: self.set_otp_discharge(v)),
            ("utp_dsg_c",       lambda v: self.set_utp_discharge(v)),
            ("balance_en",      lambda v: self.set_balance_enabled(v)),
            ("balance_v",       lambda v: self.set_balance_trigger_voltage(v)),
            ("balance_delta_mv",lambda v: self.set_balance_trigger_delta(int(v))),
            ("balance_always",  lambda v: self.set_balance_mode(v)),
            ("capacity_ah",     lambda v: self.set_capacity(int(v))),
            ("cell_count",      lambda v: self.set_cell_count(int(v))),
            ("sensor_count",    lambda v: self.set_sensor_count(int(v))),
            ("chemistry",       lambda v: self.set_chemistry(v)),
        ]
        for key, fn in handlers:
            if key in profile:
                result = await fn(profile[key])
                results.append(result)
                if not result.success:
                    log.error(f"[BMS{self.bms_id}] Profil interrompu sur erreur : {result}")
                    break
        return results


# ─── Gestionnaire multi-BMS avec couche d'écriture ───────────────────────────
class DalyWriteManager:
    """
    Encapsule DalyBusManager + CommandQueue + DalyWriter pour chaque BMS.
    Point d'entrée unique pour toutes les opérations lecture/écriture.

    Usage :
        async with DalyWriteManager("/dev/ttyUSB1", [0x01, 0x02]) as mgr:
            snap = await mgr.snapshot_all()
            result = await mgr.writer(0x01).set_ovp_cell(3.65)
    """
    def __init__(self, port_path: str, bms_ids: list[int] = None,
                 baudrate: int = 9600, cell_count: int = 16, sensor_count: int = 4):
        self._bus = DalyBusManager(port_path, bms_ids, baudrate, cell_count, sensor_count)
        self._queue = CommandQueue()
        self._writers: dict[int, DalyWriter] = {}

    async def __aenter__(self):
        await self._bus.open()
        self._queue.start()
        for bid in self._bus.bms_ids:
            self._writers[bid] = DalyWriter(self._bus.bms(bid), self._queue)
        log.info(f"DalyWriteManager prêt — {len(self._writers)} BMS, queue démarrée")
        return self

    async def __aexit__(self, *args):
        await self._queue.stop()
        await self._bus.close()

    def writer(self, bms_id: int) -> DalyWriter:
        if bms_id not in self._writers:
            raise KeyError(f"BMS {bms_id:#04x} non configuré")
        return self._writers[bms_id]

    async def snapshot_all(self) -> dict:
        return await self._bus.snapshot_all()

    async def poll_loop(self, callback, interval: float = 1.0):
        await self._bus.poll_loop(callback, interval)


# ─── Profils préconfigurés ────────────────────────────────────────────────────
PROFILE_SANTUARIO_320AH = {
    "ovp_cell_v":       3.65,
    "uvp_cell_v":       2.80,
    "ovp_pack_v":       58.4,
    "uvp_pack_v":       44.8,
    "ocp_chg_a":        70,
    "ocp_dsg_a":        100,
    "scp_a":            200,
    "otp_chg_c":        45,
    "utp_chg_c":        0,
    "otp_dsg_c":        60,
    "utp_dsg_c":        -10,
    "balance_en":       True,
    "balance_v":        3.40,
    "balance_delta_mv": 10,
    "balance_always":   False,
    "capacity_ah":      320,
    "cell_count":       16,
    "sensor_count":     4,
    "chemistry":        "LiFePO4",
}

PROFILE_SANTUARIO_360AH = {
    **PROFILE_SANTUARIO_320AH,
    "capacity_ah": 360,
}


# ─── Point d'entrée rapide pour test ─────────────────────────────────────────
async def _demo(port: str = "/dev/ttyUSB1"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    async with DalyWriteManager(port, bms_ids=[0x01, 0x02]) as mgr:
        # Lecture snapshot avant
        snaps = await mgr.snapshot_all()
        for bid, snap in snaps.items():
            from daly_protocol import log_snapshot
            log_snapshot(snap)

        # Application du profil Santuario sur BMS1 (320Ah)
        log.info("Application profil Santuario 320Ah sur BMS 0x01...")
        results = await mgr.writer(0x01).apply_profile(PROFILE_SANTUARIO_320AH)
        failed = [r for r in results if not r.success]
        log.info(f"Profil appliqué — {len(results)} commandes, {len(failed)} erreurs")
        for r in failed:
            log.error(str(r))

if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB1"
    asyncio.run(_demo(port))
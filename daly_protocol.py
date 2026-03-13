"""
daly_protocol.py — D1 : Module de Communication UART Daly BMS
Protocole binaire Daly : start byte 0xA5, adressage multi-BMS, checksum
Compatible : Daly Smart BMS 16S LiFePO4 — Installation Santuario, Badalucco
"""

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

# ─── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger("daly.protocol")

# ─── Constantes protocole ─────────────────────────────────────────────────────
START_BYTE      = 0xA5
HOST_ADDR       = 0x40          # Adresse hôte → BMS
FRAME_DATA_LEN  = 0x08          # Longueur data dans une requête
RESP_HEADER_LEN = 4             # start + addr + cmd + len
DEFAULT_TIMEOUT = 0.5           # secondes
DEFAULT_RETRIES = 3

# ─── Commandes Daly ───────────────────────────────────────────────────────────
class Cmd(IntEnum):
    SOC_DATA        = 0x90  # Tension pack, courant, SOC
    MINMAX_CELL_V   = 0x91  # Cellule min/max tension
    MINMAX_TEMP     = 0x92  # Température min/max
    MOS_STATUS      = 0x93  # État MOSFET CHG/DSG
    STATUS_INFO     = 0x94  # Infos générales pack
    CELL_VOLTAGES   = 0x95  # Tensions individuelles cellules
    TEMPERATURES    = 0x96  # Températures individuelles
    BALANCE_STATUS  = 0x97  # État balancing par cellule
    FAILURE_FLAGS   = 0x98  # Flags de protection / alarmes
    SET_DISCHARGE   = 0xD9  # Contrôle MOS décharge
    SET_CHARGE      = 0xDA  # Contrôle MOS charge
    SET_SOC         = 0x21  # Calibration SOC
    RESET           = 0x00  # Reset BMS

# ─── Structures de données ────────────────────────────────────────────────────
@dataclass
class SocData:
    bms_id: int
    timestamp: float
    pack_voltage: float         # V
    pack_current: float         # A (positif = charge, négatif = décharge)
    soc: float                  # %
    power: float                # W calculé

@dataclass
class MinMaxCellVoltage:
    bms_id: int
    timestamp: float
    max_voltage: float          # mV
    max_cell_num: int
    min_voltage: float          # mV
    min_cell_num: int
    delta: float                # mV

@dataclass
class MinMaxTemperature:
    bms_id: int
    timestamp: float
    max_temp: float             # °C
    max_sensor_num: int
    min_temp: float             # °C
    min_sensor_num: int

@dataclass
class MosStatus:
    bms_id: int
    timestamp: float
    mode: int                   # 0=stationary, 1=charge, 2=discharge
    charge_mos: bool
    discharge_mos: bool
    bms_cycles: int
    remaining_capacity: float   # Ah

@dataclass
class StatusInfo:
    bms_id: int
    timestamp: float
    cell_count: int
    sensor_count: int
    charger_running: bool
    load_running: bool
    states: int                 # Byte de flags divers
    cycle_count: int

@dataclass
class CellVoltages:
    bms_id: int
    timestamp: float
    voltages: list[float]       # mV par cellule
    average: float
    minimum: float
    maximum: float
    delta: float

@dataclass
class Temperatures:
    bms_id: int
    timestamp: float
    temps: list[float]          # °C par sonde

@dataclass
class BalanceStatus:
    bms_id: int
    timestamp: float
    balancing: list[bool]       # True si la cellule i est en train de balancer

@dataclass
class FailureFlags:
    bms_id: int
    timestamp: float
    # Byte 0 — alarmes tension (bits 0-1=OVP cellule L1/L2, 2-3=UVP cellule, 4-5=OVP pack, 6-7=UVP pack)
    cell_ovp: bool              # Sur-tension cellule (L1 ou L2)
    cell_uvp: bool              # Sous-tension cellule (L1 ou L2)
    pack_ovp: bool              # Sur-tension pack (L1 ou L2)
    pack_uvp: bool              # Sous-tension pack (L1 ou L2)
    # Byte 1 — alarmes température (bits 0-1=OTP chg L1/L2, 2-3=UTP chg, 4-5=OTP dsg, 6-7=UTP dsg)
    chg_otp: bool               # Sur-température charge (L1 ou L2)
    chg_utp: bool               # Sous-température charge (L1 ou L2)
    dsg_otp: bool               # Sur-température décharge (L1 ou L2)
    dsg_utp: bool               # Sous-température décharge (L1 ou L2)
    # Byte 2 — alarmes courant/SOC (bits 0-1=OCP chg, 2-3=OCP dsg, 4-7=SOC)
    chg_ocp: bool               # Sur-courant charge (L1 ou L2)
    dsg_ocp: bool               # Sur-courant décharge (L1 ou L2)
    soc_err: bool               # SOC hors plage (trop haut ou trop bas)
    # Byte 3 — différentiels (bits 0-1=diff tension cellules, 2-3=diff sonde temp)
    cell_v_diff: bool           # Différentiel tension cellules trop élevé
    cell_v_diff_2: bool         # Différentiel sonde température trop élevé
    # Byte 4 — défauts FET (sur-température + adhérence + disjoncteur)
    bat_err: bool               # Défaut FET (sur-temp, adhérence ou disjoncteur)
    # Byte 5 — défauts modules (AFE, capteurs, EEPROM, RTC, communication)
    sensor_err: bool            # Défaut module capteur température
    slave_comm: bool            # Défaut communication interne (véhicule/intranet)
    dtu_fault: bool             # Défaut communication véhicule
    # Byte 6 — défauts protection (court-circuit, capteur courant, basse tension)
    scp: bool                   # Défaut module protection court-circuit
    # Résumé
    any_alarm: bool

@dataclass
class BmsSnapshot:
    """Snapshot complet d'un BMS — agrégat de toutes les commandes de lecture"""
    bms_id: int
    timestamp: float
    soc:      Optional[SocData]          = None
    minmax_v: Optional[MinMaxCellVoltage] = None
    minmax_t: Optional[MinMaxTemperature] = None
    mos:      Optional[MosStatus]        = None
    status:   Optional[StatusInfo]       = None
    cells:    Optional[CellVoltages]     = None
    temps:    Optional[Temperatures]     = None
    balance:  Optional[BalanceStatus]    = None
    alarms:   Optional[FailureFlags]     = None

# ─── Couche protocole ─────────────────────────────────────────────────────────
def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF

def _build_request(bms_id: int, cmd: Cmd) -> bytes:
    """
    Construit une trame de requête Daly.
    Format : [0xA5] [addr_host] [cmd] [0x08] [8x 0x00] [checksum]
    """
    frame = bytes([START_BYTE, HOST_ADDR, int(cmd), FRAME_DATA_LEN]) + bytes(8)
    return frame + bytes([_checksum(frame)])

def _validate_response(data: bytes, expected_cmd: Cmd, bms_id: int) -> bool:
    """Vérifie start byte, adresse BMS, command ID et checksum."""
    if len(data) < RESP_HEADER_LEN + 1:
        log.warning(f"[BMS{bms_id}] Réponse trop courte : {len(data)} octets")
        return False
    if data[0] != START_BYTE:
        log.warning(f"[BMS{bms_id}] Start byte invalide : 0x{data[0]:02X}")
        return False
    if data[1] != bms_id:
        log.warning(f"[BMS{bms_id}] Adresse BMS inattendue : 0x{data[1]:02X}")
        return False
    if data[2] != int(expected_cmd):
        log.warning(f"[BMS{bms_id}] Cmd inattendue : 0x{data[2]:02X} (attendu 0x{int(expected_cmd):02X})")
        return False
    expected_crc = _checksum(data[:-1])
    if data[-1] != expected_crc:
        log.warning(f"[BMS{bms_id}] CRC invalide : reçu 0x{data[-1]:02X}, calculé 0x{expected_crc:02X}")
        return False
    return True

def _parse_soc(bms_id: int, raw: bytes) -> SocData:
    d = raw[4:12]
    pack_v  = struct.unpack(">H", d[0:2])[0] / 10.0      # 0.1V résolution
    raw_cur = struct.unpack(">H", d[2:4])[0]
    current = (raw_cur - 30000) / 10.0                    # offset 30000, 0.1A résolution
    soc     = struct.unpack(">H", d[6:8])[0] / 10.0       # 0.1% résolution
    return SocData(
        bms_id=bms_id,
        timestamp=time.time(),
        pack_voltage=pack_v,
        pack_current=current,
        soc=soc,
        power=round(pack_v * current, 1),
    )

def _parse_minmax_cell_voltage(bms_id: int, raw: bytes) -> MinMaxCellVoltage:
    d = raw[4:12]
    max_v    = struct.unpack(">H", d[0:2])[0]   # mV
    max_num  = d[2]
    min_v    = struct.unpack(">H", d[3:5])[0]   # mV
    min_num  = d[5]
    return MinMaxCellVoltage(
        bms_id=bms_id,
        timestamp=time.time(),
        max_voltage=max_v,
        max_cell_num=max_num,
        min_voltage=min_v,
        min_cell_num=min_num,
        delta=max_v - min_v,
    )

def _parse_minmax_temp(bms_id: int, raw: bytes) -> MinMaxTemperature:
    d = raw[4:12]
    max_t   = d[0] - 40
    max_s   = d[1]
    min_t   = d[2] - 40
    min_s   = d[3]
    return MinMaxTemperature(
        bms_id=bms_id,
        timestamp=time.time(),
        max_temp=float(max_t),
        max_sensor_num=max_s,
        min_temp=float(min_t),
        min_sensor_num=min_s,
    )

def _parse_mos_status(bms_id: int, raw: bytes) -> MosStatus:
    d = raw[4:12]
    mode         = d[0]
    charge_mos   = bool(d[1])
    discharge_mos = bool(d[2])
    cycles       = d[3]                                      # heartbeat byte (0-255)
    remain_cap   = struct.unpack(">I", d[4:8])[0] / 1000.0  # mAh → Ah
    return MosStatus(
        bms_id=bms_id,
        timestamp=time.time(),
        mode=mode,
        charge_mos=charge_mos,
        discharge_mos=discharge_mos,
        bms_cycles=cycles,
        remaining_capacity=remain_cap,
    )

def _parse_status_info(bms_id: int, raw: bytes) -> StatusInfo:
    d = raw[4:12]
    return StatusInfo(
        bms_id=bms_id,
        timestamp=time.time(),
        cell_count=d[0],
        sensor_count=d[1],
        charger_running=bool(d[2]),
        load_running=bool(d[3]),
        states=d[4],
        cycle_count=struct.unpack(">H", d[5:7])[0],
    )

def _parse_cell_voltages(bms_id: int, frames: list[bytes]) -> CellVoltages:
    """
    Les tensions cellules arrivent en plusieurs trames (3 cellules par trame).
    Chaque trame contient : [frame_num] [v1_H] [v1_L] [v2_H] [v2_L] [v3_H] [v3_L] [pad] [pad]
    """
    voltages = []
    for raw in frames:
        d = raw[4:12]
        for i in range(3):
            v = struct.unpack(">H", d[1 + i*2: 3 + i*2])[0]
            if v > 0:
                voltages.append(float(v))   # mV
    if not voltages:
        return CellVoltages(bms_id, time.time(), [], 0.0, 0.0, 0.0, 0.0)
    avg  = round(sum(voltages) / len(voltages), 1)
    return CellVoltages(
        bms_id=bms_id,
        timestamp=time.time(),
        voltages=voltages,
        average=avg,
        minimum=min(voltages),
        maximum=max(voltages),
        delta=round(max(voltages) - min(voltages), 1),
    )

def _parse_temperatures(bms_id: int, frames: list[bytes]) -> Temperatures:
    """2 sondes par trame, offset de 40°C."""
    temps = []
    for raw in frames:
        d = raw[4:12]
        for i in range(1, 8, 1):
            if d[i] != 0:
                temps.append(float(d[i] - 40))
    return Temperatures(bms_id=bms_id, timestamp=time.time(), temps=temps)

def _parse_balance_status(bms_id: int, raw: bytes) -> BalanceStatus:
    """
    6 octets de flags, bit i = cellule i en balancing.
    Bit 0 de l'octet 0 = cellule 1, bit 1 = cellule 2, …, bit 7 = cellule 8, etc.
    Supporte jusqu'à 48 cellules (protocole Daly standard).
    """
    d = raw[4:10]
    # little-endian pour préserver l'ordre : cellule 1 = d[0] bit 0
    bits = int.from_bytes(d, "little")
    balancing = [(bits >> i) & 1 == 1 for i in range(48)]
    return BalanceStatus(bms_id=bms_id, timestamp=time.time(), balancing=balancing)

def _parse_failure_flags(bms_id: int, raw: bytes) -> FailureFlags:
    d = raw[4:12]
    # Byte 0 : alarmes tension (cellule + pack)
    b0 = d[0]
    # Byte 1 : alarmes température (charge + décharge)
    b1 = d[1]
    # Byte 2 : alarmes courant (OCP charge/décharge) + SOC
    b2 = d[2]
    # Byte 3 : différentiel tension/température
    b3 = d[3]
    # Bytes 4-6 : défauts modules (FET, communication, protection)
    b4, b5, b6 = d[4], d[5], d[6]
    flags = FailureFlags(
        bms_id=bms_id,
        timestamp=time.time(),
        # b0 bits 0-1 : OVP cellule L1+L2 / bits 2-3 : UVP cellule L1+L2
        # bits 4-5 : OVP pack L1+L2 / bits 6-7 : UVP pack L1+L2
        cell_ovp      = bool(b0 & 0x03),
        cell_uvp      = bool(b0 & 0x0C),
        pack_ovp      = bool(b0 & 0x30),
        pack_uvp      = bool(b0 & 0xC0),
        # b1 bits 0-1 : OTP charge L1+L2 / bits 2-3 : UTP charge L1+L2
        # bits 4-5 : OTP décharge L1+L2 / bits 6-7 : UTP décharge L1+L2
        chg_otp       = bool(b1 & 0x03),
        chg_utp       = bool(b1 & 0x0C),
        dsg_otp       = bool(b1 & 0x30),
        dsg_utp       = bool(b1 & 0xC0),
        # b2 bits 0-1 : OCP charge L1+L2 / bits 2-3 : OCP décharge L1+L2
        chg_ocp       = bool(b2 & 0x03),
        dsg_ocp       = bool(b2 & 0x0C),
        soc_err       = bool(b2 & 0xF0),   # bits 4-7 : SOC trop haut/bas L1+L2
        # b3 : différentiel tension cellule (bits 0-1) + différentiel temp sonde (bits 2-3)
        cell_v_diff   = bool(b3 & 0x03),
        cell_v_diff_2 = bool(b3 & 0x0C),
        # b4 : sur-température FET + défauts adhérence/disjoncteur FET
        bat_err       = bool(b4),
        # b5 : défauts modules (AFE, capteurs, EEPROM, RTC, précharge, communication)
        slave_comm    = bool(b5 & 0xC0),   # bits 6-7 : comm véhicule + intranet
        sensor_err    = bool(b5 & 0x04),   # bit 2 : module capteur température
        # b6 bit 2 : défaut module protection court-circuit
        scp           = bool(b6 & 0x04),
        dtu_fault     = bool(b5 & 0x40),   # bit 6 : communication véhicule
        any_alarm     = (b0 | b1 | b2 | b3 | b4 | b5 | b6) != 0,
    )
    return flags

# ─── Interface de bas niveau ──────────────────────────────────────────────────
class DalyPort:
    """
    Gestion du port série UART.
    Utilisation : async with DalyPort("/dev/ttyUSB0") as port:
    """
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = DEFAULT_TIMEOUT):
        self.port     = port
        self.baudrate = baudrate
        self.timeout  = timeout
        self._reader: Optional[asyncio.StreamReader]  = None
        self._writer: Optional[asyncio.StreamWriter]  = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        """Vrai si le writer asyncio est actif et non en cours de fermeture."""
        return self._writer is not None and not self._writer.is_closing()

    async def open_with_retry(self, max_attempts: int = 8,
                               initial_delay: float = 3.0) -> bool:
        """
        Tente d'ouvrir le port avec backoff exponentiel.
        Séquence de délais : 3s → 5.4s → 9.7s → 17.5s → … → max 45s.
        Retourne True si l'ouverture réussit, False si tous les essais échouent.
        """
        import serial_asyncio
        from serial import SerialException
        delay = initial_delay
        for attempt in range(1, max_attempts + 1):
            try:
                # Ferme proprement si un writer résiduel existe
                if self._writer and not self._writer.is_closing():
                    self._writer.close()
                    try:
                        await self._writer.wait_closed()
                    except Exception:
                        pass
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=self.port,
                    baudrate=self.baudrate,
                )
                # Vide les éventuels octets résiduels dans le buffer kernel
                await self.flush()
                log.info(f"Port ouvert : {self.port} @ {self.baudrate} baud (essai {attempt}/{max_attempts})")
                return True
            except (SerialException, OSError) as exc:
                log.warning(f"Échec ouverture {self.port} (essai {attempt}/{max_attempts}) : {exc}")
                if attempt < max_attempts:
                    log.info(f"Nouvelle tentative dans {delay:.1f}s…")
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.8, 45.0)
        log.error(f"Impossible d'ouvrir {self.port} après {max_attempts} essais — abandon")
        self._reader = None
        self._writer = None
        return False

    async def open(self):
        """Ouvre le port avec retry automatique. Lève RuntimeError si tous les essais échouent."""
        if not await self.open_with_retry():
            raise RuntimeError(f"Impossible d'ouvrir le port UART : {self.port}")

    async def close(self):
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        log.info(f"Port fermé : {self.port}")

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def send_frame(self, frame: bytes) -> None:
        if not self._writer:
            raise RuntimeError("Port non ouvert")
        self._writer.write(frame)
        await self._writer.drain()
        log.debug(f"TX → {frame.hex(' ').upper()}")

    async def receive_frame(self, expected_len: int) -> Optional[bytes]:
        if not self._reader:
            raise RuntimeError("Port non ouvert")
        try:
            data = await asyncio.wait_for(
                self._reader.readexactly(expected_len),
                timeout=self.timeout
            )
            log.debug(f"RX ← {data.hex(' ').upper()}")
            return data
        except asyncio.TimeoutError:
            log.warning("Timeout réception — pas de réponse BMS")
            return None
        except asyncio.IncompleteReadError as e:
            log.warning(f"Réponse incomplète : {len(e.partial)} octets reçus sur {expected_len} attendus")
            return None

    async def flush(self):
        """Vide le buffer de lecture en cas de réponse parasite."""
        if self._reader:
            try:
                await asyncio.wait_for(self._reader.read(256), timeout=0.05)
            except asyncio.TimeoutError:
                pass

# ─── Couche commandes ─────────────────────────────────────────────────────────
class DalyBms:
    """
    Interface de haut niveau pour un BMS Daly identifié par son adresse (0x01 à 0xFF).
    Partage un DalyPort avec d'autres instances pour le multi-BMS sur même bus UART.
    Compatible RS485 : jusqu'à 32 unités par segment (standard), 255 adresses (protocole).
    """
    def __init__(self, port: DalyPort, bms_id: int,
                 retries: int = DEFAULT_RETRIES, timeout: float = DEFAULT_TIMEOUT):
        if not (0x01 <= bms_id <= 0xFF):
            raise ValueError(f"bms_id doit être entre 0x01 et 0xFF, reçu : {bms_id:#04x}")
        self.port    = port
        self.bms_id  = bms_id
        self.retries = retries
        self.timeout = timeout

    # ── Envoi / Réception générique ───────────────────────────────────────────
    async def _query(self, cmd: Cmd, resp_data_len: int,
                     extra_frames: int = 0) -> Optional[list[bytes]]:
        """
        Envoie une commande et récupère la/les réponse(s).
        resp_data_len : longueur du champ DATA dans la réponse (hors header/CRC)
        extra_frames  : pour les commandes multi-trames (cellules, températures)
        """
        total_frames = 1 + extra_frames
        single_frame_len = RESP_HEADER_LEN + resp_data_len + 1  # +1 CRC

        for attempt in range(1, self.retries + 1):
            async with self.port._lock:
                await self.port.flush()
                req = _build_request(self.bms_id, cmd)
                await self.port.send_frame(req)

                frames = []
                ok = True
                for _ in range(total_frames):
                    raw = await self.port.receive_frame(single_frame_len)
                    if raw is None or not _validate_response(raw, cmd, self.bms_id):
                        ok = False
                        break
                    frames.append(raw)

                if ok:
                    return frames

            log.warning(f"[BMS{self.bms_id}] Tentative {attempt}/{self.retries} échouée — cmd 0x{int(cmd):02X}")
            await asyncio.sleep(0.1 * attempt)

        log.error(f"[BMS{self.bms_id}] Commande 0x{int(cmd):02X} échouée après {self.retries} tentatives")
        return None

    async def _command(self, cmd: Cmd, payload: bytes = bytes(8)) -> bool:
        """Envoie une commande d'écriture (payload non vide) — retourne True si ACK reçu."""
        frame = bytes([START_BYTE, HOST_ADDR, int(cmd), 0x08]) + payload[:8]
        frame += bytes([_checksum(frame)])
        for attempt in range(1, self.retries + 1):
            async with self.port._lock:
                await self.port.flush()
                await self.port.send_frame(frame)
                ack = await self.port.receive_frame(RESP_HEADER_LEN + 8 + 1)
                if ack and _validate_response(ack, cmd, self.bms_id):
                    log.info(f"[BMS{self.bms_id}] Commande 0x{int(cmd):02X} ACK OK")
                    return True
            log.warning(f"[BMS{self.bms_id}] Commande 0x{int(cmd):02X} tentative {attempt} — pas d'ACK")
            await asyncio.sleep(0.2 * attempt)
        return False

    # ── Commandes de lecture ──────────────────────────────────────────────────
    async def get_soc(self) -> Optional[SocData]:
        frames = await self._query(Cmd.SOC_DATA, 8)
        return _parse_soc(self.bms_id, frames[0]) if frames else None

    async def get_minmax_cell_voltage(self) -> Optional[MinMaxCellVoltage]:
        frames = await self._query(Cmd.MINMAX_CELL_V, 8)
        return _parse_minmax_cell_voltage(self.bms_id, frames[0]) if frames else None

    async def get_minmax_temperature(self) -> Optional[MinMaxTemperature]:
        frames = await self._query(Cmd.MINMAX_TEMP, 8)
        return _parse_minmax_temp(self.bms_id, frames[0]) if frames else None

    async def get_mos_status(self) -> Optional[MosStatus]:
        frames = await self._query(Cmd.MOS_STATUS, 8)
        return _parse_mos_status(self.bms_id, frames[0]) if frames else None

    async def get_status_info(self) -> Optional[StatusInfo]:
        frames = await self._query(Cmd.STATUS_INFO, 8)
        return _parse_status_info(self.bms_id, frames[0]) if frames else None

    async def get_cell_voltages(self, cell_count: int = 16) -> Optional[CellVoltages]:
        """
        3 cellules par trame → nombre de trames = ceil(cell_count / 3)
        """
        import math
        n_frames = math.ceil(cell_count / 3)
        frames = await self._query(Cmd.CELL_VOLTAGES, 8, extra_frames=n_frames - 1)
        return _parse_cell_voltages(self.bms_id, frames) if frames else None

    async def get_temperatures(self, sensor_count: int = 4) -> Optional[Temperatures]:
        import math
        n_frames = math.ceil(sensor_count / 7)
        frames = await self._query(Cmd.TEMPERATURES, 8, extra_frames=n_frames - 1)
        return _parse_temperatures(self.bms_id, frames) if frames else None

    async def get_balance_status(self) -> Optional[BalanceStatus]:
        frames = await self._query(Cmd.BALANCE_STATUS, 6)
        return _parse_balance_status(self.bms_id, frames[0]) if frames else None

    async def get_failure_flags(self) -> Optional[FailureFlags]:
        frames = await self._query(Cmd.FAILURE_FLAGS, 8)
        return _parse_failure_flags(self.bms_id, frames[0]) if frames else None

    async def get_snapshot(self, cell_count: int = 16, sensor_count: int = 4) -> BmsSnapshot:
        """
        Lit tous les registres en séquence et retourne un snapshot complet.
        Délai minimal 50ms entre commandes pour ne pas saturer le bus.
        """
        snap = BmsSnapshot(bms_id=self.bms_id, timestamp=time.time())
        snap.soc      = await self.get_soc();               await asyncio.sleep(0.05)
        snap.minmax_v = await self.get_minmax_cell_voltage(); await asyncio.sleep(0.05)
        snap.minmax_t = await self.get_minmax_temperature(); await asyncio.sleep(0.05)
        snap.mos      = await self.get_mos_status();        await asyncio.sleep(0.05)
        snap.status   = await self.get_status_info();       await asyncio.sleep(0.05)
        snap.cells    = await self.get_cell_voltages(cell_count); await asyncio.sleep(0.05)
        snap.temps    = await self.get_temperatures(sensor_count); await asyncio.sleep(0.05)
        snap.balance  = await self.get_balance_status();    await asyncio.sleep(0.05)
        snap.alarms   = await self.get_failure_flags()
        return snap

    # ── Commandes de contrôle MOS ──────────────────────────────────────────────
    async def set_charge_mos(self, enable: bool) -> bool:
        """Active ou désactive le MOSFET de charge."""
        payload = bytes([0x01 if enable else 0x00]) + bytes(7)
        success = await self._command(Cmd.SET_CHARGE, payload)
        if success:
            log.info(f"[BMS{self.bms_id}] CHG MOS → {'ON' if enable else 'OFF'}")
        return success

    async def set_discharge_mos(self, enable: bool) -> bool:
        """Active ou désactive le MOSFET de décharge."""
        payload = bytes([0x01 if enable else 0x00]) + bytes(7)
        success = await self._command(Cmd.SET_DISCHARGE, payload)
        if success:
            log.info(f"[BMS{self.bms_id}] DSG MOS → {'ON' if enable else 'OFF'}")
        return success

    async def set_soc(self, soc_percent: float) -> bool:
        """
        Calibre le SOC du BMS à la valeur fournie (0.0 – 100.0 %).
        Encodage : valeur × 10 en uint16 big-endian à l'offset 4 du payload.
        """
        if not 0.0 <= soc_percent <= 100.0:
            raise ValueError(f"SOC hors plage [0, 100] : {soc_percent}")
        raw = int(soc_percent * 10) & 0xFFFF
        payload = bytes(4) + struct.pack(">H", raw) + bytes(2)
        success = await self._command(Cmd.SET_SOC, payload)
        if success:
            log.info(f"[BMS{self.bms_id}] SOC calibré → {soc_percent}%")
        return success

    async def reset(self) -> bool:
        """Envoie la commande de reset BMS."""
        payload = bytes(8)
        success = await self._command(Cmd.RESET, payload)
        if success:
            log.warning(f"[BMS{self.bms_id}] RESET envoyé — attente reconnexion 3s")
            await asyncio.sleep(3.0)
        return success

# ─── Gestionnaire multi-BMS ───────────────────────────────────────────────────
class DalyBusManager:
    """
    Gestionnaire du bus UART partagé entre plusieurs BMS Daly.

    Supporte de 1 à 255 BMS sur un même bus RS485 (limité en pratique à 32 par le
    standard TIA-485). Les adresses BMS vont de 0x01 à 0xFF.

    Usage typique : plusieurs packs LiFePO4 sur un seul adaptateur USB/RS485.
    Utiliser discover() pour détecter automatiquement les BMS présents sur le bus.
    """
    def __init__(self, port_path: str, bms_ids: list[int] = None,
                 baudrate: int = 9600, cell_count: int = 16, sensor_count: int = 4):
        self.port_path    = port_path
        self.bms_ids      = bms_ids or [0x01, 0x02]
        self.baudrate     = baudrate
        self.cell_count   = cell_count
        self.sensor_count = sensor_count
        self._port: Optional[DalyPort] = None
        self._bms: dict[int, DalyBms]  = {}
        self._consecutive_errors: int   = 0
        self._last_successful_open: float = 0.0

    async def open(self):
        self._port = DalyPort(self.port_path, self.baudrate)
        await self._port.open()
        for bid in self.bms_ids:
            self._bms[bid] = DalyBms(self._port, bid)
        log.info(f"Bus Daly initialisé : {len(self._bms)} BMS sur {self.port_path}")

    async def close(self):
        if self._port:
            await self._port.close()

    async def _reopen(self) -> bool:
        """
        Ferme le port proprement et tente de le rouvrir avec retry.
        Recrée les instances DalyBms qui tiennent une référence au DalyPort.
        Appelé uniquement depuis poll_loop, hors du contexte du verrou UART.
        """
        log.warning(f"Reconnexion UART en cours sur {self.port_path}…")
        # Fermeture propre (silencieuse si déjà fermé)
        if self._port:
            try:
                await self._port.close()
            except Exception:
                pass
        # Recrée un DalyPort vierge et tente l'ouverture avec retry
        self._port = DalyPort(self.port_path, self.baudrate)
        success = await self._port.open_with_retry()
        if success:
            # Recrée les instances DalyBms (elles pointent sur le nouveau DalyPort)
            self._bms = {bid: DalyBms(self._port, bid) for bid in self.bms_ids}
            self._consecutive_errors = 0
            self._last_successful_open = time.monotonic()
            log.info(f"Reconnexion réussie sur {self.port_path} — {len(self._bms)} BMS prêts")
        else:
            log.error(f"Reconnexion échouée sur {self.port_path}")
        return success

    @classmethod
    async def discover(cls, port_path: str, baudrate: int = 9600,
                       probe_range: range = range(1, 9),
                       timeout: float = 0.3) -> list[int]:
        """
        Sonde le bus RS485 et retourne la liste des adresses BMS répondantes.

        Envoie une requête SOC (0x90) à chaque adresse de probe_range.
        Les adresses qui répondent avec une trame valide sont retournées.
        Le port est ouvert puis fermé proprement après la découverte.

        Args:
            port_path   : chemin du port série (ex: /dev/ttyUSB0)
            baudrate    : débit UART (défaut 9600)
            probe_range : plage d'adresses à sonder (défaut 0x01..0x08)
            timeout     : timeout par sonde en secondes (défaut 0.3s)

        Returns:
            Liste d'entiers (adresses BMS trouvées), triée par ordre croissant.
        """
        from serial import SerialException
        port = DalyPort(port_path, baudrate, timeout=timeout)
        found: list[int] = []
        log.info(
            f"Découverte BMS sur {port_path} — "
            f"sonde 0x{min(probe_range):02X}..0x{max(probe_range):02X}"
        )
        try:
            if not await port.open_with_retry(max_attempts=3, initial_delay=2.0):
                log.error(f"Découverte impossible : port {port_path} inaccessible")
                return found
            for bms_id in probe_range:
                try:
                    async with port._lock:
                        await port.flush()
                        frame = _build_request(bms_id, Cmd.SOC_DATA)
                        await port.send_frame(frame)
                        resp = await port.receive_frame(RESP_HEADER_LEN + 8 + 1)
                    if resp and _validate_response(resp, Cmd.SOC_DATA, bms_id):
                        log.info(f"  ✓ BMS détecté à l'adresse 0x{bms_id:02X}")
                        found.append(bms_id)
                    else:
                        log.debug(f"  — Pas de réponse à 0x{bms_id:02X}")
                    await asyncio.sleep(0.15)
                except (SerialException, OSError, asyncio.TimeoutError) as exc:
                    log.warning(f"  ! Erreur sonde 0x{bms_id:02X} : {exc}")
        finally:
            await port.close()
        addrs = [f"0x{x:02X}" for x in found]
        log.info(f"Découverte terminée : {len(found)} BMS trouvé(s) → {addrs}")
        return found

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    def bms(self, bms_id: int) -> DalyBms:
        if bms_id not in self._bms:
            raise KeyError(f"BMS {bms_id:#04x} non configuré sur ce bus")
        return self._bms[bms_id]

    async def snapshot_all(self) -> dict[int, BmsSnapshot]:
        """Lecture séquentielle des snapshots complets de tous les BMS."""
        results = {}
        for bid, bms in self._bms.items():
            results[bid] = await bms.get_snapshot(self.cell_count, self.sensor_count)
            await asyncio.sleep(0.1)     # séparation entre BMS sur le bus
        return results

    async def poll_loop(self, callback, interval: float = 1.0):
        """
        Boucle de polling infinie avec reconnexion automatique.

        Comportement en cas d'erreur UART :
          - ferme le port immédiatement
          - attend un délai croissant : 3s × 1.5^N, plafonné à 60s
          - tente une reconnexion complète (open_with_retry)
          - après 5 erreurs consécutives sans succès : reset du compteur + log ERROR

        Callback : coroutine ou fonction sync appelée avec dict[int, BmsSnapshot].
        """
        from serial import SerialException

        log.info(f"Démarrage polling — intervalle {interval}s")
        while True:
            t0 = time.monotonic()

            # ── Vérification état du port ───────────────────────────────────
            if not (self._port and self._port.is_open):
                log.warning("Port UART fermé ou absent — tentative de reconnexion")
                if not await self._reopen():
                    await asyncio.sleep(15.0)
                    continue

            # ── Cycle de polling ────────────────────────────────────────────
            try:
                snapshots = await self.snapshot_all()
                if asyncio.iscoroutinefunction(callback):
                    await callback(snapshots)
                else:
                    callback(snapshots)
                self._consecutive_errors = 0

            except (SerialException, OSError,
                    asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                self._consecutive_errors += 1
                log.warning(
                    f"Erreur UART #{self._consecutive_errors} sur {self.port_path} : {exc}"
                )
                # Fermeture propre avant retry
                if self._port:
                    try:
                        await self._port.close()
                    except Exception:
                        pass
                # Backoff exponentiel
                wait = min(3.0 * (1.5 ** self._consecutive_errors), 60.0)
                log.info(f"Attente {wait:.1f}s avant nouvelle tentative…")
                if self._consecutive_errors >= 5:
                    log.error(
                        f"5 erreurs consécutives sur {self.port_path} — "
                        f"reset du compteur, reconnexion forcée"
                    )
                    self._consecutive_errors = 0
                await asyncio.sleep(wait)
                continue     # repart au début de la boucle → vérification is_open → _reopen

            except Exception as exc:
                log.exception(f"Erreur inattendue dans poll_loop : {exc}")
                await asyncio.sleep(10.0)

            # ── Respect de l'intervalle cible ───────────────────────────────
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_for)


# ─── Utilitaires de diagnostic ────────────────────────────────────────────────
def snapshot_to_dict(snap: BmsSnapshot) -> dict:
    """Sérialise un BmsSnapshot en dictionnaire plat pour JSON/MQTT/InfluxDB."""
    out: dict = {"bms_id": snap.bms_id, "timestamp": snap.timestamp}
    if snap.soc:
        out.update({
            "pack_voltage": snap.soc.pack_voltage,
            "pack_current": snap.soc.pack_current,
            "soc": snap.soc.soc,
            "power": snap.soc.power,
        })
    if snap.minmax_v:
        out.update({
            "cell_max_v": snap.minmax_v.max_voltage,
            "cell_max_num": snap.minmax_v.max_cell_num,
            "cell_min_v": snap.minmax_v.min_voltage,
            "cell_min_num": snap.minmax_v.min_cell_num,
            "cell_delta": snap.minmax_v.delta,
        })
    if snap.minmax_t:
        out.update({
            "temp_max": snap.minmax_t.max_temp,
            "temp_min": snap.minmax_t.min_temp,
        })
    if snap.mos:
        out.update({
            "charge_mos": snap.mos.charge_mos,
            "discharge_mos": snap.mos.discharge_mos,
            "bms_cycles": snap.mos.bms_cycles,
            "remaining_capacity": snap.mos.remaining_capacity,
        })
    if snap.cells:
        for i, v in enumerate(snap.cells.voltages, 1):
            out[f"cell_{i:02d}"] = v
        out.update({"cell_avg": snap.cells.average})
    if snap.temps:
        for i, t in enumerate(snap.temps.temps, 1):
            out[f"temp_{i:02d}"] = t
    if snap.balance:
        out["balancing_mask"] = [int(b) for b in snap.balance.balancing[:16]]
    if snap.alarms:
        out.update({
            "alarm_cell_ovp":   snap.alarms.cell_ovp,
            "alarm_cell_uvp":   snap.alarms.cell_uvp,
            "alarm_pack_ovp":   snap.alarms.pack_ovp,
            "alarm_pack_uvp":   snap.alarms.pack_uvp,
            "alarm_chg_otp":    snap.alarms.chg_otp,
            "alarm_chg_ocp":    snap.alarms.chg_ocp,
            "alarm_dsg_ocp":    snap.alarms.dsg_ocp,
            "alarm_scp":        snap.alarms.scp,
            "alarm_cell_delta": snap.alarms.cell_v_diff,
            "any_alarm":        snap.alarms.any_alarm,
        })
    return out


def log_snapshot(snap: BmsSnapshot) -> None:
    """Affiche un snapshot lisible en log INFO."""
    d = snapshot_to_dict(snap)
    log.info(
        f"[BMS{snap.bms_id}] "
        f"SOC={d.get('soc','?')}%  "
        f"V={d.get('pack_voltage','?')}V  "
        f"I={d.get('pack_current','?')}A  "
        f"Δcell={d.get('cell_delta','?')}mV  "
        f"CHG={'✓' if d.get('charge_mos') else '✗'}  "
        f"DSG={'✓' if d.get('discharge_mos') else '✗'}  "
        f"ALM={'⚠' if d.get('any_alarm') else '—'}"
    )


# ─── Point d'entrée rapide pour test ─────────────────────────────────────────
async def _demo(port: str = "/dev/ttyUSB0"):
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    async with DalyBusManager(port, bms_ids=[0x01, 0x02]) as bus:
        snaps = await bus.snapshot_all()
        for bid, snap in snaps.items():
            log_snapshot(snap)

if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    asyncio.run(_demo(port))
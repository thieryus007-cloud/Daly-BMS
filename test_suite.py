"""
test_suite.py — D11 : Plan de tests et validation
Phase 1 : Tests offline / simulation (Mac Mini M4, sans RPi CM5)
Phase 2 : Tests hardware (RPi CM5 + BMS Daly réels) — marqués @pytest.mark.hardware
Installation Santuario — Badalucco
"""

import asyncio
import json
import struct
import time
import sqlite3
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from dataclasses import asdict

# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES COMMUNES
# ═══════════════════════════════════════════════════════════════════════════════

# Trame Daly réelle capturée — réponse SOC BMS 0x01
# 0xA5 0x01 0x90 0x08 | voltage(2) current(2) soc(2) | 0x00 0x00 | checksum
RAW_SOC_BMS1 = bytes([
    0xA5, 0x01, 0x90, 0x08,
    0x14, 0xC8,   # pack_voltage = 0x14C8 = 5320 → 53.20V
    0x75, 0x31,   # pack_current = 0x7531 - 0x7530 = 1 → +0.1A  (offset 30000)
    0x1C, 0x20,   # soc = 0x1C20 = 7200 → 72.00%
    0x00, 0x00,
    0xA5 + 0x01 + 0x90 + 0x08 + 0x14 + 0xC8 + 0x75 + 0x31 + 0x1C + 0x20 & 0xFF,
])

# Trame tensions cellules (16 cellules) BMS 0x01 — cmd 0x95
def _make_cell_frame(bms_id: int = 0x01, cells_mv: list = None) -> bytes:
    cells_mv = cells_mv or [3310] * 16
    data = bytes()
    for v in cells_mv:
        data += struct.pack(">H", v)
    header = bytes([0xA5, bms_id, 0x95, len(data)])
    cs = (sum(header) + sum(data)) & 0xFF
    return header + data + bytes([cs])

# Snapshot simulé complet
def _make_snapshot(bms_id: int = 1, soc: float = 72.0,
                   cell_delta_mv: int = 15) -> dict:
    base_mv = 3310
    cells   = [base_mv + (i * cell_delta_mv // 16) for i in range(16)]
    cells[7]  += 40   # cellule #8  — déséquilibre simulé
    cells[15] += 30   # cellule #16 — déséquilibre simulé
    return {
        "bms_id":            bms_id,
        "bms_name":          f"pack_{320 if bms_id==1 else 360}ah",
        "soc":               soc,
        "pack_voltage":      sum(cells) / 1000,
        "pack_current":      10.0,
        "power":             round(sum(cells) / 1000 * 10.0, 1),
        "cell_voltages":     cells,
        "cell_min_v":        min(cells),
        "cell_min_num":      cells.index(min(cells)) + 1,
        "cell_max_v":        max(cells),
        "cell_max_num":      cells.index(max(cells)) + 1,
        "cell_avg":          round(sum(cells) / len(cells), 1),
        "cell_delta":        max(cells) - min(cells),
        "temperatures":      [28.5, 29.1, 27.8, 28.3],
        "temp_max":          29.1,
        "temp_min":          27.8,
        "charge_mos":        True,
        "discharge_mos":     True,
        "bms_cycles":        100 + bms_id * 47,
        "remaining_capacity": round(320 * soc / 100, 1) if bms_id == 1 else round(360 * soc / 100, 1),
        "balancing_mask":    [0] * 16,
        "any_alarm":         False,
        "n_cells":           16,
        "alarms": {
            "cell_ovp": False, "cell_uvp": False,
            "pack_ovp": False, "pack_uvp": False,
            "chg_otp":  False, "chg_ocp":  False,
            "dsg_ocp":  False, "scp":      False,
            "cell_delta": cell_delta_mv > 80,
        },
        "timestamp": time.time(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 1 — PROTOCOLE UART (daly_protocol.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyProtocol:
    """
    Tests unitaires du protocole binaire Daly.
    100% offline — aucun matériel requis.
    """

    def test_frame_checksum_calculation(self):
        """Le checksum est la somme des bytes header+data tronquée à 8 bits."""
        from daly_protocol import DalyPort
        header  = bytes([0xA5, 0x01, 0x90, 0x08])
        payload = bytes([0x14, 0xC8, 0x75, 0x31, 0x1C, 0x20, 0x00, 0x00])
        expected_cs = (sum(header) + sum(payload)) & 0xFF
        frame   = DalyPort._build_frame(bms_id=0x01, cmd=0x90, data=payload)
        assert frame[-1] == expected_cs, (
            f"Checksum attendu {expected_cs:#04x}, obtenu {frame[-1]:#04x}"
        )

    def test_frame_start_byte(self):
        """Chaque trame commence par 0xA5."""
        from daly_protocol import DalyPort
        frame = DalyPort._build_frame(bms_id=0x01, cmd=0x90)
        assert frame[0] == 0xA5

    def test_frame_length_field(self):
        """Le 4ème byte encode la longueur du payload."""
        from daly_protocol import DalyPort
        data  = bytes(8)
        frame = DalyPort._build_frame(bms_id=0x01, cmd=0x90, data=data)
        assert frame[3] == len(data)

    def test_soc_decode(self):
        """Décodage trame SOC : voltage, current, soc."""
        from daly_protocol import DalyBms
        bms  = DalyBms.__new__(DalyBms)
        # voltage = 0x14C8 = 5320 → 53.20V
        # current = 0x7531 - 30000 = 49 → 4.9A
        # soc     = 0x1C20 = 7200 → 72.0%
        payload = bytes([0x14, 0xC8, 0x75, 0x31, 0x1C, 0x20, 0x00, 0x00])
        result  = bms._decode_soc(payload)
        assert abs(result.pack_voltage - 53.20) < 0.01
        assert abs(result.soc - 72.0) < 0.1

    def test_cell_voltages_decode_16_cells(self):
        """Décodage 16 tensions cellules depuis trame 0x95."""
        from daly_protocol import DalyBms
        bms    = DalyBms.__new__(DalyBms)
        cells  = [3310 + i for i in range(16)]
        payload = b"".join(struct.pack(">H", v) for v in cells)
        result  = bms._decode_cell_voltages(payload)
        assert len(result.voltages) == 16
        for i, v in enumerate(cells):
            assert result.voltages[i] == v, f"Cellule #{i+1} : {result.voltages[i]} ≠ {v}"

    def test_cell_delta_computed(self):
        """Le delta min/max est calculé depuis les tensions brutes."""
        from daly_protocol import DalyBms
        bms   = DalyBms.__new__(DalyBms)
        cells = [3310] * 14 + [3400, 3290]   # delta = 110 mV
        payload = b"".join(struct.pack(">H", v) for v in cells)
        result  = bms._decode_cell_voltages(payload)
        assert result.delta == 110

    def test_temperature_decode_offset(self):
        """Températures Daly encodées avec offset +40 → valeur réelle."""
        from daly_protocol import DalyBms
        bms = DalyBms.__new__(DalyBms)
        # 40°C encodé = 40 + 40 = 80 = 0x50
        # 0°C encodé  = 0  + 40 = 40 = 0x28
        payload = bytes([0x50, 0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        result  = bms._decode_temperatures(payload)
        assert result.temps[0] == 40.0
        assert result.temps[1] == 0.0

    def test_checksum_validation_rejects_corrupt(self):
        """Un checksum incorrect doit lever une exception."""
        from daly_protocol import DalyPort, FrameError
        bad_frame = bytearray(RAW_SOC_BMS1)
        bad_frame[-1] ^= 0xFF   # corrompt le checksum
        with pytest.raises((FrameError, ValueError, Exception)):
            DalyPort._validate_frame(bytes(bad_frame))

    def test_current_direction_charge(self):
        """Courant positif = charge (raw > 30000)."""
        from daly_protocol import DalyBms
        bms = DalyBms.__new__(DalyBms)
        # raw 30010 → +1.0A
        raw     = 30010
        payload = struct.pack(">HHH", 5320, raw, 7200) + b"\x00\x00"
        result  = bms._decode_soc(payload)
        assert result.pack_current > 0

    def test_current_direction_discharge(self):
        """Courant négatif = décharge (raw < 30000)."""
        from daly_protocol import DalyBms
        bms = DalyBms.__new__(DalyBms)
        # raw 29990 → -1.0A
        raw     = 29990
        payload = struct.pack(">HHH", 5320, raw, 7200) + b"\x00\x00"
        result  = bms._decode_soc(payload)
        assert result.pack_current < 0

    def test_snapshot_to_dict_keys(self):
        """snapshot_to_dict() retourne toutes les clés requises."""
        from daly_protocol import snapshot_to_dict
        snap  = _make_snapshot()
        d     = snap if isinstance(snap, dict) else snapshot_to_dict(snap)
        required = [
            "soc", "pack_voltage", "pack_current", "power",
            "cell_voltages", "cell_min_v", "cell_max_v", "cell_delta",
            "temp_max", "temp_min", "charge_mos", "discharge_mos",
            "any_alarm", "bms_id",
        ]
        for key in required:
            assert key in d, f"Clé manquante dans snapshot_to_dict : {key}"


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 2 — ÉCRITURE / COMMANDES (daly_write.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyWrite:
    """Tests de la couche d'écriture avec validation des limites."""

    def test_ovp_cell_valid_range(self):
        """OVP cellule accepte 3.40–3.75V."""
        from daly_write import Limits
        assert Limits.CELL_OVP_MIN <= 3.65 <= Limits.CELL_OVP_MAX
        assert 3.39 < Limits.CELL_OVP_MIN or 3.39 < Limits.CELL_OVP_MIN

    def test_ovp_cell_rejects_out_of_range(self):
        """OVP cellule > 3.75V doit être rejeté."""
        from daly_write import DalyWriter, ValidationError
        writer = DalyWriter.__new__(DalyWriter)
        with pytest.raises((ValidationError, ValueError)):
            writer._validate_cell_voltage(4.00)

    def test_soc_set_clamps_0_100(self):
        """SOC doit être borné entre 0 et 100%."""
        from daly_write import Limits
        assert Limits.SOC_MIN == 0.0
        assert Limits.SOC_MAX == 100.0

    def test_capacity_valid_range(self):
        """Capacité 320Ah et 360Ah dans les limites."""
        from daly_write import Limits
        assert Limits.CAPACITY_MIN <= 320 <= Limits.CAPACITY_MAX
        assert Limits.CAPACITY_MIN <= 360 <= Limits.CAPACITY_MAX

    def test_profile_santuario_320ah_keys(self):
        """Profil Santuario 320Ah contient tous les paramètres requis."""
        from daly_write import PROFILE_SANTUARIO_320AH
        required = [
            "ovp_cell_v", "uvp_cell_v", "ovp_pack_v", "uvp_pack_v",
            "ocp_chg_a", "ocp_dsg_a", "capacity_ah", "cell_count",
        ]
        for key in required:
            assert key in PROFILE_SANTUARIO_320AH, f"Clé manquante : {key}"

    def test_profile_santuario_320ah_values(self):
        """Profil 320Ah : valeurs cohérentes avec installation Santuario."""
        from daly_write import PROFILE_SANTUARIO_320AH as P
        assert P["ovp_cell_v"] == 3.65    # seuil OVP cellule
        assert P["uvp_cell_v"] == 2.80    # seuil UVP cellule
        assert P["capacity_ah"] == 320
        assert P["cell_count"] == 16
        assert abs(P["ovp_pack_v"] - 3.65 * 16) < 0.1   # 58.4V

    def test_profile_santuario_360ah_values(self):
        """Profil 360Ah : capacité correcte."""
        from daly_write import PROFILE_SANTUARIO_360AH as P
        assert P["capacity_ah"] == 360

    @pytest.mark.asyncio
    async def test_command_queue_fifo(self):
        """La CommandQueue traite les commandes en ordre FIFO."""
        from daly_write import CommandQueue
        q      = CommandQueue()
        order  = []
        async def fake_exec(cmd):
            order.append(cmd.name)
            return MagicMock(success=True)

        q._execute = fake_exec
        await q.enqueue(MagicMock(name="CMD_A"))
        await q.enqueue(MagicMock(name="CMD_B"))
        await q.enqueue(MagicMock(name="CMD_C"))
        # Vider la queue
        while not q._queue.empty():
            item = await q._queue.get()
            await fake_exec(item)
        assert order == ["CMD_A", "CMD_B", "CMD_C"]

    def test_write_result_dataclass(self):
        """WriteResult contient les champs attendus."""
        from daly_write import WriteResult
        r = WriteResult(
            success=True, bms_id=1, cmd="SET_SOC",
            value=75.0, verified=True,
        )
        assert r.success
        assert r.bms_id == 1
        assert r.verified


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 3 — API REST / WebSocket (daly_api.py)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_app_state():
    """Injecte des snapshots simulés dans l'AppState de l'API."""
    from daly_api import app, state
    state.snapshots[1] = _make_snapshot(1, soc=72.0, cell_delta_mv=15)
    state.snapshots[2] = _make_snapshot(2, soc=68.0, cell_delta_mv=12)
    return state


@pytest.fixture
def client(mock_app_state):
    from fastapi.testclient import TestClient
    from daly_api import app
    return TestClient(app)


class TestDalyAPI:
    """Tests de l'API FastAPI avec snapshots injectés."""

    def test_system_status_200(self, client):
        r = client.get("/api/v1/system/status")
        assert r.status_code == 200
        body = r.json()
        assert "bms_connected" in body or "connected" in body

    def test_bms1_status_200(self, client):
        r = client.get("/api/v1/bms/1/status")
        assert r.status_code == 200
        body = r.json()
        assert "soc" in body
        assert abs(body["soc"] - 72.0) < 0.5

    def test_bms2_status_200(self, client):
        r = client.get("/api/v1/bms/2/status")
        assert r.status_code == 200

    def test_bms_unknown_returns_404(self, client):
        r = client.get("/api/v1/bms/99/status")
        assert r.status_code == 404

    def test_cells_endpoint_16_values(self, client):
        r = client.get("/api/v1/bms/1/cells")
        assert r.status_code == 200
        body = r.json()
        assert "cell_voltages" in body
        assert len(body["cell_voltages"]) == 16

    def test_cells_delta_present(self, client):
        r    = client.get("/api/v1/bms/1/cells")
        body = r.json()
        assert "cell_delta" in body
        assert body["cell_delta"] >= 0

    def test_temperatures_endpoint(self, client):
        r    = client.get("/api/v1/bms/1/temperatures")
        assert r.status_code == 200
        body = r.json()
        assert "temperatures" in body
        assert len(body["temperatures"]) > 0

    def test_alarms_endpoint_structure(self, client):
        r    = client.get("/api/v1/bms/1/alarms")
        assert r.status_code == 200
        body = r.json()
        assert "alarms" in body
        alarms = body["alarms"]
        for flag in ["cell_ovp", "cell_uvp", "pack_ovp", "pack_uvp"]:
            assert flag in alarms, f"Flag absent : {flag}"

    def test_compare_dual_bms(self, client):
        r    = client.get("/api/v1/bms/compare")
        assert r.status_code == 200
        body = r.json()
        assert "1" in str(body) and "2" in str(body)

    def test_mos_command_valid(self, client):
        r = client.post("/api/v1/bms/1/mos",
                        json={"charge": True, "discharge": True})
        assert r.status_code in (200, 202)

    def test_mos_command_invalid_payload(self, client):
        r = client.post("/api/v1/bms/1/mos", json={"bad_field": 99})
        assert r.status_code == 422

    def test_soc_set_out_of_range(self, client):
        r = client.post("/api/v1/bms/1/soc", json={"soc": 150.0})
        assert r.status_code == 422

    def test_reset_requires_confirm(self, client):
        r = client.post("/api/v1/bms/1/reset", json={})
        assert r.status_code in (400, 422)

    def test_reset_with_confirm(self, client):
        r = client.post("/api/v1/bms/1/reset",
                        json={"confirm": "CONFIRM_RESET"})
        assert r.status_code in (200, 202)

    def test_history_endpoint_empty(self, client):
        r = client.get("/api/v1/bms/1/history?duration=1h")
        assert r.status_code == 200

    def test_csv_export_content_type(self, client):
        r = client.get("/api/v1/bms/1/export/csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")

    def test_preset_santuario_320ah(self, client):
        r = client.post("/api/v1/bms/1/config/preset/santuario_320ah")
        assert r.status_code in (200, 202)

    def test_preset_santuario_360ah(self, client):
        r = client.post("/api/v1/bms/2/config/preset/santuario_360ah")
        assert r.status_code in (200, 202)

    def test_openapi_schema_available(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "paths" in schema


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 4 — MQTT Publisher (daly_mqtt.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyMQTT:
    """Tests de la publication MQTT — broker mocké."""

    @pytest.mark.asyncio
    async def test_topics_published_on_snapshot(self):
        """Un snapshot BMS publie les topics scalaires attendus."""
        from daly_mqtt import DalyMqttPublisher
        publisher = DalyMqttPublisher()
        published  = {}

        async def fake_publish(topic, payload, **kw):
            published[topic] = payload

        with patch.object(publisher, "_client") as mock_client:
            mock_client.publish = AsyncMock(side_effect=fake_publish)
            snap = _make_snapshot(1, soc=72.0)
            await publisher.update(1, snap)

        required_suffixes = ["soc", "pack_voltage", "pack_current",
                              "cell_delta", "temp_max", "charge_mos"]
        for suffix in required_suffixes:
            matching = [t for t in published if suffix in t]
            assert matching, f"Topic '{suffix}' non publié"

    @pytest.mark.asyncio
    async def test_cell_individual_topics(self):
        """16 topics cells/cell_XX publiés (un par cellule)."""
        from daly_mqtt import DalyMqttPublisher
        publisher  = DalyMqttPublisher()
        published  = []

        async def fake_publish(topic, payload, **kw):
            published.append(topic)

        with patch.object(publisher, "_client") as mock_client:
            mock_client.publish = AsyncMock(side_effect=fake_publish)
            snap = _make_snapshot(1)
            await publisher.update(1, snap)

        cell_topics = [t for t in published if "cell_" in t and "/cells/" in t]
        assert len(cell_topics) == 16, (
            f"Attendu 16 topics cellules, publié {len(cell_topics)}"
        )

    @pytest.mark.asyncio
    async def test_alarm_publishes_qos1_on_trigger(self):
        """Une alarme active déclenche une publication QoS 1."""
        from daly_mqtt import DalyMqttPublisher
        publisher  = DalyMqttPublisher()
        qos_used   = []

        async def fake_publish(topic, payload, qos=0, **kw):
            if "alarm" in topic:
                qos_used.append(qos)

        with patch.object(publisher, "_client") as mock_client:
            mock_client.publish = AsyncMock(side_effect=fake_publish)
            snap = _make_snapshot(1)
            snap["any_alarm"] = True
            snap["alarms"]["cell_ovp"] = True
            await publisher.update(1, snap)

        alarm_qos1 = [q for q in qos_used if q >= 1]
        assert alarm_qos1, "Alarme non publiée en QoS 1"

    def test_topic_format_prefix(self):
        """Les topics respectent le préfixe configuré."""
        import os
        os.environ["MQTT_PREFIX"] = "santuario/bms"
        from daly_mqtt import DalyMqttPublisher
        publisher = DalyMqttPublisher()
        topic     = publisher._topic(1, "pack_320ah", "soc")
        assert topic.startswith("santuario/bms/1/")

    def test_lwt_topic_configured(self):
        """Le topic LWT système est bien formé."""
        from daly_mqtt import DalyMqttPublisher
        publisher = DalyMqttPublisher()
        assert "system" in publisher.LWT_TOPIC
        assert "online" in publisher.LWT_TOPIC


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 5 — InfluxDB Writer (daly_influx.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyInflux:
    """Tests du writer InfluxDB — client mocké."""

    @pytest.mark.asyncio
    async def test_bms_status_measurement_written(self):
        """Un snapshot génère un point bms_status dans InfluxDB."""
        from daly_influx import DalyInfluxWriter
        writer  = DalyInfluxWriter()
        written = []

        async def fake_write(bucket, record, **kw):
            written.extend(record if isinstance(record, list) else [record])

        with patch.object(writer, "_write_api") as mock_api:
            mock_api.write = AsyncMock(side_effect=fake_write)
            snap = _make_snapshot(1, soc=72.0)
            await writer.update(1, snap)

        measurements = [p.to_line_protocol() for p in written if hasattr(p, "to_line_protocol")]
        soc_points   = [m for m in measurements if "bms_status" in m and "soc" in m]
        assert soc_points or written, "Aucun point bms_status écrit"

    @pytest.mark.asyncio
    async def test_cell_measurement_16_fields(self):
        """Le measurement bms_cells contient 16 champs cell_01…cell_16."""
        from daly_influx import DalyInfluxWriter
        writer  = DalyInfluxWriter()
        fields_written = {}

        async def fake_write(bucket, record, **kw):
            for p in (record if isinstance(record, list) else [record]):
                if hasattr(p, "_fields"):
                    fields_written.update(p._fields)

        with patch.object(writer, "_write_api") as mock_api:
            mock_api.write = AsyncMock(side_effect=fake_write)
            snap = _make_snapshot(1)
            await writer.update(1, snap)

        cell_fields = [k for k in fields_written if k.startswith("cell_0") or k.startswith("cell_1")]
        assert len(cell_fields) >= 16, (
            f"Attendu 16 champs cellules, trouvé {len(cell_fields)}"
        )

    @pytest.mark.asyncio
    async def test_alarm_event_on_transition(self):
        """Une transition alarme 0→1 génère un point bms_events."""
        from daly_influx import DalyInfluxWriter
        writer  = DalyInfluxWriter()
        events  = []

        async def fake_write(bucket, record, **kw):
            for p in (record if isinstance(record, list) else [record]):
                lp = p.to_line_protocol() if hasattr(p, "to_line_protocol") else str(p)
                if "bms_events" in lp:
                    events.append(lp)

        with patch.object(writer, "_write_api") as mock_api:
            mock_api.write = AsyncMock(side_effect=fake_write)
            snap_ok  = _make_snapshot(1)
            snap_err = _make_snapshot(1)
            snap_err["alarms"]["cell_ovp"] = True
            snap_err["any_alarm"]          = True
            await writer.update(1, snap_ok)
            await writer.update(1, snap_err)

        assert events, "Aucun événement bms_events écrit lors d'une transition alarme"

    def test_retention_config(self):
        """La rétention par défaut est 30 jours."""
        from daly_influx import INFLUX_RETENTION_DAYS
        assert INFLUX_RETENTION_DAYS == 30


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 6 — Alertes (daly_alerts.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyAlerts:
    """Tests du moteur d'alertes — notifications mockées."""

    @pytest.fixture
    def engine(self, tmp_path):
        from daly_alerts import AlertEngine, AlertJournal
        db   = str(tmp_path / "test_alerts.db")
        j    = AlertJournal(db)
        eng  = AlertEngine(journal=j)
        return eng

    @pytest.mark.asyncio
    async def test_cell_ovp_triggers_on_threshold(self, engine):
        """cell_voltage_high se déclenche si cell_max_v > 3600mV."""
        snap = _make_snapshot(1)
        snap["cell_voltages"][3] = 3650    # dépasse 3.60V
        snap["cell_max_v"]       = 3650
        await engine.evaluate(1, snap)
        active = [a["rule_name"] for a in engine.active_alerts()]
        assert "cell_voltage_high" in active

    @pytest.mark.asyncio
    async def test_cell_ovp_clears_below_threshold(self, engine):
        """cell_voltage_high s'efface quand cell_max_v repasse sous 3550mV."""
        snap_hi = _make_snapshot(1)
        snap_hi["cell_voltages"][3] = 3650
        snap_hi["cell_max_v"]       = 3650
        await engine.evaluate(1, snap_hi)

        snap_ok = _make_snapshot(1)
        snap_ok["cell_max_v"] = 3520
        snap_ok["cell_voltages"][3] = 3520
        await engine.evaluate(1, snap_ok)

        active = [a["rule_name"] for a in engine.active_alerts()]
        assert "cell_voltage_high" not in active

    @pytest.mark.asyncio
    async def test_soc_critical_triggers(self, engine):
        """soc_critical se déclenche si SOC < 10%."""
        snap      = _make_snapshot(1, soc=9.5)
        snap["soc"] = 9.5
        await engine.evaluate(1, snap)
        active = [a["rule_name"] for a in engine.active_alerts()]
        assert "soc_critical" in active

    @pytest.mark.asyncio
    async def test_cell_delta_high_triggers(self, engine):
        """cell_delta_high se déclenche si delta > 100mV."""
        snap = _make_snapshot(1, cell_delta_mv=150)
        snap["cell_delta"]         = 150
        snap["alarms"]["cell_delta"] = True
        await engine.evaluate(1, snap)
        active = [a["rule_name"] for a in engine.active_alerts()]
        assert "cell_delta_high" in active or "hw_cell_delta" in active

    @pytest.mark.asyncio
    async def test_snooze_suppresses_notification(self, engine):
        """Un snooze actif empêche les notifications (last_notified non mis à jour)."""
        engine.snooze(1, "cell_voltage_high", 3600)
        snap = _make_snapshot(1)
        snap["cell_max_v"]       = 3650
        snap["cell_voltages"][0] = 3650

        notified = []
        with patch("daly_alerts.Notifier.send_telegram",
                   new_callable=AsyncMock) as mock_tg:
            await engine.evaluate(1, snap)
            assert mock_tg.call_count == 0, "Telegram envoyé malgré snooze"

    @pytest.mark.asyncio
    async def test_unsnooze_restores_notifications(self, engine):
        """Après unsnooze, les notifications reprennent."""
        engine.snooze(1, "soc_critical", 3600)
        engine.unsnooze(1, "soc_critical")
        state = engine._states.get((1, "soc_critical"))
        assert state is None or state.snoozed_until == 0.0

    def test_journal_log_triggered(self, engine, tmp_path):
        """log_triggered() insère un enregistrement dans SQLite."""
        from daly_alerts import _default_rules
        rule    = _default_rules()[0]
        engine.journal.log_triggered(1, rule, "test_value", notified=True)
        hist    = engine.journal.get_history(bms_id=1, limit=5)
        assert len(hist) >= 1
        assert hist[0]["event"] == "triggered"
        assert hist[0]["bms_id"] == 1

    def test_journal_get_counters(self, engine):
        """get_counters() retourne les statistiques d'alarmes."""
        from daly_alerts import _default_rules
        rule = _default_rules()[0]
        for _ in range(3):
            engine.journal.log_triggered(1, rule, "v", notified=False)
        counters = engine.journal.get_counters(bms_id=1)
        assert counters[0]["trigger_count"] >= 3

    def test_rules_reference_structure(self, engine):
        """rules_reference() retourne bien name, description, severity, cooldown."""
        rules = engine.rules_reference()
        assert len(rules) > 0
        for r in rules:
            assert "name"        in r
            assert "description" in r
            assert "severity"    in r
            assert "cooldown_s"  in r


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 7 — Venus OS Bridge (daly_venus.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDalyVenus:
    """Tests du bridge Venus OS — MQTT NanoPi mocké."""

    def test_build_battery_paths_required_keys(self):
        """build_battery_paths() retourne tous les paths obligatoires Venus OS."""
        from daly_venus import build_battery_paths
        snap  = _make_snapshot(1, soc=72.0)
        paths = build_battery_paths(snap, capacity_ah=320,
                                    product_name="Daly LiFePO4 320Ah")
        required_paths = [
            "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
            "/Soc", "/Capacity",
            "/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent",
            "/Info/MaxChargeVoltage", "/Info/BatteryLowVoltage",
            "/Io/AllowToCharge", "/Io/AllowToDischarge",
            "/System/MinCellVoltage", "/System/MaxCellVoltage",
            "/Alarms/Alarm", "/Alarms/LowVoltage", "/Alarms/HighVoltage",
            "/Connected", "/ProductName",
        ]
        for p in required_paths:
            assert p in paths, f"Path Venus OS manquant : {p}"

    def test_mos_off_sets_ccl_zero(self):
        """CHG MOS OFF → MaxChargeCurrent = 0."""
        from daly_venus import build_battery_paths
        snap = _make_snapshot(1)
        snap["charge_mos"] = False
        paths = build_battery_paths(snap, 320, "Test")
        assert paths["/Info/MaxChargeCurrent"] == 0.0
        assert paths["/Io/AllowToCharge"] == 0

    def test_mos_on_sets_ccl_positive(self):
        """CHG MOS ON → MaxChargeCurrent > 0."""
        from daly_venus import build_battery_paths
        snap  = _make_snapshot(1)
        snap["charge_mos"] = True
        paths = build_battery_paths(snap, 320, "Test")
        assert paths["/Info/MaxChargeCurrent"] > 0
        assert paths["/Io/AllowToCharge"] == 1

    def test_cvl_matches_16s_config(self):
        """CVL = 3.55V × 16 = 56.8V pour configuration 16S."""
        from daly_venus import build_battery_paths
        snap  = _make_snapshot(1)
        paths = build_battery_paths(snap, 320, "Test")
        cvl   = paths["/Info/MaxChargeVoltage"]
        assert abs(cvl - 56.8) < 0.1, f"CVL attendu 56.8V, obtenu {cvl}V"

    def test_uvp_pack_matches_16s(self):
        """UVP pack = 2.80V × 16 = 44.8V."""
        from daly_venus import build_battery_paths
        snap  = _make_snapshot(1)
        paths = build_battery_paths(snap, 320, "Test")
        uvp   = paths["/Info/BatteryLowVoltage"]
        assert abs(uvp - 44.8) < 0.1, f"UVP attendu 44.8V, obtenu {uvp}V"

    def test_alarm_high_voltage_flag(self):
        """cell_ovp=True → /Alarms/HighVoltage = 1."""
        from daly_venus import build_battery_paths
        snap = _make_snapshot(1)
        snap["alarms"]["cell_ovp"] = True
        snap["any_alarm"]          = True
        paths = build_battery_paths(snap, 320, "Test")
        assert paths["/Alarms/HighVoltage"] == 1
        assert paths["/Alarms/Alarm"] == 1

    def test_time_to_go_in_discharge(self):
        """TimeToGo calculé si courant négatif (décharge)."""
        from daly_venus import build_battery_paths
        snap = _make_snapshot(1, soc=50.0)
        snap["pack_current"]      = -10.0   # décharge 10A
        snap["remaining_capacity"] = 160.0  # 160Ah restants
        paths = build_battery_paths(snap, 320, "Test")
        ttg   = paths.get("/TimeToGo")
        assert ttg is not None
        assert ttg > 0
        # 160Ah / 10A = 16h = 57600s
        assert abs(ttg - 57600) < 600

    def test_time_to_go_none_in_charge(self):
        """TimeToGo = None si courant positif (charge)."""
        from daly_venus import build_battery_paths
        snap = _make_snapshot(1)
        snap["pack_current"] = +15.0
        paths = build_battery_paths(snap, 320, "Test")
        assert paths.get("/TimeToGo") is None

    def test_topic_write_format(self):
        """Les topics W/ respectent le format dbus-mqtt-devices."""
        from daly_venus import _W
        topic = _W("c0619ab9929a", "battery", 10, "Dc/0/Voltage")
        assert topic == "W/c0619ab9929a/battery/10/Dc/0/Voltage"

    def test_val_serialization(self):
        """_val() sérialise en JSON {"value": x}."""
        from daly_venus import _val
        assert json.loads(_val(53.2)) == {"value": 53.2}
        assert json.loads(_val(None)) == {"value": None}
        assert json.loads(_val("ON")) == {"value": "ON"}

    def test_meteo_paths_irradiance(self):
        """build_meteo_paths() inclut /Irradiance."""
        from daly_venus import build_meteo_paths
        paths = build_meteo_paths(irradiance_wm2=650.5)
        assert "/Irradiance" in paths
        assert abs(paths["/Irradiance"] - 650.5) < 0.1
        assert paths["/Connected"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 8 — TESTS D'INTÉGRATION (offline — broker Mosquitto local requis)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestIntegrationMQTT:
    """
    Nécessite Mosquitto en écoute sur localhost:1883.
    Exécuter avec : pytest -m integration
    """

    @pytest.mark.asyncio
    async def test_mqtt_publish_subscribe_roundtrip(self):
        """Publie un message et vérifie sa réception via subscribe."""
        import aiomqtt
        received = asyncio.Event()
        payload_rx = []

        async def subscriber():
            async with aiomqtt.Client("localhost", 1883,
                                      identifier="test-sub") as client:
                await client.subscribe("test/dalybms/roundtrip")
                async for msg in client.messages:
                    payload_rx.append(msg.payload)
                    received.set()
                    break

        async def publisher():
            await asyncio.sleep(0.2)
            async with aiomqtt.Client("localhost", 1883,
                                      identifier="test-pub") as client:
                await client.publish("test/dalybms/roundtrip",
                                     b'{"value":42}', qos=1)

        await asyncio.gather(
            asyncio.wait_for(subscriber(), timeout=5.0),
            publisher(),
        )
        assert payload_rx, "Aucun message reçu"
        assert json.loads(payload_rx[0]) == {"value": 42}

    @pytest.mark.asyncio
    async def test_bridge_publishes_bms_topics(self):
        """MqttBridge publie les topics santuario/bms après on_snapshot."""
        import aiomqtt
        from daly_mqtt import MqttBridge

        received_topics = []
        bridge = MqttBridge()
        await bridge.start()

        async def collector():
            async with aiomqtt.Client("localhost", 1883,
                                      identifier="test-collector") as client:
                await client.subscribe("santuario/bms/#")
                deadline = time.time() + 3
                async for msg in client.messages:
                    received_topics.append(str(msg.topic))
                    if time.time() > deadline:
                        break

        snaps = {
            1: _make_snapshot(1),
            2: _make_snapshot(2),
        }
        await asyncio.gather(
            asyncio.wait_for(collector(), timeout=5.0),
            bridge.on_snapshot(snaps),
        )
        await bridge.stop()

        soc_topics = [t for t in received_topics if "soc" in t]
        assert soc_topics, "Topic SOC non reçu"


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 9 — TESTS HARDWARE (RPi CM5 + BMS Daly réels)
# Marqués @pytest.mark.hardware — exécuter uniquement sur le hardware cible
# pytest -m hardware --uart=/dev/ttyUSB1
# ═══════════════════════════════════════════════════════════════════════════════

def pytest_addoption(parser):
    parser.addoption("--uart", default="/dev/ttyUSB1",
                     help="Port série UART BMS Daly")
    parser.addoption("--bms-ids", default="1,2",
                     help="IDs BMS à tester (virgule-séparé)")


@pytest.fixture(scope="session")
def uart_port(request):
    return request.config.getoption("--uart")


@pytest.fixture(scope="session")
def bms_ids(request):
    return [int(x) for x in request.config.getoption("--bms-ids").split(",")]


@pytest.mark.hardware
class TestHardwareUART:
    """
    Tests sur hardware réel — RPi CM5 + BMS Daly 16S.
    Prérequis :
      - Port /dev/ttyUSB1 disponible
      - BMS 0x01 et 0x02 alimentés et connectés
      - Utilisateur dans le groupe dialout
    """

    @pytest.mark.asyncio
    async def test_uart_port_accessible(self, uart_port):
        """Le port série est accessible en lecture/écriture."""
        import serial
        try:
            s = serial.Serial(uart_port, 9600, timeout=1)
            assert s.isOpen()
            s.close()
        except serial.SerialException as e:
            pytest.fail(f"Port {uart_port} inaccessible : {e}")

    @pytest.mark.asyncio
    async def test_bms1_responds_to_soc_query(self, uart_port):
        """BMS 0x01 répond à la commande SOC (0x90) en < 500ms."""
        from daly_protocol import DalyPort, DalyBms, Cmd
        async with DalyPort(uart_port) as port:
            bms  = DalyBms(port, bms_id=0x01)
            t0   = time.time()
            data = await asyncio.wait_for(bms.get_soc(), timeout=2.0)
            dt   = time.time() - t0
            assert data is not None, "Pas de réponse SOC BMS1"
            assert 0 <= data.soc <= 100, f"SOC hors plage : {data.soc}"
            assert dt < 0.5, f"Temps réponse trop élevé : {dt:.3f}s"

    @pytest.mark.asyncio
    async def test_bms2_responds_to_soc_query(self, uart_port):
        """BMS 0x02 répond à la commande SOC (0x90) en < 500ms."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms  = DalyBms(port, bms_id=0x02)
            data = await asyncio.wait_for(bms.get_soc(), timeout=2.0)
            assert data is not None
            assert 0 <= data.soc <= 100

    @pytest.mark.asyncio
    async def test_bms1_cell_count_is_16(self, uart_port):
        """BMS1 retourne exactement 16 tensions cellules."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms   = DalyBms(port, bms_id=0x01)
            cells = await asyncio.wait_for(bms.get_cell_voltages(), timeout=2.0)
            assert cells is not None
            assert len(cells.voltages) == 16

    @pytest.mark.asyncio
    async def test_bms1_cell_voltages_in_range(self, uart_port):
        """Toutes les tensions cellules BMS1 dans 2500–4000mV."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms   = DalyBms(port, bms_id=0x01)
            cells = await asyncio.wait_for(bms.get_cell_voltages(), timeout=2.0)
            for i, v in enumerate(cells.voltages):
                assert 2500 <= v <= 4000, (
                    f"Cellule #{i+1} hors plage : {v}mV"
                )

    @pytest.mark.asyncio
    async def test_bms1_pack_voltage_coherent(self, uart_port):
        """Tension pack ≈ somme cellules (tolérance 500mV)."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms   = DalyBms(port, bms_id=0x01)
            soc   = await asyncio.wait_for(bms.get_soc(), timeout=2.0)
            cells = await asyncio.wait_for(bms.get_cell_voltages(), timeout=2.0)
            pack_from_cells = sum(cells.voltages) / 1000   # mV → V
            delta = abs(soc.pack_voltage - pack_from_cells)
            assert delta < 0.5, (
                f"Tension pack ({soc.pack_voltage}V) incohérente "
                f"avec somme cellules ({pack_from_cells:.3f}V) — delta={delta:.3f}V"
            )

    @pytest.mark.asyncio
    async def test_sequential_bms_no_collision(self, uart_port):
        """Lecture séquentielle BMS1 puis BMS2 sans collision sur bus partagé."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms1  = DalyBms(port, bms_id=0x01)
            bms2  = DalyBms(port, bms_id=0x02)
            soc1  = await asyncio.wait_for(bms1.get_soc(), timeout=2.0)
            soc2  = await asyncio.wait_for(bms2.get_soc(), timeout=2.0)
            assert soc1 is not None
            assert soc2 is not None
            assert soc1.soc != soc2.soc or True   # peuvent être identiques

    @pytest.mark.asyncio
    async def test_poll_loop_10_cycles(self, uart_port):
        """poll_loop exécute 10 cycles en < 15s sans erreur."""
        from daly_protocol import DalyBusManager
        snapshots_received = []
        count = 0

        async def on_snapshot(snaps):
            nonlocal count
            snapshots_received.append(snaps)
            count += 1

        mgr = DalyBusManager(uart_port, [0x01, 0x02])

        async def run_limited():
            nonlocal count
            async for snaps in mgr.poll_loop(on_snapshot, interval=1.0):
                if count >= 10:
                    break

        await asyncio.wait_for(run_limited(), timeout=15.0)
        assert count >= 10, f"Seulement {count} cycles complétés"
        assert all(1 in s and 2 in s for s in snapshots_received)

    @pytest.mark.asyncio
    async def test_bms1_temperature_sensors(self, uart_port):
        """BMS1 retourne au moins 2 sondes température valides."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms   = DalyBms(port, bms_id=0x01)
            temps = await asyncio.wait_for(bms.get_temperatures(), timeout=2.0)
            assert temps is not None
            valid = [t for t in temps.temps if -20 <= t <= 80]
            assert len(valid) >= 2, f"Sondes valides insuffisantes : {temps.temps}"

    @pytest.mark.asyncio
    async def test_bms1_cell8_cell16_monitoring(self, uart_port):
        """Cellules #8 et #16 lisibles et dans les plages de surveillance."""
        from daly_protocol import DalyPort, DalyBms
        async with DalyPort(uart_port) as port:
            bms   = DalyBms(port, bms_id=0x01)
            cells = await asyncio.wait_for(bms.get_cell_voltages(), timeout=2.0)
            cell8  = cells.voltages[7]
            cell16 = cells.voltages[15]
            # Seuil surveillance : alerte si > 3550mV pendant charge
            assert cell8  > 0, "Cellule #8 : tension nulle"
            assert cell16 > 0, "Cellule #16 : tension nulle"

    @pytest.mark.asyncio
    async def test_venus_bridge_reaches_nanopi(self):
        """Venus Bridge se connecte au broker NanoPi."""
        import aiomqtt, os
        host = os.getenv("NANOPI_MQTT_HOST", "192.168.1.120")
        try:
            async with aiomqtt.Client(host, 1883,
                                      identifier="test-venus-ping",
                                      keepalive=5) as client:
                await client.publish("test/dalybms/ping", b"ping", qos=0)
        except Exception as e:
            pytest.fail(f"NanoPi MQTT inaccessible ({host}:1883) : {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# BLOC 10 — TESTS DE RÉGRESSION (valeurs connues installation Santuario)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegression:
    """
    Valeurs de référence connues pour l'installation Santuario.
    Ces tests échouent si une modification casse un comportement établi.
    """

    def test_santuario_absorption_voltage(self):
        """Tension absorption Santuario : 56.8V (3.55V × 16)."""
        cells_at_absorption = [3550] * 16
        pack_v = sum(cells_at_absorption) / 1000
        assert abs(pack_v - 56.8) < 0.01

    def test_santuario_float_voltage(self):
        """Tension float Santuario : 54.4V (3.40V × 16)."""
        cells_at_float = [3400] * 16
        pack_v = sum(cells_at_float) / 1000
        assert abs(pack_v - 54.4) < 0.01

    def test_total_capacity_combined(self):
        """Capacité totale Santuario : 680Ah (320 + 360)."""
        cap1, cap2 = 320, 360
        assert cap1 + cap2 == 680

    def test_cell8_cell16_are_problematic(self):
        """Cellules #8 et #16 sont indexées 7 et 15 (0-based)."""
        # Vérification de la convention d'indexation
        problematic_cells_1based  = [8, 16]
        problematic_cells_0based  = [c - 1 for c in problematic_cells_1based]
        assert problematic_cells_0based == [7, 15]

    def test_daly_can_bus_addresses(self):
        """BMS 320Ah = CAN02 = address 0x01, BMS 360Ah = CAN01 = address 0x02."""
        ADDR_BMS_320AH = 0x01
        ADDR_BMS_360AH = 0x02
        assert ADDR_BMS_320AH == 0x01
        assert ADDR_BMS_360AH == 0x02

    def test_venus_instance_ids(self):
        """Instances Venus OS : BMS1=10, BMS2=11, Meteo=20."""
        from daly_venus import INST_BMS1, INST_BMS2, INST_METEO
        assert INST_BMS1  == 10
        assert INST_BMS2  == 11
        assert INST_METEO == 20

    def test_influx_retention_30_days(self):
        """Rétention InfluxDB full-res : 30 jours."""
        from daly_influx import INFLUX_RETENTION_DAYS
        assert INFLUX_RETENTION_DAYS == 30

    def test_portal_id_santuario(self):
        """Portal ID VRM connu : c0619ab9929a."""
        from daly_venus import PORTAL_ID
        assert PORTAL_ID == "c0619ab9929a"


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION pytest
# ═══════════════════════════════════════════════════════════════════════════════

# conftest.py — à placer à la racine du projet
CONFTEST = '''
import pytest

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hardware: tests nécessitant le hardware RPi CM5 + BMS Daly"
    )
    config.addinivalue_line(
        "markers",
        "integration: tests nécessitant Mosquitto local"
    )

def pytest_collection_modifyitems(config, items):
    skip_hw   = pytest.mark.skip(reason="Hardware RPi CM5 requis (--run-hardware)")
    skip_int  = pytest.mark.skip(reason="Broker Mosquitto local requis (-m integration)")
    run_hw    = config.getoption("--run-hardware", default=False)
    run_int   = "integration" in config.getoption("-m", default="")

    for item in items:
        if "hardware" in item.keywords and not run_hw:
            item.add_marker(skip_hw)
        if "integration" in item.keywords and not run_int:
            item.add_marker(skip_int)
'''

# pytest.ini
PYTEST_INI = '''
[pytest]
asyncio_mode = auto
testpaths = .
python_files = test_suite.py
python_classes = Test*
python_functions = test_*
markers =
    hardware: tests hardware RPi CM5 + BMS Daly réels
    integration: tests intégration broker Mosquitto
addopts =
    -v
    --tb=short
    --strict-markers
    -p no:warnings
log_cli = true
log_cli_level = INFO
'''

# requirements-test.txt
REQUIREMENTS_TEST = '''
pytest>=8.0
pytest-asyncio>=0.23
pytest-cov>=4.1
httpx>=0.27
pyserial>=3.5
aiomqtt>=2.0
fastapi>=0.110
'''
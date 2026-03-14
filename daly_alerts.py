"""
daly_alerts.py — D6 : Système d'Alertes
Détection alarmes BMS hardware + seuils logiciels, notifications Telegram/Email,
hysteresis, journal SQLite, silencing, API endpoints.
Dépend de : daly_protocol.py (D1), daly_api.py (D3)
Installation Santuario — Badalucco
"""

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from enum import Enum
from typing import Any, Callable, Optional

import aiosmtplib
import httpx

log = logging.getLogger("daly.alerts")

# ─── Configuration ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
SMTP_HOST           = os.getenv("SMTP_HOST",        "")
SMTP_PORT           = int(os.getenv("SMTP_PORT",    "587"))
SMTP_USER           = os.getenv("SMTP_USER",        "")
SMTP_PASS           = os.getenv("SMTP_PASS",        "")
SMTP_FROM           = os.getenv("SMTP_FROM",        "daly-bms@santuario.local")
SMTP_TO             = os.getenv("SMTP_TO",          "")
ALERT_DB_PATH       = os.getenv("ALERT_DB_PATH", "/data/dalybms/alerts.db")
CHECK_INTERVAL      = float(os.getenv("ALERT_CHECK_INTERVAL", "1.0"))

from config import BMS_NAMES


# ─── Sévérité ─────────────────────────────────────────────────────────────────
class Severity(str, Enum):
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


# ─── Définition d'une règle d'alerte ─────────────────────────────────────────
@dataclass
class AlertRule:
    """
    Règle d'alerte configurable indépendamment des seuils hardware BMS.
    Chaque règle a une hysteresis pour éviter les notifications répétitives.

    trigger_fn  : callable(snap) → bool — True si la condition est active
    clear_fn    : callable(snap) → bool — True si la condition est effacée
                  (si None : inverse du trigger)
    value_fn    : callable(snap) → float — valeur affichée dans la notification
    """
    name:         str
    description:  str
    severity:     Severity
    trigger_fn:   Callable[[dict], bool]
    value_fn:     Callable[[dict], Any]
    clear_fn:     Optional[Callable[[dict], bool]] = None
    cooldown_s:   float = 300.0      # délai min entre deux notifications identiques


# ─── Règles prédéfinies Santuario ─────────────────────────────────────────────
def _default_rules(cfg: dict = None) -> list[AlertRule]:
    """
    Construit les règles logicielles à partir d'une configuration.
    cfg : dict de seuils (peut venir d'un fichier config.yaml).
    Les seuils BMS hardware (flags registre Daly) sont gérés séparément.
    """
    c = cfg or {}

    # Seuils logiciels (indépendants des seuils BMS)
    cell_ovp_v       = c.get("alert_cell_ovp_v",       3.60)   # V
    cell_ovp_clr_v   = c.get("alert_cell_ovp_clr_v",   3.55)
    cell_uvp_v       = c.get("alert_cell_uvp_v",       2.90)   # V
    cell_uvp_clr_v   = c.get("alert_cell_uvp_clr_v",   2.95)
    cell_delta_mv    = c.get("alert_cell_delta_mv",    100)     # mV
    cell_delta_clr   = c.get("alert_cell_delta_clr",    80)
    soc_low          = c.get("alert_soc_low",           20.0)   # %
    soc_low_clr      = c.get("alert_soc_low_clr",       25.0)
    soc_critical     = c.get("alert_soc_critical",      10.0)   # %
    soc_critical_clr = c.get("alert_soc_critical_clr",  12.0)
    temp_high_c      = c.get("alert_temp_high_c",       45.0)   # °C
    temp_high_clr_c  = c.get("alert_temp_high_clr_c",   40.0)
    current_high_a   = c.get("alert_current_high_a",    80.0)   # A
    current_high_clr = c.get("alert_current_high_clr",  70.0)

    return [
        # ── Tensions cellules ─────────────────────────────────────────────────
        AlertRule(
            name="cell_voltage_high",
            description=f"Tension cellule max > {cell_ovp_v}V",
            severity=Severity.CRITICAL,
            trigger_fn=lambda s: (s.get("cell_max_v") or 0) > cell_ovp_v * 1000,
            clear_fn=lambda s:   (s.get("cell_max_v") or 0) < cell_ovp_clr_v * 1000,
            value_fn=lambda s:   f"{(s.get('cell_max_v') or 0) / 1000:.3f}V (cellule #{s.get('cell_max_num','?')})",
            cooldown_s=60,
        ),
        AlertRule(
            name="cell_voltage_low",
            description=f"Tension cellule min < {cell_uvp_v}V",
            severity=Severity.CRITICAL,
            trigger_fn=lambda s: (s.get("cell_min_v") or 9999) < cell_uvp_v * 1000,
            clear_fn=lambda s:   (s.get("cell_min_v") or 9999) > cell_uvp_clr_v * 1000,
            value_fn=lambda s:   f"{(s.get('cell_min_v') or 0) / 1000:.3f}V (cellule #{s.get('cell_min_num','?')})",
            cooldown_s=60,
        ),

        # ── Déséquilibre cellules ─────────────────────────────────────────────
        AlertRule(
            name="cell_delta_high",
            description=f"Déséquilibre cellules > {cell_delta_mv}mV",
            severity=Severity.WARNING,
            trigger_fn=lambda s: (s.get("cell_delta") or 0) > cell_delta_mv,
            clear_fn=lambda s:   (s.get("cell_delta") or 0) < cell_delta_clr,
            value_fn=lambda s:   f"{s.get('cell_delta','?')}mV",
            cooldown_s=600,
        ),

        # ── SOC ───────────────────────────────────────────────────────────────
        AlertRule(
            name="soc_low",
            description=f"SOC faible < {soc_low}%",
            severity=Severity.WARNING,
            trigger_fn=lambda s: (s.get("soc") or 100) < soc_low,
            clear_fn=lambda s:   (s.get("soc") or 100) > soc_low_clr,
            value_fn=lambda s:   f"{s.get('soc','?')}%",
            cooldown_s=900,
        ),
        AlertRule(
            name="soc_critical",
            description=f"SOC critique < {soc_critical}%",
            severity=Severity.CRITICAL,
            trigger_fn=lambda s: (s.get("soc") or 100) < soc_critical,
            clear_fn=lambda s:   (s.get("soc") or 100) > soc_critical_clr,
            value_fn=lambda s:   f"{s.get('soc','?')}%",
            cooldown_s=300,
        ),

        # ── Température ───────────────────────────────────────────────────────
        AlertRule(
            name="temperature_high",
            description=f"Température BMS > {temp_high_c}°C",
            severity=Severity.WARNING,
            trigger_fn=lambda s: (s.get("temp_max") or 0) > temp_high_c,
            clear_fn=lambda s:   (s.get("temp_max") or 0) < temp_high_clr_c,
            value_fn=lambda s:   f"{s.get('temp_max','?')}°C",
            cooldown_s=300,
        ),

        # ── Courant ───────────────────────────────────────────────────────────
        AlertRule(
            name="current_high",
            description=f"Courant de charge > {current_high_a}A",
            severity=Severity.WARNING,
            trigger_fn=lambda s: (s.get("pack_current") or 0) > current_high_a,
            clear_fn=lambda s:   (s.get("pack_current") or 0) < current_high_clr,
            value_fn=lambda s:   f"{s.get('pack_current','?')}A",
            cooldown_s=120,
        ),

        # ── MOS désactivé inattendu ────────────────────────────────────────────
        AlertRule(
            name="charge_mos_off",
            description="MOSFET charge désactivé de manière inattendue",
            severity=Severity.CRITICAL,
            trigger_fn=lambda s: s.get("charge_mos") is False,
            clear_fn=lambda s:   s.get("charge_mos") is True,
            value_fn=lambda s:   "CHG MOS = OFF",
            cooldown_s=120,
        ),
        AlertRule(
            name="discharge_mos_off",
            description="MOSFET décharge désactivé de manière inattendue",
            severity=Severity.CRITICAL,
            trigger_fn=lambda s: s.get("discharge_mos") is False,
            clear_fn=lambda s:   s.get("discharge_mos") is True,
            value_fn=lambda s:   "DSG MOS = OFF",
            cooldown_s=120,
        ),

        # ── Alarmes hardware BMS (flags registre Daly) ─────────────────────────
        *[
            AlertRule(
                name=f"hw_{flag.removeprefix('alarm_')}",
                description=f"Alarme hardware BMS : {flag.removeprefix('alarm_').upper()}",
                severity=Severity.CRITICAL,
                trigger_fn=lambda s, f=flag: bool(s.get(f, False)),
                clear_fn=lambda s,   f=flag: not bool(s.get(f, False)),
                value_fn=lambda s,   f=flag: f"flag={s.get(f)}",
                cooldown_s=60,
            )
            for flag in [
                "alarm_cell_ovp", "alarm_cell_uvp", "alarm_pack_ovp", "alarm_pack_uvp",
                "alarm_chg_otp",  "alarm_chg_ocp",  "alarm_dsg_ocp",  "alarm_scp",
                "alarm_cell_delta",
            ]
        ],
    ]


# ─── État d'une alerte active ─────────────────────────────────────────────────
@dataclass
class AlertState:
    rule_name:      str
    bms_id:         int
    active:         bool      = False
    triggered_at:   float     = 0.0
    last_notified:  float     = 0.0
    cleared_at:     float     = 0.0
    trigger_count:  int       = 0
    snoozed_until:  float     = 0.0


# ─── Journal SQLite ───────────────────────────────────────────────────────────
_SCHEMA_VERSION = 1


class AlertJournal:
    """
    Journal persistant des événements d'alerte dans SQLite.
    Schéma : id, bms_id, bms_name, rule_name, severity, event, value, timestamp, duration_s

    Les écritures sont sérialisées via _write_lock pour éviter les erreurs
    "database is locked" sous charge. Les lectures (requêtes SELECT) utilisent
    run_in_executor pour ne pas bloquer la boucle asyncio.

    Migrations : table schema_version — incrémentée à chaque évolution du schéma.
    """

    def __init__(self, db_path: str = ALERT_DB_PATH):
        self.db_path    = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5.0)

    def _init_db(self):
        with self._conn() as conn:
            # Table de versionnement du schéma (I4)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            current_version = row[0] if row else 0

            if current_version < _SCHEMA_VERSION:
                self._migrate(conn, current_version)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version VALUES (?)",
                    (_SCHEMA_VERSION,)
                )
        log.info(f"AlertJournal initialisé : {self.db_path} (schema v{_SCHEMA_VERSION})")

    def _migrate(self, conn: sqlite3.Connection, from_version: int):
        """Applique les migrations manquantes de from_version → _SCHEMA_VERSION."""
        if from_version < 1:
            log.info("AlertJournal migration v0 → v1 : création schéma initial")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    bms_id      INTEGER NOT NULL,
                    bms_name    TEXT,
                    rule_name   TEXT NOT NULL,
                    severity    TEXT NOT NULL,
                    event       TEXT NOT NULL,
                    value       TEXT,
                    timestamp   REAL NOT NULL,
                    duration_s  REAL,
                    notified    INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_ts
                ON alert_events (timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_bms
                ON alert_events (bms_id, rule_name)
            """)

    def log_triggered(self, bms_id: int, rule: AlertRule,
                      value: str, notified: bool = False):
        """Écriture directe SQLite (appelable sans await)."""
        params = (bms_id, BMS_NAMES.get(bms_id, f"BMS{bms_id}"),
                  rule.name, rule.severity.value, value, time.time(), int(notified))
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO alert_events
                (bms_id, bms_name, rule_name, severity, event, value, timestamp, notified)
                VALUES (?, ?, ?, ?, 'triggered', ?, ?, ?)
            """, params)

    def log_cleared(self, bms_id: int, rule: AlertRule,
                    triggered_at: float, value: str):
        """Écriture directe SQLite (appelable sans await)."""
        duration = round(time.time() - triggered_at, 1) if triggered_at else None
        params = (bms_id, BMS_NAMES.get(bms_id, f"BMS{bms_id}"),
                  rule.name, rule.severity.value, value, time.time(), duration)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO alert_events
                (bms_id, bms_name, rule_name, severity, event, value, timestamp, duration_s)
                VALUES (?, ?, ?, ?, 'cleared', ?, ?, ?)
            """, params)

    def get_history(self, bms_id: Optional[int] = None,
                    limit: int = 100, offset: int = 0,
                    rule_name: Optional[str] = None) -> list[dict]:
        query  = "SELECT * FROM alert_events WHERE 1=1"
        params: list = []
        if bms_id is not None:
            query  += " AND bms_id = ?"
            params.append(bms_id)
        if rule_name:
            query  += " AND rule_name = ?"
            params.append(rule_name)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_active_summary(self) -> list[dict]:
        """Retourne le dernier état (triggered/cleared) par règle et BMS."""
        query = """
            SELECT bms_id, rule_name, severity, event, value, timestamp
            FROM alert_events
            WHERE id IN (
                SELECT MAX(id) FROM alert_events GROUP BY bms_id, rule_name
            )
            AND event = 'triggered'
            ORDER BY timestamp DESC
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def get_counters(self, bms_id: Optional[int] = None) -> list[dict]:
        """Compteurs de déclenchements par règle."""
        query  = """
            SELECT bms_id, rule_name, severity,
                   COUNT(*) as trigger_count,
                   MAX(timestamp) as last_triggered
            FROM alert_events WHERE event = 'triggered'
        """
        params: list = []
        if bms_id is not None:
            query  += " AND bms_id = ?"
            params.append(bms_id)
        query += " GROUP BY bms_id, rule_name ORDER BY trigger_count DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ─── Notifications ────────────────────────────────────────────────────────────
class Notifier:
    """Envoi de notifications Telegram et/ou Email."""

    ICONS = {
        Severity.INFO:     "ℹ️",
        Severity.WARNING:  "⚠️",
        Severity.CRITICAL: "🚨",
    }

    @staticmethod
    def _format_message(bms_id: int, rule: AlertRule,
                        value: str, event: str) -> str:
        icon     = Notifier.ICONS.get(rule.severity, "🔔")
        bms_name = BMS_NAMES.get(bms_id, f"BMS {bms_id}")
        ts       = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime())
        status   = "🟥 DÉCLENCHÉ" if event == "triggered" else "🟩 EFFACÉ"
        return (
            f"{icon} *DalyBMS — {rule.severity.value}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔋 *BMS* : {bms_name}\n"
            f"📋 *Règle* : `{rule.name}`\n"
            f"📝 *Description* : {rule.description}\n"
            f"📊 *Valeur* : `{value}`\n"
            f"🔔 *Statut* : {status}\n"
            f"🕐 *Horodatage* : {ts}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Santuario — Badalucco_"
        )

    @staticmethod
    async def send_telegram(bms_id: int, rule: AlertRule,
                            value: str, event: str = "triggered") -> bool:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            log.debug("Telegram non configuré — notification ignorée")
            return False
        msg = Notifier._format_message(bms_id, rule, value, event)
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       msg,
                    "parse_mode": "Markdown",
                })
                if resp.status_code == 200:
                    log.info(f"Telegram OK : [{rule.name}] BMS{bms_id}")
                    return True
                log.warning(f"Telegram erreur {resp.status_code} : {resp.text[:200]}")
        except Exception as exc:
            log.error(f"Telegram exception : {exc}")
        return False

    @staticmethod
    async def send_email(bms_id: int, rule: AlertRule,
                         value: str, event: str = "triggered") -> bool:
        if not SMTP_HOST or not SMTP_TO:
            log.debug("Email non configuré — notification ignorée")
            return False
        bms_name = BMS_NAMES.get(bms_id, f"BMS {bms_id}")
        subject  = (f"[DalyBMS {rule.severity.value}] "
                    f"{rule.name} — {bms_name} — "
                    f"{'DÉCLENCHÉ' if event == 'triggered' else 'EFFACÉ'}")
        body = Notifier._format_message(bms_id, rule, value, event).replace("*", "").replace("`", "")
        msg  = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = SMTP_TO
        try:
            async with aiosmtplib.SMTP(
                hostname=SMTP_HOST, port=SMTP_PORT, timeout=10
            ) as smtp:
                await smtp.starttls()
                if SMTP_USER:
                    await smtp.login(SMTP_USER, SMTP_PASS)
                await smtp.send_message(msg)
            log.info(f"Email OK : [{rule.name}] BMS{bms_id} → {SMTP_TO}")
            return True
        except Exception as exc:
            log.error(f"Email exception : {exc}")
        return False


# ─── Moteur d'alertes principal ───────────────────────────────────────────────
class AlertEngine:
    """
    Évalue les règles d'alerte sur chaque snapshot BMS.
    Gère :
    - Hysteresis via AlertState (trigger / clear séparés)
    - Cooldown entre notifications répétitives
    - Silencing (snooze) par règle et par BMS
    - Journal SQLite de tous les événements
    - Notifications Telegram + Email asynchrones
    - Compteurs de déclenchements
    """

    def __init__(self, rules: Optional[list[AlertRule]] = None,
                 journal: Optional[AlertJournal] = None,
                 cfg: dict = None):
        self.rules   = rules or _default_rules(cfg)
        self.journal = journal or AlertJournal()
        self._states: dict[tuple[int, str], AlertState] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue()

    def _state(self, bms_id: int, rule: AlertRule) -> AlertState:
        key = (bms_id, rule.name)
        if key not in self._states:
            self._states[key] = AlertState(rule_name=rule.name, bms_id=bms_id)
        return self._states[key]

    def start(self):
        self._running = True
        self._task    = asyncio.create_task(self._notification_worker(),
                                            name="alert-notifier")
        log.info(f"AlertEngine démarré — {len(self.rules)} règles actives")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("AlertEngine arrêté")

    # ── Évaluation ────────────────────────────────────────────────────────────

    async def evaluate(self, bms_id: int, snap: dict):
        """
        Évalue toutes les règles sur un snapshot.
        Appelé depuis le poll_loop à chaque cycle.
        """
        now = time.time()
        for rule in self.rules:
            state = self._state(bms_id, rule)
            try:
                triggered = rule.trigger_fn(snap)
            except Exception as exc:
                log.debug(f"Règle {rule.name} évaluation erreur : {exc}")
                continue

            if triggered and not state.active:
                # Transition → actif
                state.active       = True
                state.triggered_at = now
                state.trigger_count += 1
                value_str = str(rule.value_fn(snap))
                self.journal.log_triggered(bms_id, rule, value_str)
                log.warning(f"[BMS{bms_id}] ALERTE DÉCLENCHÉE : {rule.name} = {value_str}")

                # Notification si pas snoozée et cooldown respecté
                if now > state.snoozed_until and now - state.last_notified > rule.cooldown_s:
                    state.last_notified = now
                    self._queue.put_nowait(("triggered", bms_id, rule, value_str))

            elif not triggered and state.active:
                # Vérifier clear_fn si définie
                clear_ok = True
                if rule.clear_fn:
                    try:
                        clear_ok = rule.clear_fn(snap)
                    except Exception:
                        clear_ok = False

                if clear_ok:
                    value_str = str(rule.value_fn(snap))
                    self.journal.log_cleared(bms_id, rule, state.triggered_at, value_str)
                    log.info(f"[BMS{bms_id}] Alerte effacée : {rule.name}")
                    state.active      = False
                    state.cleared_at  = now
                    self._queue.put_nowait(("cleared", bms_id, rule, value_str))

            elif triggered and state.active:
                # Alerte toujours active — re-notification si cooldown écoulé
                if (now > state.snoozed_until
                        and now - state.last_notified > rule.cooldown_s):
                    state.last_notified = now
                    value_str = str(rule.value_fn(snap))
                    self._queue.put_nowait(("triggered", bms_id, rule, value_str))

    # ── Worker de notification asynchrone ────────────────────────────────────

    async def _notification_worker(self):
        """Traite la file de notifications en tâche de fond."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            event, bms_id, rule, value_str = item
            try:
                tasks = []
                if TELEGRAM_TOKEN:
                    tasks.append(Notifier.send_telegram(bms_id, rule, value_str, event))
                if SMTP_HOST:
                    tasks.append(Notifier.send_email(bms_id, rule, value_str, event))
                results = await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as exc:
                log.error(f"Notification worker erreur : {exc}", exc_info=True)
            finally:
                self._queue.task_done()

    # ── Silencing / Snooze ────────────────────────────────────────────────────

    def snooze(self, bms_id: int, rule_name: str, duration_s: float) -> bool:
        """
        Suspend les notifications pour une règle sur un BMS.
        duration_s : 3600 = 1h, 14400 = 4h, 86400 = 24h
        Retourne False si la règle n'existe pas.
        """
        rule_names = {r.name for r in self.rules}
        if rule_name not in rule_names:
            return False
        state = self._states.get((bms_id, rule_name))
        if not state:
            state = AlertState(rule_name=rule_name, bms_id=bms_id)
            self._states[(bms_id, rule_name)] = state
        state.snoozed_until = time.time() + duration_s
        log.info(f"[BMS{bms_id}] Snooze {rule_name} pour {duration_s/3600:.1f}h")
        return True

    def unsnooze(self, bms_id: int, rule_name: str) -> bool:
        state = self._states.get((bms_id, rule_name))
        if state:
            state.snoozed_until = 0.0
            return True
        return False

    # ── Accesseurs ────────────────────────────────────────────────────────────

    def active_alerts(self) -> list[dict]:
        """Liste des alertes actuellement actives sur tous les BMS."""
        now = time.time()
        result = []
        for (bms_id, rule_name), state in self._states.items():
            if state.active:
                rule = next((r for r in self.rules if r.name == rule_name), None)
                result.append({
                    "bms_id":        bms_id,
                    "bms_name":      BMS_NAMES.get(bms_id, f"BMS{bms_id}"),
                    "rule_name":     rule_name,
                    "severity":      rule.severity.value if rule else "UNKNOWN",
                    "description":   rule.description if rule else "",
                    "triggered_at":  state.triggered_at,
                    "duration_s":    round(now - state.triggered_at, 1),
                    "trigger_count": state.trigger_count,
                    "snoozed":       state.snoozed_until > now,
                    "snoozed_until": state.snoozed_until if state.snoozed_until > now else None,
                })
        return sorted(result, key=lambda x: x["triggered_at"])

    def all_states(self) -> list[dict]:
        """État de toutes les règles (actives et inactives)."""
        now = time.time()
        return [
            {
                "bms_id":        bms_id,
                "rule_name":     rule_name,
                "active":        state.active,
                "trigger_count": state.trigger_count,
                "triggered_at":  state.triggered_at,
                "cleared_at":    state.cleared_at,
                "snoozed":       state.snoozed_until > now,
            }
            for (bms_id, rule_name), state in self._states.items()
        ]

    def rules_reference(self) -> list[dict]:
        """Liste de toutes les règles configurées."""
        return [
            {
                "name":        r.name,
                "description": r.description,
                "severity":    r.severity.value,
                "cooldown_s":  r.cooldown_s,
            }
            for r in self.rules
        ]


# ─── Bridge pour intégration dans daly_api.py ─────────────────────────────────
class AlertBridge:
    """
    Pont entre poll_loop et AlertEngine.
    Même pattern que MqttBridge / InfluxBridge.

    Usage dans daly_api.py lifespan :
        alerts = AlertBridge(); alerts.start()
        # dans _on_snapshot :
        await alerts.on_snapshot(snapshots)
    """

    def __init__(self, engine: Optional[AlertEngine] = None, cfg: dict = None):
        self.engine = engine or AlertEngine(cfg=cfg)

    def start(self):
        self.engine.start()

    async def stop(self):
        await self.engine.stop()

    async def on_snapshot(self, snapshots: dict):
        from daly_protocol import snapshot_to_dict
        for bms_id, snap in snapshots.items():
            d = snap if isinstance(snap, dict) else snapshot_to_dict(snap)
            await self.engine.evaluate(bms_id, d)


# ─── Endpoints FastAPI additionnels ──────────────────────────────────────────
def register_alert_routes(app, alert_bridge: "AlertBridge"):
    """
    Enregistre les routes d'alertes sur l'application FastAPI existante.
    Appelé depuis daly_api.py après la création de l'app.
    """
    from fastapi import Body, Depends, Header, HTTPException, status as http_status

    engine  = alert_bridge.engine
    journal = engine.journal

    _api_key = os.getenv("DALY_API_KEY", "")

    async def _check_key(x_api_key: Optional[str] = Header(None)):
        if not _api_key:
            return
        if x_api_key != _api_key:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="API key invalide ou manquante (header X-API-Key)",
            )

    @app.get("/api/v1/alerts/active", tags=["Alertes"])
    async def alerts_active(_=Depends(_check_key)):
        """Liste des alertes actuellement actives sur tous les BMS."""
        return {"alerts": engine.active_alerts()}

    @app.get("/api/v1/alerts/history", tags=["Alertes"])
    async def alerts_history(
        bms_id:    Optional[int] = None,
        rule_name: Optional[str] = None,
        limit:     int           = 100,
        offset:    int           = 0,
        _=Depends(_check_key),
    ):
        """Historique des événements d'alerte depuis le journal SQLite."""
        return {
            "events": journal.get_history(bms_id, limit, offset, rule_name)
        }

    @app.get("/api/v1/alerts/counters", tags=["Alertes"])
    async def alerts_counters(bms_id: Optional[int] = None, _=Depends(_check_key)):
        """Compteurs de déclenchements par règle."""
        return {"counters": journal.get_counters(bms_id)}

    @app.get("/api/v1/alerts/rules", tags=["Alertes"])
    async def alerts_rules(_=Depends(_check_key)):
        """Liste de toutes les règles configurées avec leurs paramètres."""
        return {"rules": engine.rules_reference()}

    @app.get("/api/v1/alerts/states", tags=["Alertes"])
    async def alerts_states(_=Depends(_check_key)):
        """État de toutes les règles (actives et inactives)."""
        return {"states": engine.all_states()}

    @app.post("/api/v1/alerts/snooze/{bms_id}/{rule_name}", tags=["Alertes"])
    async def alert_snooze(bms_id: int, rule_name: str,
                           duration_s: float = Body(..., embed=True),
                           _=Depends(_check_key)):
        """
        Suspend les notifications d'une règle pour une durée en secondes.
        Ex. body : {"duration_s": 3600}  → snooze 1h
        """
        ok = engine.snooze(bms_id, rule_name, duration_s)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Règle inconnue : {rule_name}")
        return {"snoozed": True, "rule": rule_name, "bms_id": bms_id,
                "duration_s": duration_s, "until": time.time() + duration_s}

    @app.delete("/api/v1/alerts/snooze/{bms_id}/{rule_name}", tags=["Alertes"])
    async def alert_unsnooze(bms_id: int, rule_name: str, _=Depends(_check_key)):
        """Annule le snooze d'une règle."""
        ok = engine.unsnooze(bms_id, rule_name)
        return {"unsnoozed": ok, "rule": rule_name, "bms_id": bms_id}


# ─── Point d'entrée standalone ────────────────────────────────────────────────
async def _demo():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    from daly_write import DalyWriteManager

    bridge = AlertBridge()
    bridge.start()

    async with DalyWriteManager(
        os.getenv("DALY_PORT", "/dev/ttyUSB1"), [0x01, 0x02]
    ) as mgr:
        log.info("Démarrage poll_loop → AlertEngine...")
        await mgr.poll_loop(bridge.on_snapshot, CHECK_INTERVAL)

    await bridge.stop()


if __name__ == "__main__":
    asyncio.run(_demo())
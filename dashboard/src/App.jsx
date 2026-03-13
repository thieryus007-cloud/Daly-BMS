import { useState, useEffect, useCallback, useRef, useMemo } from "react";

// ─── Palette & constantes ─────────────────────────────────────────────────────
const C = {
  primary:    "#1d6fa4",
  primaryDk:  "#0d3f5e",
  primaryLt:  "#2196F3",
  accent:     "#00e5ff",
  green:      "#00c853",
  orange:     "#ff6d00",
  red:        "#f44336",
  yellow:     "#ffd600",
  bg:         "#080f1a",
  bgCard:     "#0d1929",
  bgPanel:    "#111e30",
  border:     "#1a2d45",
  text:       "#e8f4ff",
  textMuted:  "#5a7a9a",
  textDim:    "#2a4a6a",
};

// ─── Noms BMS ────────────────────────────────────────────────────────────────
const BMS_NAMES = { 1: "Pack 320Ah", 2: "Pack 360Ah" };

// ─── Mock data (état initial avant connexion WS) ──────────────────────────────
function generateSnapshot(bmsId, t = Date.now()) {
  const base = bmsId === 1 ? 320 : 360;
  const soc  = 72 + Math.sin(t / 30000) * 8;
  const cells = Array.from({ length: 16 }, (_, i) => {
    const v = 3310 + Math.sin(t / 5000 + i) * 15
      + (i === 7 ? 45 : 0) + (i === 15 ? 38 : 0);
    return Math.round(v);
  });
  return {
    bms_id:         bmsId,
    bms_name:       BMS_NAMES[bmsId],
    soc:            +soc.toFixed(1),
    pack_voltage:   +(cells.reduce((a, b) => a + b, 0) / 1000).toFixed(2),
    pack_current:   +(12 + Math.sin(t / 8000) * 5).toFixed(1),
    power:          +(Math.abs(12 + Math.sin(t / 8000) * 5) * 53.2).toFixed(0),
    cell_voltages:  cells,
    cell_min_v:     Math.min(...cells),
    cell_min_num:   cells.indexOf(Math.min(...cells)) + 1,
    cell_max_v:     Math.max(...cells),
    cell_max_num:   cells.indexOf(Math.max(...cells)) + 1,
    cell_avg:       +(cells.reduce((a,b)=>a+b,0)/cells.length).toFixed(1),
    cell_delta:     Math.max(...cells) - Math.min(...cells),
    temperatures:   [28.5, 29.1, 27.8, 28.3],
    temp_max:       29.1,
    temp_min:       27.8,
    charge_mos:     true,
    discharge_mos:  true,
    bms_cycles:     bmsId === 1 ? 147 : 89,
    remaining_capacity: +(base * soc / 100).toFixed(1),
    balancing_mask: cells.map(v => v === Math.max(...cells) ? 1 : 0),
    any_alarm:      false,
    alarms: {
      cell_ovp: false, cell_uvp: false, pack_ovp: false,
      pack_uvp: false, chg_otp:  false, chg_ocp:  false,
      dsg_ocp:  false, scp:      false, cell_delta: cells[7] - cells[0] > 80,
    },
    timestamp: t / 1000,
  };
}

// ─── Normalisation snapshot API → format dashboard ────────────────────────────
// L'API retourne un dict plat (cell_01…cell_16, temp_01…, alarm_*)
// Le dashboard attend cell_voltages[], temperatures[], alarms{}
function normalizeSnapshot(raw) {
  const id = raw.bms_id;

  const cell_voltages = Array.from({ length: 16 }, (_, i) =>
    raw[`cell_${String(i + 1).padStart(2, "0")}`] ?? 0
  );

  const temperatures = Array.from({ length: 4 }, (_, i) =>
    raw[`temp_${String(i + 1).padStart(2, "0")}`] ?? 0
  ).filter(t => t !== 0);

  // alarm_cell_ovp → alarms.cell_ovp
  const alarms = {};
  Object.entries(raw).forEach(([k, v]) => {
    if (k.startsWith("alarm_") && k !== "any_alarm") alarms[k.slice(6)] = v;
  });

  return {
    ...raw,
    bms_name:     BMS_NAMES[id] || `BMS ${id}`,
    cell_voltages,
    temperatures,
    alarms,
  };
}

// ─── Hooks ────────────────────────────────────────────────────────────────────

// WebSocket live — se connecte à /ws/bms/stream, fallback sur mock
function useLiveData() {
  const [data, setData] = useState({
    1: generateSnapshot(1),
    2: generateSnapshot(2),
  });
  const [connected, setConnected] = useState(false);
  const histRef   = useRef({ 1: [], 2: [] });
  const wsRef     = useRef(null);

  useEffect(() => {
    const proto  = window.location.protocol === "https:" ? "wss" : "ws";
    const WS_URL = `${proto}://${window.location.host}/ws/bms/stream`;
    let reconnectTimer = null;

    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);

      ws.onmessage = evt => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "snapshot" && msg.data) {
            const next = {};
            Object.entries(msg.data).forEach(([k, v]) => {
              next[+k] = normalizeSnapshot(v);
            });
            setData(next);
            const ts = Date.now();
            [1, 2].forEach(id => {
              if (next[id]) {
                histRef.current[id].push({ ...next[id], ts });
                if (histRef.current[id].length > 180) histRef.current[id].shift();
              }
            });
          }
        } catch (_) { /* ignore parse errors */ }
      };

      ws.onerror = () => ws.close();

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        reconnectTimer = setTimeout(connect, 3000);
      };
    }

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, []);

  return { data, history: histRef.current, connected };
}

// Historique alertes — interroge /api/v1/alerts/history toutes les 10s
function useAlertHistory() {
  const [events, setEvents] = useState([]);

  useEffect(() => {
    const load = () =>
      fetch("/api/v1/alerts/history?limit=20")
        .then(r => r.ok ? r.json() : null)
        .then(d => d && setEvents(d.events || []))
        .catch(() => {});

    load();
    const iv = setInterval(load, 10000);
    return () => clearInterval(iv);
  }, []);

  return events;
}

// ─── Composants UI de base ────────────────────────────────────────────────────
const Card = ({ children, className = "", style = {} }) => (
  <div style={{
    background: C.bgCard, border: `1px solid ${C.border}`,
    borderRadius: 8, padding: 16, ...style,
  }} className={className}>
    {children}
  </div>
);

const Label = ({ children, style = {} }) => (
  <div style={{
    fontSize: 10, letterSpacing: 2, textTransform: "uppercase",
    color: C.textMuted, fontFamily: "'Space Mono', monospace",
    marginBottom: 4, ...style,
  }}>{children}</div>
);

const BigVal = ({ value, unit, color = C.text, size = 32 }) => (
  <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
    <span style={{
      fontSize: size, fontWeight: 700, fontFamily: "'Space Mono', monospace",
      color, lineHeight: 1, letterSpacing: -1,
    }}>{value ?? "—"}</span>
    {unit && <span style={{ fontSize: size * 0.45, color: C.textMuted }}>{unit}</span>}
  </div>
);

const Pill = ({ children, color = C.primary }) => (
  <span style={{
    display: "inline-block", padding: "2px 8px", borderRadius: 99,
    background: color + "22", border: `1px solid ${color}55`,
    color, fontSize: 11, fontFamily: "'Space Mono', monospace",
    letterSpacing: 1,
  }}>{children}</span>
);

const MosBadge = ({ on, label }) => (
  <div style={{
    display: "flex", alignItems: "center", gap: 6,
    padding: "6px 12px", borderRadius: 6,
    background: on ? C.green + "18" : C.red + "18",
    border: `1px solid ${on ? C.green : C.red}44`,
  }}>
    <div style={{
      width: 8, height: 8, borderRadius: "50%",
      background: on ? C.green : C.red,
      boxShadow: `0 0 6px ${on ? C.green : C.red}`,
    }}/>
    <span style={{ fontSize: 11, color: on ? C.green : C.red,
      fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>
      {label} {on ? "ON" : "OFF"}
    </span>
  </div>
);

// ─── Gauge SOC ────────────────────────────────────────────────────────────────
function SocGauge({ soc, size = 160 }) {
  const r = size * 0.38;
  const cx = size / 2, cy = size / 2;
  const startA = 210, endA = -30;
  const range  = 240;
  const angle  = startA - (soc / 100) * range;
  const toRad  = d => (d * Math.PI) / 180;
  const arc    = (a1, a2, r2) => {
    const x1 = cx + r2 * Math.cos(toRad(a1));
    const y1 = cy - r2 * Math.sin(toRad(a1));
    const x2 = cx + r2 * Math.cos(toRad(a2));
    const y2 = cy - r2 * Math.sin(toRad(a2));
    const lg  = Math.abs(a1 - a2) > 180 ? 1 : 0;
    return `M ${x1} ${y1} A ${r2} ${r2} 0 ${lg} 0 ${x2} ${y2}`;
  };
  const color = soc > 50 ? C.green : soc > 20 ? C.yellow : C.red;
  return (
    <svg width={size} height={size}>
      <defs>
        <linearGradient id="gSOC" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor={C.red}/>
          <stop offset="50%" stopColor={C.yellow}/>
          <stop offset="100%" stopColor={C.green}/>
        </linearGradient>
      </defs>
      {/* Track */}
      <path d={arc(startA, endA + 0.01, r)} fill="none"
        stroke={C.border} strokeWidth={12} strokeLinecap="round"/>
      {/* Fill */}
      <path d={arc(startA, angle, r)} fill="none"
        stroke="url(#gSOC)" strokeWidth={12} strokeLinecap="round"/>
      {/* Needle */}
      <line
        x1={cx} y1={cy}
        x2={cx + (r - 10) * Math.cos(toRad(angle))}
        y2={cy - (r - 10) * Math.sin(toRad(angle))}
        stroke={color} strokeWidth={2} strokeLinecap="round"/>
      <circle cx={cx} cy={cy} r={6} fill={color}/>
      {/* Value */}
      <text x={cx} y={cy + 24} textAnchor="middle"
        fill={color} fontSize={28} fontWeight={700}
        fontFamily="'Space Mono', monospace">
        {soc?.toFixed(1)}%
      </text>
      <text x={cx} y={cy + 40} textAnchor="middle"
        fill={C.textMuted} fontSize={9} letterSpacing={2}
        fontFamily="'Space Mono', monospace">STATE OF CHARGE</text>
    </svg>
  );
}

// ─── SVG Range Chart cellules ─────────────────────────────────────────────────
function CellRangeChart({ voltages = [], highlight = [7, 15] }) {
  const W = 520, H = 140;
  const PAD = { l: 40, r: 8, t: 8, b: 24 };
  const n   = voltages.length;
  if (!n) return null;
  const vMin = 3000, vMax = 3600;
  const barW  = (W - PAD.l - PAD.r) / n - 2;
  const yScale = v => PAD.t + (H - PAD.t - PAD.b) * (1 - (v - vMin) / (vMax - vMin));
  const yLines = [3000, 3100, 3200, 3300, 3400, 3500, 3600];

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ overflow: "visible" }}>
      {/* Grid lines */}
      {yLines.map(v => (
        <g key={v}>
          <line x1={PAD.l} x2={W - PAD.r} y1={yScale(v)} y2={yScale(v)}
            stroke={C.border} strokeWidth={0.5}/>
          <text x={PAD.l - 4} y={yScale(v) + 4} textAnchor="end"
            fill={C.textMuted} fontSize={7} fontFamily="'Space Mono', monospace">
            {(v/1000).toFixed(1)}
          </text>
        </g>
      ))}
      {/* Bars */}
      {voltages.map((v, i) => {
        const isHL  = highlight.includes(i);
        const isMax = v === Math.max(...voltages);
        const isMin = v === Math.min(...voltages);
        const color = isHL ? C.red : isMax ? C.orange : isMin ? C.yellow : C.primary;
        const x     = PAD.l + i * ((W - PAD.l - PAD.r) / n);
        const y     = yScale(v);
        const bh    = H - PAD.b - y;
        return (
          <g key={i}>
            <rect x={x + 1} y={y} width={barW} height={bh}
              fill={color + "33"} stroke={color} strokeWidth={1} rx={2}/>
            <text x={x + barW/2 + 1} y={H - PAD.b + 10}
              textAnchor="middle" fill={C.textMuted} fontSize={7}
              fontFamily="'Space Mono', monospace">
              {i + 1}
            </text>
          </g>
        );
      })}
      {/* Average line */}
      {voltages.length > 0 && (() => {
        const avg = voltages.reduce((a,b)=>a+b,0)/voltages.length;
        const y   = yScale(avg);
        return (
          <line x1={PAD.l} x2={W - PAD.r} y1={y} y2={y}
            stroke={C.accent} strokeWidth={1} strokeDasharray="4 3" opacity={0.6}/>
        );
      })()}
    </svg>
  );
}

// ─── Sparkline ────────────────────────────────────────────────────────────────
function Sparkline({ data = [], color = C.primary, height = 40 }) {
  if (data.length < 2) return null;
  const W = 200;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const pts   = data.map((v, i) => [
    (i / (data.length - 1)) * W,
    height - ((v - min) / range) * (height - 4) - 2,
  ]);
  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ");
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id={`sg${color.replace("#","")}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.3}/>
          <stop offset="100%" stopColor={color} stopOpacity={0}/>
        </linearGradient>
      </defs>
      <path d={`${d} L ${W} ${height} L 0 ${height} Z`}
        fill={`url(#sg${color.replace("#","")})`}/>
      <path d={d} fill="none" stroke={color} strokeWidth={1.5}/>
    </svg>
  );
}

// ─── Barre de navigation ──────────────────────────────────────────────────────
const PAGES = [
  { id: "dashboard",    label: "Dashboard",    icon: "⬡" },
  { id: "cells",        label: "Cellules",     icon: "▦" },
  { id: "temperatures", label: "Températures", icon: "⬡" },
  { id: "alarms",       label: "Alarmes",      icon: "◈" },
  { id: "control",      label: "Contrôle",     icon: "◉" },
  { id: "config",       label: "Config",       icon: "⚙" },
  { id: "dual",         label: "Dual BMS",     icon: "⬡⬡" },
  { id: "stats",        label: "Stats",        icon: "▦" },
];

function NavBar({ page, setPage, data, connected }) {
  const hasAlarm = Object.values(data).some(d => d.any_alarm);
  return (
    <div style={{
      position: "fixed", top: 0, left: 0, right: 0, zIndex: 100,
      background: C.bgCard + "ee",
      backdropFilter: "blur(12px)",
      borderBottom: `1px solid ${C.border}`,
      display: "flex", alignItems: "center",
      padding: "0 16px", height: 52,
      gap: 0,
    }}>
      {/* Logo */}
      <div style={{
        fontFamily: "'Space Mono', monospace",
        fontSize: 13, fontWeight: 700, color: C.primary,
        letterSpacing: 2, marginRight: 24, whiteSpace: "nowrap",
        borderRight: `1px solid ${C.border}`, paddingRight: 24,
      }}>
        DALY<span style={{ color: C.accent }}>BMS</span>
      </div>

      {/* Nav links */}
      <div style={{ display: "flex", flex: 1, gap: 2, overflowX: "auto" }}>
        {PAGES.map(p => (
          <button key={p.id} onClick={() => setPage(p.id)} style={{
            background: page === p.id ? C.primary + "33" : "transparent",
            border: `1px solid ${page === p.id ? C.primary + "88" : "transparent"}`,
            borderRadius: 6, padding: "5px 12px", cursor: "pointer",
            color: page === p.id ? C.text : C.textMuted,
            fontFamily: "'Space Mono', monospace", fontSize: 11,
            letterSpacing: 1, whiteSpace: "nowrap",
            display: "flex", alignItems: "center", gap: 6,
            transition: "all 0.15s",
            position: "relative",
          }}>
            {p.id === "alarms" && hasAlarm && (
              <span style={{
                position: "absolute", top: 4, right: 4,
                width: 6, height: 6, borderRadius: "50%",
                background: C.red, boxShadow: `0 0 6px ${C.red}`,
              }}/>
            )}
            {p.label}
          </button>
        ))}
      </div>

      {/* Status indicators */}
      <div style={{ display: "flex", gap: 12, marginLeft: 16, alignItems: "center" }}>
        {[1, 2].map(id => (
          <div key={id} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{
              width: 7, height: 7, borderRadius: "50%",
              background: data[id] ? C.green : C.red,
              boxShadow: `0 0 5px ${data[id] ? C.green : C.red}`,
            }}/>
            <span style={{
              fontSize: 10, color: C.textMuted,
              fontFamily: "'Space Mono', monospace", letterSpacing: 1,
            }}>BMS{id}</span>
          </div>
        ))}
        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: connected ? C.primary : C.red,
            boxShadow: `0 0 5px ${connected ? C.primary : C.red}`,
          }}/>
          <span style={{ fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            {connected ? "LIVE" : "OFF"}
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── BMS Selector ─────────────────────────────────────────────────────────────
function BmsSelector({ selected, setSelected }) {
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
      {[1, 2].map(id => (
        <button key={id} onClick={() => setSelected(id)} style={{
          padding: "6px 16px", borderRadius: 6, cursor: "pointer",
          background: selected === id ? C.primary : "transparent",
          border: `1px solid ${selected === id ? C.primary : C.border}`,
          color: selected === id ? "#fff" : C.textMuted,
          fontFamily: "'Space Mono', monospace", fontSize: 11,
          letterSpacing: 1,
        }}>
          BMS {id} — {id === 1 ? "320Ah" : "360Ah"}
        </button>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 1 — DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════════
function PageDashboard({ data, history }) {
  const [sel, setSel] = useState(1);
  const d   = data[sel] || {};
  const hist = history[sel] || [];

  const socHist     = hist.map(h => h.soc);
  const voltHist    = hist.map(h => h.pack_voltage);
  const currHist    = hist.map(h => h.pack_current);
  const powerHist   = hist.map(h => h.power);

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>
      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr 1fr", gap: 12 }}>
        {/* SOC Gauge */}
        <Card style={{ display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center", padding: 20 }}>
          <SocGauge soc={d.soc} size={170}/>
          <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
            <MosBadge on={d.charge_mos}    label="CHG"/>
            <MosBadge on={d.discharge_mos} label="DSG"/>
          </div>
        </Card>

        {/* Tension */}
        <Card>
          <Label>Tension Pack</Label>
          <BigVal value={d.pack_voltage?.toFixed(2)} unit="V" color={C.primaryLt}/>
          <div style={{ marginTop: 8, height: 50 }}>
            <Sparkline data={voltHist} color={C.primaryLt}/>
          </div>
          <div style={{ marginTop: 8, display: "flex", justifyContent: "space-between" }}>
            <div>
              <Label>Cell Min</Label>
              <span style={{ color: C.yellow, fontFamily: "'Space Mono', monospace", fontSize: 13 }}>
                {(d.cell_min_v/1000).toFixed(3)}V <span style={{color:C.textMuted}}>#{d.cell_min_num}</span>
              </span>
            </div>
            <div>
              <Label>Cell Max</Label>
              <span style={{ color: C.orange, fontFamily: "'Space Mono', monospace", fontSize: 13 }}>
                {(d.cell_max_v/1000).toFixed(3)}V <span style={{color:C.textMuted}}>#{d.cell_max_num}</span>
              </span>
            </div>
            <div>
              <Label>Delta</Label>
              <span style={{
                color: d.cell_delta > 80 ? C.red : d.cell_delta > 40 ? C.yellow : C.green,
                fontFamily: "'Space Mono', monospace", fontSize: 13,
              }}>{d.cell_delta}mV</span>
            </div>
          </div>
        </Card>

        {/* Courant & Puissance */}
        <Card>
          <Label>Courant</Label>
          <BigVal
            value={d.pack_current?.toFixed(1)} unit="A"
            color={d.pack_current >= 0 ? C.green : C.orange}/>
          <div style={{ marginTop: 8, height: 50 }}>
            <Sparkline data={currHist} color={C.green}/>
          </div>
          <div style={{ marginTop: 8 }}>
            <Label>Puissance</Label>
            <BigVal value={d.power?.toFixed(0)} unit="W" color={C.accent} size={22}/>
          </div>
          <div style={{ marginTop: 8, height: 30 }}>
            <Sparkline data={powerHist} color={C.accent} height={30}/>
          </div>
        </Card>

        {/* Infos pack */}
        <Card>
          <Label>Capacité Restante</Label>
          <BigVal value={d.remaining_capacity?.toFixed(1)} unit="Ah" color={C.text} size={24}/>
          <div style={{ marginTop: 12 }}>
            <Label>Température Max</Label>
            <BigVal value={d.temp_max?.toFixed(1)} unit="°C"
              color={d.temp_max > 40 ? C.red : d.temp_max > 35 ? C.yellow : C.text} size={22}/>
          </div>
          <div style={{ marginTop: 12 }}>
            <Label>Cycles BMS</Label>
            <BigVal value={d.bms_cycles} color={C.textMuted} size={22}/>
          </div>
          <div style={{ marginTop: 12 }}>
            <Label>Balancing</Label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 3, marginTop: 4 }}>
              {(d.balancing_mask || []).map((b, i) => (
                <div key={i} style={{
                  width: 14, height: 14, borderRadius: 3,
                  background: b ? C.accent + "66" : C.border,
                  border: `1px solid ${b ? C.accent : C.border}`,
                  fontSize: 7, color: b ? C.accent : C.textDim,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontFamily: "'Space Mono', monospace",
                }}>{i+1}</div>
              ))}
            </div>
          </div>
        </Card>
      </div>

      {/* Barre temps réel SOC */}
      <Card style={{ marginTop: 12 }}>
        <Label>SOC — Historique 3 minutes</Label>
        <Sparkline data={socHist} color={C.primary} height={60}/>
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 2 — CELLULES
// ═══════════════════════════════════════════════════════════════════════════════
function PageCells({ data, history }) {
  const [sel, setSel] = useState(1);
  const d    = data[sel] || {};
  const cells = d.cell_voltages || [];
  const hist  = history[sel] || [];

  const cellColor = (v, i) => {
    if (i === 7 || i === 15) return C.red;
    if (v === Math.max(...cells)) return C.orange;
    if (v === Math.min(...cells)) return C.yellow;
    if (v > 3380) return C.green;
    return C.primary;
  };

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 12 }}>
        <div>
          {/* Grille cellules */}
          <Card style={{ marginBottom: 12 }}>
            <Label>Tensions individuelles — 16 cellules</Label>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(8, 1fr)", gap: 6, marginTop: 10 }}>
              {cells.map((v, i) => {
                const color = cellColor(v, i);
                const isHl  = i === 7 || i === 15;
                return (
                  <div key={i} style={{
                    background: color + "18",
                    border: `1px solid ${color}${isHl ? "ff" : "66"}`,
                    borderRadius: 6, padding: "8px 6px", textAlign: "center",
                    boxShadow: isHl ? `0 0 10px ${color}44` : "none",
                  }}>
                    <div style={{
                      fontSize: 9, color: C.textMuted,
                      fontFamily: "'Space Mono', monospace", letterSpacing: 1,
                    }}>#{i+1}</div>
                    <div style={{
                      fontSize: 13, fontWeight: 700, color,
                      fontFamily: "'Space Mono', monospace", marginTop: 3,
                    }}>{(v/1000).toFixed(3)}</div>
                    {d.balancing_mask?.[i] ? (
                      <div style={{ fontSize: 8, color: C.accent, marginTop: 2 }}>⚡BAL</div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </Card>

          {/* SVG Range Chart */}
          <Card>
            <Label>Range Chart — 3.000V à 3.600V (cellules #8 et #16 en rouge)</Label>
            <div style={{ marginTop: 10 }}>
              <CellRangeChart voltages={cells} highlight={[7, 15]}/>
            </div>
            <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
              {[
                { color: C.red,     label: "Cellules surveillées (#8, #16)" },
                { color: C.orange,  label: "Maximum" },
                { color: C.yellow,  label: "Minimum" },
                { color: C.primary, label: "Normal" },
                { color: C.accent,  label: "Moyenne" },
              ].map(({ color, label }) => (
                <div key={label} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                  <div style={{ width: 10, height: 10, background: color, borderRadius: 2 }}/>
                  <span style={{ fontSize: 9, color: C.textMuted,
                    fontFamily: "'Space Mono', monospace" }}>{label}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>

        {/* Panneau stats + historique */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Card>
            <Label>Statistiques cellules</Label>
            {[
              { l: "Moyenne",  v: `${(d.cell_avg/1000).toFixed(3)}V`, c: C.accent  },
              { l: "Minimum",  v: `${(d.cell_min_v/1000).toFixed(3)}V #${d.cell_min_num}`, c: C.yellow },
              { l: "Maximum",  v: `${(d.cell_max_v/1000).toFixed(3)}V #${d.cell_max_num}`, c: C.orange },
              { l: "Delta",    v: `${d.cell_delta}mV`, c: d.cell_delta > 80 ? C.red : d.cell_delta > 40 ? C.yellow : C.green },
            ].map(({ l, v, c }) => (
              <div key={l} style={{ marginTop: 10, paddingBottom: 10,
                borderBottom: `1px solid ${C.border}` }}>
                <Label>{l}</Label>
                <span style={{ fontFamily: "'Space Mono', monospace",
                  fontSize: 16, color: c }}>{v}</span>
              </div>
            ))}
          </Card>

          <Card>
            <Label>Delta cellule — historique</Label>
            <Sparkline
              data={hist.map(h => h.cell_delta)}
              color={C.orange} height={60}/>
          </Card>

          <Card>
            <Label>Cellule #8 — historique mV</Label>
            <Sparkline
              data={hist.map(h => h.cell_voltages?.[7] || 0)}
              color={C.red} height={50}/>
            <Label style={{ marginTop: 8 }}>Cellule #16 — historique mV</Label>
            <Sparkline
              data={hist.map(h => h.cell_voltages?.[15] || 0)}
              color={C.red} height={50}/>
          </Card>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 3 — TEMPÉRATURES
// ═══════════════════════════════════════════════════════════════════════════════
function PageTemperatures({ data, history }) {
  const [sel, setSel] = useState(1);
  const d    = data[sel] || {};
  const hist = history[sel] || [];
  const temps = d.temperatures || [];
  const colors = [C.primary, C.accent, C.orange, C.yellow];

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        {temps.map((t, i) => (
          <Card key={i}>
            <Label>Sonde NTC {i + 1}</Label>
            <BigVal
              value={t?.toFixed(1)} unit="°C"
              color={t > 45 ? C.red : t > 35 ? C.orange : colors[i]}
              size={36}/>
            <div style={{ marginTop: 12, height: 50 }}>
              <Sparkline
                data={hist.map(h => h.temperatures?.[i] || 0)}
                color={colors[i]}/>
            </div>
            <div style={{ marginTop: 8 }}>
              <Pill color={t > 45 ? C.red : t > 35 ? C.orange : C.green}>
                {t > 45 ? "CRITIQUE" : t > 35 ? "ÉLEVÉE" : "NORMALE"}
              </Pill>
            </div>
          </Card>
        ))}
      </div>

      <Card style={{ marginTop: 12 }}>
        <Label>Toutes les sondes — historique 3 minutes</Label>
        <div style={{ position: "relative", height: 100, marginTop: 10 }}>
          <svg width="100%" height={100} viewBox="0 0 600 100" preserveAspectRatio="none">
            {temps.map((_, i) => {
              const data_i = hist.map(h => h.temperatures?.[i] || 0);
              if (data_i.length < 2) return null;
              const min = 20, max = 60;
              const pts = data_i.map((v, j) => [
                (j / (data_i.length - 1)) * 600,
                100 - ((v - min) / (max - min)) * 96 - 2,
              ]);
              const d = pts.map((p, j) => `${j===0?"M":"L"} ${p[0]} ${p[1]}`).join(" ");
              return <path key={i} d={d} fill="none"
                stroke={colors[i]} strokeWidth={1.5} opacity={0.8}/>;
            })}
          </svg>
        </div>
        <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
          {temps.map((_, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 5 }}>
              <div style={{ width: 12, height: 3, background: colors[i], borderRadius: 2 }}/>
              <span style={{ fontSize: 9, color: C.textMuted,
                fontFamily: "'Space Mono', monospace" }}>Sonde {i+1}</span>
            </div>
          ))}
        </div>
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <Card>
          <Label>Température minimale</Label>
          <BigVal value={d.temp_min?.toFixed(1)} unit="°C" color={C.primary} size={28}/>
        </Card>
        <Card>
          <Label>Température maximale</Label>
          <BigVal value={d.temp_max?.toFixed(1)} unit="°C"
            color={d.temp_max > 45 ? C.red : d.temp_max > 35 ? C.orange : C.green} size={28}/>
        </Card>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 4 — ALARMES
// ═══════════════════════════════════════════════════════════════════════════════
const ALARM_LABELS = {
  cell_ovp:   "Surtension cellule (OVP)",
  cell_uvp:   "Sous-tension cellule (UVP)",
  pack_ovp:   "Surtension pack (OVP)",
  pack_uvp:   "Sous-tension pack (UVP)",
  chg_otp:    "Surtempérature charge (OTP)",
  chg_ocp:    "Surcourant charge (OCP)",
  dsg_ocp:    "Surcourant décharge (OCP)",
  scp:        "Court-circuit (SCP)",
  cell_delta: "Déséquilibre cellules",
};

const SEV_COLOR = { CRITICAL: C.red, WARNING: C.yellow, INFO: C.primary };

function PageAlarms({ data }) {
  const alertHistory = useAlertHistory();

  const allAlarms = Object.entries(data).flatMap(([id, d]) =>
    Object.entries(d.alarms || {}).map(([key, val]) => ({
      bms_id: +id, key, label: ALARM_LABELS[key] || key, active: val,
    }))
  );
  const active = allAlarms.filter(a => a.active);

  return (
    <div>
      {/* Bannière globale */}
      <div style={{
        padding: "12px 16px", borderRadius: 8, marginBottom: 16,
        background: active.length > 0 ? C.red + "18" : C.green + "18",
        border: `1px solid ${active.length > 0 ? C.red : C.green}44`,
        display: "flex", alignItems: "center", gap: 12,
      }}>
        <div style={{
          width: 12, height: 12, borderRadius: "50%",
          background: active.length > 0 ? C.red : C.green,
          boxShadow: `0 0 10px ${active.length > 0 ? C.red : C.green}`,
        }}/>
        <span style={{ fontFamily: "'Space Mono', monospace", fontSize: 12,
          color: active.length > 0 ? C.red : C.green, letterSpacing: 1 }}>
          {active.length > 0
            ? `${active.length} ALARME(S) ACTIVE(S)`
            : "SYSTÈME NOMINAL — AUCUNE ALARME"}
        </span>
      </div>

      {/* Tableau des flags */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {[1, 2].map(bmsId => (
          <Card key={bmsId}>
            <Label>BMS {bmsId} — {bmsId === 1 ? "Pack 320Ah" : "Pack 360Ah"}</Label>
            <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
              {Object.entries(data[bmsId]?.alarms || {}).map(([key, val]) => (
                <div key={key} style={{
                  display: "flex", justifyContent: "space-between",
                  alignItems: "center", padding: "7px 10px", borderRadius: 6,
                  background: val ? C.red + "18" : C.bgPanel,
                  border: `1px solid ${val ? C.red + "66" : C.border}`,
                }}>
                  <span style={{ fontSize: 11, color: val ? C.text : C.textMuted,
                    fontFamily: "'Space Mono', monospace" }}>
                    {ALARM_LABELS[key] || key}
                  </span>
                  <Pill color={val ? C.red : C.green}>
                    {val ? "ACTIF" : "OK"}
                  </Pill>
                </div>
              ))}
            </div>
          </Card>
        ))}
      </div>

      {/* Journal des événements — AlertBridge SQLite */}
      <Card style={{ marginTop: 12 }}>
        <Label>Journal des événements — {alertHistory.length > 0 ? `${alertHistory.length} derniers` : "chargement…"}</Label>
        <div style={{ marginTop: 10 }}>
          {alertHistory.length === 0 ? (
            <span style={{ fontSize: 10, color: C.textMuted,
              fontFamily: "'Space Mono', monospace" }}>
              Aucun événement — AlertBridge démarré
            </span>
          ) : alertHistory.map((row, i) => {
            const isTriggered = row.event === "triggered";
            const sev = SEV_COLOR[row.severity] || C.primary;
            const evtColor = isTriggered ? sev : C.green;
            const ts = new Date(row.timestamp * 1000).toLocaleTimeString("fr-FR");
            return (
              <div key={i} style={{
                display: "flex", gap: 12, padding: "8px 0",
                borderBottom: `1px solid ${C.border}`,
                alignItems: "center", flexWrap: "wrap",
              }}>
                <span style={{ fontSize: 10, color: C.textMuted,
                  fontFamily: "'Space Mono', monospace", minWidth: 60 }}>{ts}</span>
                <Pill color={C.textMuted}>BMS {row.bms_id}</Pill>
                <Pill color={sev}>{row.severity}</Pill>
                <span style={{ fontSize: 11, color: evtColor, flex: 1,
                  fontFamily: "'Space Mono', monospace" }}>
                  {row.rule_name} — {isTriggered ? "DÉCLENCHÉ" : "EFFACÉ"}
                </span>
                {row.value && (
                  <span style={{ fontSize: 11, color: C.textMuted,
                    fontFamily: "'Space Mono', monospace" }}>{row.value}</span>
                )}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 5 — CONTRÔLE MOS & SOC
// ═══════════════════════════════════════════════════════════════════════════════
function PageControl({ data }) {
  const [sel, setSel] = useState(1);
  const [confirm, setConfirm] = useState(null);
  const [socVal, setSocVal] = useState("72.0");
  const [feedback, setFeedback] = useState(null);
  const d = data[sel] || {};

  const doAction = (label, action) => {
    if (confirm === label) {
      action();
      setFeedback({ msg: `${label} — commande envoyée`, ok: true });
      setConfirm(null);
      setTimeout(() => setFeedback(null), 3000);
    } else {
      setConfirm(label);
    }
  };

  const CtrlBtn = ({ label, color = C.primary, action, danger = false }) => (
    <button onClick={() => doAction(label, action)} style={{
      padding: "10px 20px", borderRadius: 6, cursor: "pointer",
      background: confirm === label
        ? (danger ? C.red : color) + "44"
        : (danger ? C.red : color) + "18",
      border: `1px solid ${confirm === label
        ? (danger ? C.red : color)
        : (danger ? C.red : color) + "66"}`,
      color: danger ? C.red : color,
      fontFamily: "'Space Mono', monospace", fontSize: 11, letterSpacing: 1,
      transition: "all 0.15s",
    }}>
      {confirm === label ? `⚠ CONFIRMER ${label}` : label}
    </button>
  );

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>

      {feedback && (
        <div style={{
          padding: "10px 16px", borderRadius: 8, marginBottom: 12,
          background: feedback.ok ? C.green + "18" : C.red + "18",
          border: `1px solid ${feedback.ok ? C.green : C.red}44`,
          color: feedback.ok ? C.green : C.red,
          fontFamily: "'Space Mono', monospace", fontSize: 11,
        }}>{feedback.msg}</div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        {/* MOSFET CHG */}
        <Card>
          <Label>MOSFET Charge</Label>
          <div style={{ marginTop: 10 }}>
            <MosBadge on={d.charge_mos} label="CHG"/>
          </div>
          <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
            <CtrlBtn label="CHG ON"  color={C.green}  action={() => {}}/>
            <CtrlBtn label="CHG OFF" color={C.red}    action={() => {}} danger/>
          </div>
          <div style={{ marginTop: 8, fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            Commande MOSFET charge — double confirmation requise
          </div>
        </Card>

        {/* MOSFET DSG */}
        <Card>
          <Label>MOSFET Décharge</Label>
          <div style={{ marginTop: 10 }}>
            <MosBadge on={d.discharge_mos} label="DSG"/>
          </div>
          <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
            <CtrlBtn label="DSG ON"  color={C.green}  action={() => {}}/>
            <CtrlBtn label="DSG OFF" color={C.red}    action={() => {}} danger/>
          </div>
          <div style={{ marginTop: 8, fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            Commande MOSFET décharge — double confirmation requise
          </div>
        </Card>

        {/* Reset */}
        <Card>
          <Label>Reset BMS</Label>
          <div style={{ marginTop: 10, fontSize: 12, color: C.textMuted,
            fontFamily: "'Space Mono', monospace", lineHeight: 1.6 }}>
            Redémarre le BMS.<br/>Reconnexion en ~3s.
          </div>
          <div style={{ marginTop: 12 }}>
            <CtrlBtn label="RESET BMS" color={C.red} action={() => {}} danger/>
          </div>
        </Card>

        {/* Calibration SOC */}
        <Card style={{ gridColumn: "span 2" }}>
          <Label>Calibration SOC</Label>
          <div style={{ marginTop: 12, display: "flex", gap: 12, alignItems: "flex-end" }}>
            <div>
              <Label>Valeur cible (%)</Label>
              <input
                type="number" min={0} max={100} step={0.1}
                value={socVal}
                onChange={e => setSocVal(e.target.value)}
                style={{
                  background: C.bgPanel, border: `1px solid ${C.border}`,
                  borderRadius: 6, padding: "8px 12px", color: C.text,
                  fontFamily: "'Space Mono', monospace", fontSize: 16,
                  width: 120,
                }}/>
            </div>
            <CtrlBtn label={`SET SOC ${socVal}%`} color={C.primary}
              action={() => {}}/>
            <CtrlBtn label="SET 100%" color={C.green} action={() => setSocVal("100.0")}/>
            <CtrlBtn label="SET 0%"   color={C.orange} action={() => setSocVal("0.0")}/>
          </div>
          <div style={{ marginTop: 8, fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            SOC actuel : {d.soc?.toFixed(1)}% — Encodage : uint16, résolution 0.1%
          </div>
        </Card>

        {/* Cycles */}
        <Card>
          <Label>Compteur Cycles</Label>
          <BigVal value={d.bms_cycles} color={C.text} size={36}/>
          <div style={{ marginTop: 8, fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            Cycles charge / décharge enregistrés
          </div>
        </Card>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 6 — CONFIGURATION
// ═══════════════════════════════════════════════════════════════════════════════
function PageConfig() {
  const [sel, setSel] = useState(1);
  const [group, setGroup] = useState("protection_v");
  const [saved, setSaved] = useState(false);
  const [values, setValues] = useState({
    ovp_cell_v: "3.65", uvp_cell_v: "2.80",
    ovp_pack_v: "58.4", uvp_pack_v: "44.8",
    ocp_chg_a:  "70",   ocp_dsg_a: "100",
    scp_a:      "200",  otp_chg_c: "45",
    utp_chg_c:  "0",    otp_dsg_c: "60",
    utp_dsg_c:  "-10",  balance_v: "3.40",
    balance_delta_mv: "10", capacity_ah: "320",
    cell_count: "16",   sensor_count: "4",
  });

  const GROUPS = {
    protection_v: {
      label: "Protections Tension",
      fields: [
        { key: "ovp_cell_v", label: "OVP cellule",   unit: "V",   min: 3.40, max: 3.75 },
        { key: "uvp_cell_v", label: "UVP cellule",   unit: "V",   min: 2.50, max: 3.20 },
        { key: "ovp_pack_v", label: "OVP pack",      unit: "V",   min: 40,   max: 61   },
        { key: "uvp_pack_v", label: "UVP pack",      unit: "V",   min: 40,   max: 61   },
      ],
    },
    protection_i: {
      label: "Protections Courant",
      fields: [
        { key: "ocp_chg_a", label: "OCP charge",     unit: "A",   min: 1,    max: 500  },
        { key: "ocp_dsg_a", label: "OCP décharge",   unit: "A",   min: 1,    max: 500  },
        { key: "scp_a",     label: "SCP court-circuit", unit: "A", min: 1,    max: 500  },
      ],
    },
    protection_t: {
      label: "Protections Thermiques",
      fields: [
        { key: "otp_chg_c", label: "OTP charge",     unit: "°C",  min: 0,    max: 80   },
        { key: "utp_chg_c", label: "UTP charge",     unit: "°C",  min: -40,  max: 10   },
        { key: "otp_dsg_c", label: "OTP décharge",   unit: "°C",  min: 0,    max: 80   },
        { key: "utp_dsg_c", label: "UTP décharge",   unit: "°C",  min: -40,  max: 10   },
      ],
    },
    balancing: {
      label: "Balancing",
      fields: [
        { key: "balance_v",        label: "Tension déclenchement", unit: "V",  min: 3.30, max: 3.65 },
        { key: "balance_delta_mv", label: "Delta déclenchement",   unit: "mV", min: 5,    max: 500  },
      ],
    },
    pack: {
      label: "Paramètres Pack",
      fields: [
        { key: "capacity_ah",   label: "Capacité nominale", unit: "Ah", min: 10, max: 2000 },
        { key: "cell_count",    label: "Cellules en série", unit: "S",  min: 3,  max: 24   },
        { key: "sensor_count",  label: "Sondes NTC",        unit: "",   min: 1,  max: 8    },
      ],
    },
  };

  const handleSave = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>

      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        {Object.entries(GROUPS).map(([key, g]) => (
          <button key={key} onClick={() => setGroup(key)} style={{
            padding: "6px 14px", borderRadius: 6, cursor: "pointer",
            background: group === key ? C.primary + "33" : "transparent",
            border: `1px solid ${group === key ? C.primary : C.border}`,
            color: group === key ? C.text : C.textMuted,
            fontFamily: "'Space Mono', monospace", fontSize: 10, letterSpacing: 1,
          }}>{g.label}</button>
        ))}
      </div>

      <Card style={{ maxWidth: 600 }}>
        <Label>{GROUPS[group]?.label}</Label>
        <div style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 14 }}>
          {GROUPS[group]?.fields.map(({ key, label, unit, min, max }) => {
            const val = parseFloat(values[key]);
            const inRange = val >= min && val <= max;
            return (
              <div key={key}>
                <div style={{ display: "flex", justifyContent: "space-between",
                  alignItems: "center", marginBottom: 6 }}>
                  <Label style={{ margin: 0 }}>{label}</Label>
                  <span style={{ fontSize: 9, color: C.textMuted,
                    fontFamily: "'Space Mono', monospace" }}>
                    [{min} – {max} {unit}]
                  </span>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    type="number" step={0.01} min={min} max={max}
                    value={values[key]}
                    onChange={e => setValues(v => ({...v, [key]: e.target.value}))}
                    style={{
                      flex: 1, background: C.bgPanel,
                      border: `1px solid ${inRange ? C.border : C.red}`,
                      borderRadius: 6, padding: "8px 12px", color: C.text,
                      fontFamily: "'Space Mono', monospace", fontSize: 14,
                    }}/>
                  <span style={{ fontSize: 11, color: C.textMuted,
                    fontFamily: "'Space Mono', monospace", minWidth: 30 }}>{unit}</span>
                  {!inRange && (
                    <span style={{ color: C.red, fontSize: 10,
                      fontFamily: "'Space Mono', monospace" }}>✗</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: 20, display: "flex", gap: 10 }}>
          <button onClick={handleSave} style={{
            padding: "10px 24px", borderRadius: 6, cursor: "pointer",
            background: saved ? C.green + "33" : C.primary + "33",
            border: `1px solid ${saved ? C.green : C.primary}`,
            color: saved ? C.green : C.primary,
            fontFamily: "'Space Mono', monospace", fontSize: 11, letterSpacing: 1,
          }}>
            {saved ? "✓ ENREGISTRÉ" : "APPLIQUER BMS " + sel}
          </button>
          <div style={{ fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace", alignSelf: "center" }}>
            Double confirmation — vérification post-écriture activée
          </div>
        </div>
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 7 — DUAL BMS
// ═══════════════════════════════════════════════════════════════════════════════
function PageDual({ data }) {
  const Row = ({ label, v1, v2, unit = "", format = x => x, colorFn = () => C.text }) => (
    <div style={{
      display: "grid", gridTemplateColumns: "1fr 180px 1fr",
      alignItems: "center", padding: "9px 0",
      borderBottom: `1px solid ${C.border}`,
    }}>
      <span style={{ fontFamily: "'Space Mono', monospace", fontSize: 13,
        color: colorFn(v1), textAlign: "right", paddingRight: 20 }}>
        {format(v1)}{unit}
      </span>
      <span style={{ fontSize: 9, color: C.textMuted, textAlign: "center",
        fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>{label}</span>
      <span style={{ fontFamily: "'Space Mono', monospace", fontSize: 13,
        color: colorFn(v2), paddingLeft: 20 }}>
        {format(v2)}{unit}
      </span>
    </div>
  );

  const d1 = data[1] || {}, d2 = data[2] || {};

  return (
    <div>
      {/* Headers */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 180px 1fr", marginBottom: 12 }}>
        <Card style={{ textAlign: "right", marginRight: 6 }}>
          <Label>BMS 1 — Pack 320Ah</Label>
          <BigVal value={d1.soc?.toFixed(1)} unit="%" color={C.primary} size={28}/>
        </Card>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ fontSize: 10, color: C.textMuted,
            fontFamily: "'Space Mono', monospace", letterSpacing: 2 }}>VS</span>
        </div>
        <Card style={{ marginLeft: 6 }}>
          <Label>BMS 2 — Pack 360Ah</Label>
          <BigVal value={d2.soc?.toFixed(1)} unit="%" color={C.accent} size={28}/>
        </Card>
      </div>

      <Card>
        <Row label="SOC"             v1={d1.soc}           v2={d2.soc}
          unit="%" format={v => v?.toFixed(1)}
          colorFn={v => v > 50 ? C.green : v > 20 ? C.yellow : C.red}/>
        <Row label="Tension pack"    v1={d1.pack_voltage}  v2={d2.pack_voltage}
          unit="V" format={v => v?.toFixed(2)}/>
        <Row label="Courant"         v1={d1.pack_current}  v2={d2.pack_current}
          unit="A" format={v => v?.toFixed(1)}
          colorFn={v => v > 0 ? C.green : C.orange}/>
        <Row label="Puissance"       v1={d1.power}         v2={d2.power}
          unit="W" format={v => v?.toFixed(0)}/>
        <Row label="Delta cellule"   v1={d1.cell_delta}    v2={d2.cell_delta}
          unit="mV" format={v => v}
          colorFn={v => v > 80 ? C.red : v > 40 ? C.yellow : C.green}/>
        <Row label="Cell min"        v1={d1.cell_min_v}    v2={d2.cell_min_v}
          unit="mV" format={v => v}/>
        <Row label="Cell max"        v1={d1.cell_max_v}    v2={d2.cell_max_v}
          unit="mV" format={v => v}/>
        <Row label="Temp. max"       v1={d1.temp_max}      v2={d2.temp_max}
          unit="°C" format={v => v?.toFixed(1)}
          colorFn={v => v > 40 ? C.red : v > 35 ? C.yellow : C.text}/>
        <Row label="CHG MOS"         v1={d1.charge_mos}    v2={d2.charge_mos}
          format={v => v ? "ON" : "OFF"}
          colorFn={v => v ? C.green : C.red}/>
        <Row label="DSG MOS"         v1={d1.discharge_mos} v2={d2.discharge_mos}
          format={v => v ? "ON" : "OFF"}
          colorFn={v => v ? C.green : C.red}/>
        <Row label="Cycles"          v1={d1.bms_cycles}    v2={d2.bms_cycles}
          format={v => v}/>
        <Row label="Alarme active"   v1={d1.any_alarm}     v2={d2.any_alarm}
          format={v => v ? "OUI" : "NON"}
          colorFn={v => v ? C.red : C.green}/>
      </Card>

      {/* Barre comparaison SOC */}
      <Card style={{ marginTop: 12 }}>
        <Label>Comparaison SOC</Label>
        {[
          { id: 1, soc: d1.soc, color: C.primary, label: "Pack 320Ah" },
          { id: 2, soc: d2.soc, color: C.accent,  label: "Pack 360Ah" },
        ].map(({ id, soc, color, label }) => (
          <div key={id} style={{ marginTop: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between",
              marginBottom: 6 }}>
              <span style={{ fontSize: 10, color: C.textMuted,
                fontFamily: "'Space Mono', monospace" }}>{label}</span>
              <span style={{ fontSize: 10, color,
                fontFamily: "'Space Mono', monospace" }}>{soc?.toFixed(1)}%</span>
            </div>
            <div style={{ height: 10, background: C.border, borderRadius: 5 }}>
              <div style={{
                height: "100%", width: `${soc || 0}%`, borderRadius: 5,
                background: `linear-gradient(90deg, ${color}88, ${color})`,
                transition: "width 0.5s ease",
              }}/>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 8 — STATISTIQUES
// ═══════════════════════════════════════════════════════════════════════════════
function PageStats({ data, history }) {
  const [sel, setSel] = useState(1);
  const d    = data[sel] || {};
  const hist = history[sel] || [];
  const cap  = sel === 1 ? 320 : 360;

  // Données journalières simulées
  const dailyEnergy = Array.from({ length: 7 }, (_, i) => ({
    day:     ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"][i],
    charged: +(8 + Math.random() * 6).toFixed(1),
    discharged: +(7 + Math.random() * 5).toFixed(1),
  }));

  const maxE = Math.max(...dailyEnergy.flatMap(d => [d.charged, d.discharged]));

  return (
    <div>
      <BmsSelector selected={sel} setSelected={setSel}/>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        {[
          { label: "Cycles totaux",    v: d.bms_cycles,                 unit: "",    color: C.primary },
          { label: "Capacité restante",v: d.remaining_capacity?.toFixed(1), unit: "Ah", color: C.green },
          { label: "SOC actuel",       v: d.soc?.toFixed(1),            unit: "%",   color: C.accent  },
          { label: "Énergie nominale", v: (cap * 51.2 / 1000).toFixed(1), unit:"kWh", color: C.orange },
        ].map(({ label, v, unit, color }) => (
          <Card key={label}>
            <Label>{label}</Label>
            <BigVal value={v} unit={unit} color={color} size={28}/>
          </Card>
        ))}
      </div>

      {/* Graphique énergie 7 jours */}
      <Card style={{ marginTop: 12 }}>
        <Label>Énergie journalière — 7 derniers jours (simulation)</Label>
        <div style={{ marginTop: 16 }}>
          <svg width="100%" viewBox="0 0 560 120" preserveAspectRatio="xMidYMid meet">
            {dailyEnergy.map((d, i) => {
              const x   = 20 + i * 74;
              const bw  = 28;
              const hC  = (d.charged    / maxE) * 100;
              const hD  = (d.discharged / maxE) * 100;
              return (
                <g key={i}>
                  {/* Charged */}
                  <rect x={x} y={110 - hC} width={bw} height={hC}
                    fill={C.green + "88"} stroke={C.green} strokeWidth={1} rx={3}/>
                  {/* Discharged */}
                  <rect x={x + bw + 2} y={110 - hD} width={bw} height={hD}
                    fill={C.orange + "88"} stroke={C.orange} strokeWidth={1} rx={3}/>
                  <text x={x + bw} y={118} textAnchor="middle"
                    fill={C.textMuted} fontSize={9}
                    fontFamily="'Space Mono', monospace">{d.day}</text>
                </g>
              );
            })}
          </svg>
          <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
              <div style={{ width: 12, height: 12, background: C.green, borderRadius: 2 }}/>
              <span style={{ fontSize: 9, color: C.textMuted,
                fontFamily: "'Space Mono', monospace" }}>Chargée (kWh)</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
              <div style={{ width: 12, height: 12, background: C.orange, borderRadius: 2 }}/>
              <span style={{ fontSize: 9, color: C.textMuted,
                fontFamily: "'Space Mono', monospace" }}>Déchargée (kWh)</span>
            </div>
          </div>
        </div>
      </Card>

      {/* SOC historique */}
      <Card style={{ marginTop: 12 }}>
        <Label>SOC — Historique session en cours</Label>
        <Sparkline data={hist.map(h => h.soc)} color={C.primary} height={80}/>
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6 }}>
          <span style={{ fontSize: 9, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            Min : {Math.min(...hist.map(h => h.soc), 100).toFixed(1)}%
          </span>
          <span style={{ fontSize: 9, color: C.textMuted,
            fontFamily: "'Space Mono', monospace" }}>
            Max : {Math.max(...hist.map(h => h.soc), 0).toFixed(1)}%
          </span>
        </div>
      </Card>

      {/* Santé pack */}
      <Card style={{ marginTop: 12 }}>
        <Label>Santé estimée du pack</Label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginTop: 12 }}>
          {[
            { label: "Delta moyen", v: `${d.cell_delta || 0}mV`,
              ok: (d.cell_delta || 0) < 50 },
            { label: "Temp. opé.",  v: `${d.temp_max?.toFixed(1) || "--"}°C`,
              ok: (d.temp_max || 0) < 40 },
            { label: "SOC nominal", v: `${d.soc?.toFixed(1) || "--"}%`,
              ok: (d.soc || 0) > 20 },
          ].map(({ label, v, ok }) => (
            <div key={label} style={{
              padding: 12, borderRadius: 6,
              background: ok ? C.green + "18" : C.red + "18",
              border: `1px solid ${ok ? C.green : C.red}44`,
              textAlign: "center",
            }}>
              <div style={{ fontSize: 9, color: C.textMuted,
                fontFamily: "'Space Mono', monospace", letterSpacing: 1 }}>{label}</div>
              <div style={{ fontSize: 18, color: ok ? C.green : C.red,
                fontFamily: "'Space Mono', monospace", fontWeight: 700, marginTop: 4 }}>{v}</div>
              <div style={{ fontSize: 8, color: ok ? C.green : C.red,
                fontFamily: "'Space Mono', monospace", marginTop: 4, letterSpacing: 1 }}>
                {ok ? "✓ NOMINAL" : "⚠ ATTENTION"}
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// APP ROOT
// ═══════════════════════════════════════════════════════════════════════════════
export default function App() {
  const [page, setPage]     = useState("dashboard");
  const { data, history, connected } = useLiveData();

  const PAGE_MAP = {
    dashboard:    <PageDashboard    data={data} history={history}/>,
    cells:        <PageCells        data={data} history={history}/>,
    temperatures: <PageTemperatures data={data} history={history}/>,
    alarms:       <PageAlarms       data={data}/>,
    control:      <PageControl      data={data}/>,
    config:       <PageConfig/>,
    dual:         <PageDual         data={data}/>,
    stats:        <PageStats        data={data} history={history}/>,
  };

  return (
    <>
      {/* Google Fonts */}
      <link rel="preconnect" href="https://fonts.googleapis.com"/>
      <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap"
        rel="stylesheet"/>

      <div style={{
        minHeight: "100vh",
        background: C.bg,
        color: C.text,
        fontFamily: "'Space Mono', monospace",
      }}>
        <NavBar page={page} setPage={setPage} data={data} connected={connected}/>

        {/* Content */}
        <div style={{ paddingTop: 68, padding: "68px 16px 24px" }}>
          {/* Page title */}
          <div style={{
            display: "flex", alignItems: "center", gap: 12,
            marginBottom: 16, paddingBottom: 12,
            borderBottom: `1px solid ${C.border}`,
          }}>
            <div style={{ width: 3, height: 18, background: C.primary, borderRadius: 2 }}/>
            <span style={{ fontSize: 11, letterSpacing: 3, color: C.textMuted,
              textTransform: "uppercase" }}>
              {PAGES.find(p => p.id === page)?.label}
            </span>
            <span style={{ marginLeft: "auto", fontSize: 9, color: C.textDim }}>
              {new Date().toLocaleTimeString("fr-FR")}
            </span>
          </div>

          {PAGE_MAP[page]}
        </div>
      </div>
    </>
  );
}
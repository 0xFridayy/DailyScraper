import { useState } from "react";

const ACTIONS = ["ENTRY", "TP", "CUT LOSS", "EXIT"];

const CHECKLIST = {
  ENTRY: {
    TREND: [
      { id: "t1", text: "Price is above EMA 20 and EMA 50", bull: true },
      { id: "t2", text: "EMA 20 is above EMA 50 (golden alignment)", bull: true },
      { id: "t3", text: "Alligator lines are fanning out (not tangled)", bull: true },
      { id: "t4", text: "Higher High / Higher Low structure intact", bull: true },
    ],
    MOMENTUM: [
      { id: "m1", text: "RSI > 50 (bullish zone)", bull: true },
      { id: "m2", text: "RSI not overbought (< 70) or in confirmed uptrend", bull: true },
      { id: "m3", text: "No bearish divergence on RSI/Stochastic", bull: true },
      { id: "m4", text: "Stochastic %K crossing %D upward", bull: true },
      { id: "m5", text: "No Hidden Bearish Divergence visible", bull: true },
    ],
    VOLUME: [
      { id: "v1", text: "Volume on up-bars > volume on down-bars (last 5 candles)", bull: true },
      { id: "v2", text: "OBV is rising (confirming price)", bull: true },
      { id: "v3", text: "MFI > 50 (money flowing in)", bull: true },
      { id: "v4", text: "No climactic volume spike (exhaustion risk)", bull: true },
    ],
    TRANSACTION: [
      { id: "x1", text: "Bid wall holding at key support level", bull: true },
      { id: "x2", text: "Bid volume > Offer volume at current price", bull: true },
      { id: "x3", text: "No large offer wall directly above (resistance clear)", bull: true },
      { id: "x4", text: "Broker code CP/YP/target code is net buyer", bull: true },
      { id: "x5", text: "Running trades: buyers are aggressor (lifting offer)", bull: true },
      { id: "x6", text: "No spoofing detected (bid walls being pulled)", bull: true },
    ],
  },
  TP: {
    TREND: [
      { id: "t1", text: "Price approaching major resistance / previous high", bull: false },
      { id: "t2", text: "Price extended far from EMA 20 (overextended)", bull: false },
      { id: "t3", text: "Alligator lips starting to curl down or tangle", bull: false },
      { id: "t4", text: "HH/HL structure showing signs of weakening", bull: false },
    ],
    MOMENTUM: [
      { id: "m1", text: "RSI entering overbought zone (> 70)", bull: false },
      { id: "m2", text: "Bearish divergence forming (price new high, RSI lower high)", bull: false },
      { id: "m3", text: "Stochastic overbought and %K crossing %D downward", bull: false },
      { id: "m4", text: "Momentum slowing — smaller candle bodies on new highs", bull: false },
    ],
    VOLUME: [
      { id: "v1", text: "Volume declining on last up-move (weak push)", bull: false },
      { id: "v2", text: "OBV flattening or diverging from price", bull: false },
      { id: "v3", text: "MFI turning down from overbought", bull: false },
      { id: "v4", text: "Climactic volume spike on last candle (exhaustion)", bull: false },
    ],
    TRANSACTION: [
      { id: "x1", text: "Large offer wall appearing above (distribution)", bull: false },
      { id: "x2", text: "Offer volume > Bid volume at current price", bull: false },
      { id: "x3", text: "Target broker code switching to net seller", bull: false },
      { id: "x4", text: "Running trades: sellers becoming aggressor (hitting bid)", bull: false },
      { id: "x5", text: "Bid walls being pulled (fake support disappearing)", bull: false },
    ],
  },
  "CUT LOSS": {
    TREND: [
      { id: "t1", text: "Price breaks below EMA 20 with strong candle body", bull: false },
      { id: "t2", text: "EMA 20 crosses below EMA 50 (bearish cross)", bull: false },
      { id: "t3", text: "Alligator lines tangling or reversing downward", bull: false },
      { id: "t4", text: "Lower Low formed — HH/HL structure broken", bull: false },
      { id: "t5", text: "Price closes below your defined invalidation level", bull: false },
    ],
    MOMENTUM: [
      { id: "m1", text: "RSI breaks below 50 (regime shift)", bull: false },
      { id: "m2", text: "RSI making new lows (not just divergence — actual breakdown)", bull: false },
      { id: "m3", text: "Stochastic deeply oversold with no bounce signal", bull: false },
      { id: "m4", text: "Bearish divergence has fully played out", bull: false },
    ],
    VOLUME: [
      { id: "v1", text: "High volume on down-bar that breaks support", bull: false },
      { id: "v2", text: "OBV declining sharply (real selling pressure)", bull: false },
      { id: "v3", text: "MFI dropping below 20 (money leaving)", bull: false },
      { id: "v4", text: "Volume expanding on red candles (distribution confirmed)", bull: false },
    ],
    TRANSACTION: [
      { id: "x1", text: "Bid wall at support has been fully absorbed or pulled", bull: false },
      { id: "x2", text: "Offer volume overwhelms bid — queue collapsing", bull: false },
      { id: "x3", text: "Target broker code is now net seller", bull: false },
      { id: "x4", text: "Tape: large sell transactions hitting bid aggressively", bull: false },
      { id: "x5", text: "No institutional buying appearing on dip (no absorption)", bull: false },
    ],
  },
  EXIT: {
    TREND: [
      { id: "t1", text: "Trend has reached original target / measured move", bull: false },
      { id: "t2", text: "Price at major structure level (resistance, prev high)", bull: false },
      { id: "t3", text: "Time-based exit: session closing (14:45+ IDX)", bull: false },
      { id: "t4", text: "Overall market (IHSG) reversing trend", bull: false },
    ],
    MOMENTUM: [
      { id: "m1", text: "Momentum has normalized (RSI back to 50 from extreme)", bull: false },
      { id: "m2", text: "Divergence resolved — initial thesis played out", bull: false },
      { id: "m3", text: "No new momentum catalyst visible", bull: false },
    ],
    VOLUME: [
      { id: "v1", text: "Volume drying up — move losing participation", bull: false },
      { id: "v2", text: "OBV flat — no new money entering", bull: false },
      { id: "v3", text: "Risk/reward no longer favorable at current price", bull: false },
    ],
    TRANSACTION: [
      { id: "x1", text: "Broker code activity normalized (no more smart money flow)", bull: false },
      { id: "x2", text: "Bid-offer balanced — no directional edge remaining", bull: false },
      { id: "x3", text: "Tape quiet — no institutional aggression either direction", bull: false },
      { id: "x4", text: "Original transaction thesis has fully played out", bull: false },
    ],
  },
};

const LAYER_META = {
  TREND: { color: "#4ade80", label: "TREND", icon: "↗" },
  MOMENTUM: { color: "#fb923c", label: "MOMENTUM", icon: "⚡" },
  VOLUME: { color: "#60a5fa", label: "VOLUME", icon: "▐" },
  TRANSACTION: { color: "#c084fc", label: "TRANSACTION", icon: "⬡" },
};

const ACTION_META = {
  ENTRY: { color: "#4ade80", icon: "▲", desc: "Pre-entry confirmation" },
  TP: { color: "#facc15", icon: "✦", desc: "Take profit signal check" },
  "CUT LOSS": { color: "#f87171", icon: "✕", desc: "Invalidation check" },
  EXIT: { color: "#94a3b8", icon: "◉", desc: "Full position exit" },
};

export default function App() {
  const [action, setAction] = useState("ENTRY");
  const [checked, setChecked] = useState({});
  const [notes, setNotes] = useState({});
  const [noteOpen, setNoteOpen] = useState(null);

  const layers = CHECKLIST[action];
  const allItems = Object.values(layers).flat();
  const totalChecked = allItems.filter(i => checked[action + i.id]).length;
  const totalItems = allItems.length;
  const pct = Math.round((totalChecked / totalItems) * 100);

  function toggle(id) {
    const key = action + id;
    setChecked(c => ({ ...c, [key]: !c[key] }));
  }

  function reset() {
    const keys = allItems.map(i => action + i.id);
    setChecked(c => { const n = { ...c }; keys.forEach(k => delete n[k]); return n; });
    setNotes(n => { const m = { ...n }; delete m[action]; return m; });
  }

  const scoreColor = pct >= 80 ? "#4ade80" : pct >= 50 ? "#facc15" : "#f87171";
  const verdict = pct >= 80 ? "HIGH CONFLUENCE — proceed" : pct >= 60 ? "MODERATE — proceed with caution" : pct < 40 ? "LOW — do NOT act" : "MIXED — wait for clarity";

  return (
    <div style={{ background: "#0a0a0a", minHeight: "100vh", color: "#e0e0e0", fontFamily: "'JetBrains Mono','Fira Mono',monospace", fontSize: 12 }}>

      {/* Header */}
      <div style={{ borderBottom: "1px solid #1a1a1a", padding: "14px 20px" }}>
        <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: 3, color: "#fff", marginBottom: 4 }}>MODERN TA CHECKLIST</div>
        <div style={{ color: "#444", fontSize: 10, letterSpacing: 1 }}>Pre-trade signal confluence framework · IDX</div>
      </div>

      {/* Action Selector */}
      <div style={{ display: "flex", borderBottom: "1px solid #1a1a1a" }}>
        {ACTIONS.map(a => {
          const m = ACTION_META[a];
          const isActive = action === a;
          return (
            <button key={a} onClick={() => setAction(a)} style={{
              flex: 1, background: isActive ? "#111" : "none",
              border: "none", borderBottom: isActive ? `2px solid ${m.color}` : "2px solid transparent",
              color: isActive ? m.color : "#444",
              padding: "10px 6px", cursor: "pointer", fontFamily: "inherit",
              fontSize: 11, letterSpacing: 1, fontWeight: isActive ? 700 : 400,
            }}>
              {m.icon} {a}
            </button>
          );
        })}
      </div>

      <div style={{ padding: "16px 20px" }}>

        {/* Score bar */}
        <div style={{ background: "#111", border: "1px solid #1e1e1e", borderRadius: 4, padding: "14px 16px", marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div>
              <div style={{ color: "#555", fontSize: 10, marginBottom: 3 }}>CONFLUENCE SCORE</div>
              <div style={{ color: scoreColor, fontSize: 22, fontWeight: 700 }}>{pct}%</div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ color: "#555", fontSize: 10, marginBottom: 3 }}>VERDICT</div>
              <div style={{ color: scoreColor, fontSize: 11 }}>{verdict}</div>
            </div>
          </div>
          <div style={{ background: "#1a1a1a", borderRadius: 2, height: 4, overflow: "hidden" }}>
            <div style={{ background: scoreColor, width: `${pct}%`, height: "100%", transition: "width 0.3s ease", borderRadius: 2 }} />
          </div>
          <div style={{ color: "#333", fontSize: 10, marginTop: 6 }}>{totalChecked} / {totalItems} signals confirmed</div>
        </div>

        {/* Layer mini-scores */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginBottom: 16 }}>
          {Object.entries(layers).map(([layer, items]) => {
            const m = LAYER_META[layer];
            const cnt = items.filter(i => checked[action + i.id]).length;
            const layerPct = Math.round((cnt / items.length) * 100);
            return (
              <div key={layer} style={{ background: "#111", border: `1px solid #1e1e1e`, borderRadius: 4, padding: "10px 12px" }}>
                <div style={{ color: m.color, fontSize: 9, letterSpacing: 1, marginBottom: 6 }}>{m.icon} {m.label}</div>
                <div style={{ color: "#fff", fontSize: 15, fontWeight: 700 }}>{layerPct}%</div>
                <div style={{ background: "#1a1a1a", borderRadius: 2, height: 2, marginTop: 6 }}>
                  <div style={{ background: m.color, width: `${layerPct}%`, height: "100%", opacity: 0.7 }} />
                </div>
                <div style={{ color: "#444", fontSize: 9, marginTop: 4 }}>{cnt}/{items.length}</div>
              </div>
            );
          })}
        </div>

        {/* Checklist layers */}
        {Object.entries(layers).map(([layer, items]) => {
          const m = LAYER_META[layer];
          return (
            <div key={layer} style={{ marginBottom: 14 }}>
              <div style={{ color: m.color, fontSize: 10, letterSpacing: 2, marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
                <span>{m.icon} {m.label}</span>
                <div style={{ flex: 1, height: 1, background: "#1a1a1a" }} />
              </div>
              {items.map(item => {
                const key = action + item.id;
                const isChecked = !!checked[key];
                return (
                  <div key={item.id}
                    onClick={() => toggle(item.id)}
                    style={{
                      display: "flex", alignItems: "flex-start", gap: 12, padding: "9px 12px",
                      marginBottom: 4, borderRadius: 4, cursor: "pointer",
                      background: isChecked ? "rgba(255,255,255,0.03)" : "transparent",
                      border: `1px solid ${isChecked ? "#222" : "transparent"}`,
                      transition: "all 0.15s",
                    }}>
                    <div style={{
                      width: 16, height: 16, borderRadius: 3, border: `1px solid ${isChecked ? m.color : "#333"}`,
                      background: isChecked ? m.color + "22" : "transparent",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      flexShrink: 0, marginTop: 1, transition: "all 0.15s",
                    }}>
                      {isChecked && <span style={{ color: m.color, fontSize: 10, fontWeight: 700 }}>✓</span>}
                    </div>
                    <div style={{ color: isChecked ? "#ccc" : "#555", lineHeight: 1.6, fontSize: 12, transition: "color 0.15s" }}>
                      {item.text}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}

        {/* Notes */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ color: "#333", fontSize: 10, letterSpacing: 2, marginBottom: 6, display: "flex", alignItems: "center", gap: 8 }}>
            <span>◎ TRADE NOTES</span>
            <div style={{ flex: 1, height: 1, background: "#1a1a1a" }} />
            <span onClick={() => setNoteOpen(n => n === action ? null : action)} style={{ cursor: "pointer", color: "#444" }}>
              {noteOpen === action ? "▲ hide" : "▼ add"}
            </span>
          </div>
          {noteOpen === action && (
            <textarea
              value={notes[action] ?? ""}
              onChange={e => setNotes(n => ({ ...n, [action]: e.target.value }))}
              placeholder={`Notes for ${action}... ticker, price level, reasoning`}
              style={{
                width: "100%", background: "#111", border: "1px solid #222", borderRadius: 4,
                color: "#ccc", fontFamily: "inherit", fontSize: 11, padding: "10px 12px",
                resize: "vertical", minHeight: 80, outline: "none", boxSizing: "border-box", lineHeight: 1.7,
              }}
            />
          )}
          {notes[action] && noteOpen !== action && (
            <div style={{ color: "#444", fontSize: 11, fontStyle: "italic", padding: "6px 0" }}>
              "{notes[action].slice(0, 80)}{notes[action].length > 80 ? "..." : ""}"
            </div>
          )}
        </div>

        {/* Reset */}
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button onClick={reset} style={{
            background: "none", border: "1px solid #222", color: "#444",
            padding: "6px 16px", borderRadius: 4, cursor: "pointer",
            fontFamily: "inherit", fontSize: 11, letterSpacing: 1,
          }}>
            RESET {action}
          </button>
        </div>

      </div>
    </div>
  );
}

import { useState, useEffect, useRef, useCallback } from "react";

// ─── DATA ENGINE ────────────────────────────────────────────────────────────

const TICKERS = {
  BBCA: { name: "Bank Central Asia", sector: "Banking", beta: 0.85, vol: 0.018 },
  BBRI: { name: "Bank Rakyat Indonesia", sector: "Banking", beta: 0.9, vol: 0.021 },
  BMRI: { name: "Bank Mandiri", sector: "Banking", beta: 0.88, vol: 0.019 },
  BBNI: { name: "Bank Negara Indonesia", sector: "Banking", beta: 0.92, vol: 0.023 },
  UNVR: { name: "Unilever Indonesia", sector: "Consumer", beta: 0.6, vol: 0.014 },
  ICBP: { name: "Indofood CBP", sector: "Consumer", beta: 0.65, vol: 0.016 },
  MYOR: { name: "Mayora Indah", sector: "Consumer", beta: 0.7, vol: 0.022 },
  TLKM: { name: "Telkom Indonesia", sector: "Telco", beta: 0.75, vol: 0.017 },
  EXCL: { name: "XL Axiata", sector: "Telco", beta: 0.8, vol: 0.025 },
  ADRO: { name: "Adaro Energy", sector: "Mining", beta: 1.2, vol: 0.032 },
  PTBA: { name: "Tambang Batubara", sector: "Mining", beta: 1.15, vol: 0.03 },
  INCO: { name: "Vale Indonesia", sector: "Mining", beta: 1.3, vol: 0.035 },
  ASII: { name: "Astra International", sector: "Industrial", beta: 1.0, vol: 0.022 },
  UNTR: { name: "United Tractors", sector: "Industrial", beta: 1.05, vol: 0.024 },
  BSDE: { name: "Bumi Serpong Damai", sector: "Property", beta: 1.1, vol: 0.028 },
  SMRA: { name: "Summarecon Agung", sector: "Property", beta: 1.2, vol: 0.03 },
  GOTO: { name: "GoTo Gojek Tokopedia", sector: "Tech", beta: 1.5, vol: 0.045 },
  BUKA: { name: "Bukalapak.com", sector: "Tech", beta: 1.6, vol: 0.05 },
  TPIA: { name: "Chandra Asri Pacific", sector: "Chemical", beta: 1.1, vol: 0.03 },
  AALI: { name: "Astra Agro Lestari", sector: "Plantation", beta: 0.95, vol: 0.026 },
};

const BASE_PRICES = {
  BBCA: 9250, BBRI: 5400, BMRI: 6850, BBNI: 5250, UNVR: 2750, ICBP: 11200, MYOR: 2450,
  TLKM: 3850, EXCL: 1680, ADRO: 3200, PTBA: 3100, INCO: 6500, ASII: 5200, UNTR: 28000,
  BSDE: 1150, SMRA: 890, GOTO: 62, BUKA: 128, TPIA: 8500, AALI: 8200,
};

function seededRandom(seed) {
  let s = seed;
  return () => { s = (s * 1664525 + 1013904223) & 0xffffffff; return (s >>> 0) / 0x100000000; };
}

function generateOHLCV(ticker, days = 365) {
  const info = TICKERS[ticker];
  const rand = seededRandom(ticker.charCodeAt(0) * 1337 + ticker.charCodeAt(1) * 31);
  const data = [];
  let price = BASE_PRICES[ticker] * (0.7 + rand() * 0.3);
  const now = new Date();

  for (let i = days; i >= 0; i--) {
    const date = new Date(now); date.setDate(now.getDate() - i);
    if (date.getDay() === 0 || date.getDay() === 6) continue;

    const trend = 0.0002;
    const drift = trend + (rand() - 0.5) * info.vol * 2;
    const open = price;
    const close = open * (1 + drift);
    const high = Math.max(open, close) * (1 + rand() * info.vol * 0.5);
    const low = Math.min(open, close) * (1 - rand() * info.vol * 0.5);
    const volume = Math.floor((500000 + rand() * 2000000) * (1 + Math.abs(drift) * 10));
    const foreignFlow = (rand() - 0.45) * volume * 0.3;

    data.push({ date: date.toISOString().split("T")[0], open, high, low, close, volume, foreignFlow });
    price = close;
  }
  return data;
}

const PRICE_CACHE = {};
function getPrices(ticker) {
  if (!PRICE_CACHE[ticker]) PRICE_CACHE[ticker] = generateOHLCV(ticker, 500);
  return PRICE_CACHE[ticker];
}

// ─── TECHNICAL INDICATORS ───────────────────────────────────────────────────

function calcEMA(closes, period) {
  const k = 2 / (period + 1);
  const ema = [closes[0]];
  for (let i = 1; i < closes.length; i++) ema.push(closes[i] * k + ema[i - 1] * (1 - k));
  return ema;
}

function calcRSI(closes, period = 14) {
  const rsi = new Array(period).fill(null);
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) gains += diff; else losses -= diff;
  }
  let avgGain = gains / period, avgLoss = losses / period;
  rsi.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(0, diff)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(0, -diff)) / period;
    rsi.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
  }
  return rsi;
}

function calcMACD(closes) {
  const ema12 = calcEMA(closes, 12), ema26 = calcEMA(closes, 26);
  const macd = closes.map((_, i) => ema12[i] - ema26[i]);
  const signal = calcEMA(macd.slice(26), 9);
  return { macd, signal: [...new Array(26).fill(null), ...signal] };
}

function calcBollinger(closes, period = 20) {
  return closes.map((_, i) => {
    if (i < period - 1) return { mid: null, upper: null, lower: null };
    const slice = closes.slice(i - period + 1, i + 1);
    const mid = slice.reduce((a, b) => a + b, 0) / period;
    const std = Math.sqrt(slice.reduce((a, b) => a + (b - mid) ** 2, 0) / period);
    return { mid, upper: mid + 2 * std, lower: mid - 2 * std };
  });
}

function detectWyckoff(data) {
  const closes = data.map(d => d.close);
  const volumes = data.map(d => d.volume);
  const last20 = closes.slice(-20);
  const range = Math.max(...last20) - Math.min(...last20);
  const avgVol = volumes.slice(-20).reduce((a, b) => a + b, 0) / 20;
  const recentVol = volumes.slice(-5).reduce((a, b) => a + b, 0) / 5;
  const trend = (closes[closes.length - 1] - closes[closes.length - 20]) / closes[closes.length - 20];
  const volRatio = recentVol / avgVol;

  if (trend < -0.05 && volRatio > 1.3) return "Distribution";
  if (trend < -0.02 && range / closes[closes.length - 1] < 0.08) return "Markdown";
  if (Math.abs(trend) < 0.03 && volRatio > 1.2 && closes[closes.length - 1] < closes[closes.length - 20]) return "Accumulation";
  if (trend > 0.05 && volRatio > 1.2) return "Markup";
  if (Math.abs(trend) < 0.02 && range / closes[closes.length - 1] < 0.06) return "Ranging";
  return "Accumulation";
}

// ─── BACKTEST ENGINE ────────────────────────────────────────────────────────

function runBacktest(ticker, strategy, lookback = 180) {
  const allData = getPrices(ticker);
  const data = allData.slice(-lookback);
  const closes = data.map(d => d.close);
  const trades = [];
  let position = null;
  let signals = [];

  if (strategy === "EMA Crossover") {
    const ema9 = calcEMA(closes, 9), ema21 = calcEMA(closes, 21);
    for (let i = 22; i < closes.length; i++) {
      const crossUp = ema9[i] > ema21[i] && ema9[i - 1] <= ema21[i - 1];
      const crossDn = ema9[i] < ema21[i] && ema9[i - 1] >= ema21[i - 1];
      signals.push({ date: data[i].date, signal: crossUp ? 1 : crossDn ? -1 : 0 });
      if (crossUp && !position) position = { entry: closes[i], date: data[i].date };
      if (crossDn && position) { trades.push({ ...position, exit: closes[i], exitDate: data[i].date }); position = null; }
    }
  } else if (strategy === "RSI Mean Reversion") {
    const rsi = calcRSI(closes);
    for (let i = 15; i < closes.length; i++) {
      if (!rsi[i]) continue;
      signals.push({ date: data[i].date, signal: rsi[i] < 30 ? 1 : rsi[i] > 70 ? -1 : 0 });
      if (rsi[i] < 30 && !position) position = { entry: closes[i], date: data[i].date };
      if (rsi[i] > 65 && position) { trades.push({ ...position, exit: closes[i], exitDate: data[i].date }); position = null; }
    }
  } else if (strategy === "MACD Signal") {
    const { macd, signal } = calcMACD(closes);
    for (let i = 27; i < closes.length; i++) {
      if (!signal[i]) continue;
      const crossUp = macd[i] > signal[i] && macd[i - 1] <= signal[i - 1];
      const crossDn = macd[i] < signal[i] && macd[i - 1] >= signal[i - 1];
      signals.push({ date: data[i].date, signal: crossUp ? 1 : crossDn ? -1 : 0 });
      if (crossUp && !position) position = { entry: closes[i], date: data[i].date };
      if (crossDn && position) { trades.push({ ...position, exit: closes[i], exitDate: data[i].date }); position = null; }
    }
  } else if (strategy === "Breakout") {
    for (let i = 20; i < closes.length; i++) {
      const high20 = Math.max(...closes.slice(i - 20, i));
      const low20 = Math.min(...closes.slice(i - 20, i));
      const breakUp = closes[i] > high20 * 1.002;
      const breakDn = closes[i] < low20 * 0.998;
      signals.push({ date: data[i].date, signal: breakUp ? 1 : breakDn ? -1 : 0 });
      if (breakUp && !position) position = { entry: closes[i], date: data[i].date };
      if (breakDn && position) { trades.push({ ...position, exit: closes[i], exitDate: data[i].date }); position = null; }
    }
  } else if (strategy === "Broker Flow") {
    const foreignFlows = data.map(d => d.foreignFlow);
    for (let i = 5; i < closes.length; i++) {
      const netFlow5 = foreignFlows.slice(i - 5, i).reduce((a, b) => a + b, 0);
      signals.push({ date: data[i].date, signal: netFlow5 > 0 ? 1 : netFlow5 < 0 ? -1 : 0 });
      if (netFlow5 > 500000 && !position) position = { entry: closes[i], date: data[i].date };
      if (netFlow5 < -500000 && position) { trades.push({ ...position, exit: closes[i], exitDate: data[i].date }); position = null; }
    }
  }

  if (position) trades.push({ ...position, exit: closes[closes.length - 1], exitDate: data[data.length - 1].date });

  let equity = 100, peak = 100, maxDD = 0;
  const curve = [{ date: data[0].date, equity: 100 }];
  let tIdx = 0;
  for (let i = 0; i < data.length; i++) {
    if (tIdx < trades.length && data[i].date === trades[tIdx].exitDate) {
      equity *= trades[tIdx].exit / trades[tIdx].entry;
      tIdx++;
    }
    if (equity > peak) peak = equity;
    const dd = (peak - equity) / peak;
    if (dd > maxDD) maxDD = dd;
    curve.push({ date: data[i].date, equity: parseFloat(equity.toFixed(2)) });
  }

  const wins = trades.filter(t => t.exit > t.entry).length;
  const totalReturn = (equity - 100).toFixed(1);
  const annualized = ((Math.pow(equity / 100, 252 / lookback) - 1) * 100).toFixed(1);
  const sharpe = ((annualized / 100) / (TICKERS[ticker].vol * Math.sqrt(252))).toFixed(2);
  const winRate = trades.length ? ((wins / trades.length) * 100).toFixed(0) : 0;

  return {
    trades: trades.map(t => ({ ...t, pnl: (((t.exit - t.entry) / t.entry) * 100).toFixed(2) })),
    curve, totalReturn, annualized, maxDD: (maxDD * 100).toFixed(1), sharpe, winRate,
    numTrades: trades.length, signals,
  };
}

// ─── SIGNAL SCANNER ─────────────────────────────────────────────────────────

function computeSignals() {
  return Object.entries(TICKERS).map(([ticker, info]) => {
    const data = getPrices(ticker);
    const closes = data.map(d => d.close);
    const last = closes[closes.length - 1];
    const rsi = calcRSI(closes);
    const rsiVal = rsi[rsi.length - 1];
    const ema9 = calcEMA(closes, 9), ema21 = calcEMA(closes, 21);
    const emaCross = ema9[ema9.length - 1] > ema21[ema21.length - 1];
    const vol5 = data.slice(-5).map(d => d.volume).reduce((a, b) => a + b, 0) / 5;
    const vol20 = data.slice(-20).map(d => d.volume).reduce((a, b) => a + b, 0) / 20;
    const volSpike = vol5 / vol20;
    const ff5 = data.slice(-5).map(d => d.foreignFlow).reduce((a, b) => a + b, 0);
    const wyckoff = detectWyckoff(data);
    const change1d = ((last - closes[closes.length - 2]) / closes[closes.length - 2] * 100).toFixed(2);

    let signal = "Neutral", strength = 3;
    if (rsiVal < 35 && emaCross && ff5 > 0) { signal = "Strong Buy"; strength = 5; }
    else if (rsiVal < 45 && emaCross) { signal = "Buy"; strength = 4; }
    else if (rsiVal > 65 && !emaCross && ff5 < 0) { signal = "Strong Sell"; strength = 5; }
    else if (rsiVal > 55 && !emaCross) { signal = "Sell"; strength = 4; }
    else if (wyckoff === "Accumulation") { signal = "Watch"; strength = 3; }

    return {
      ticker, ...info, price: last.toFixed(0),
      signal, strength, rsi: rsiVal.toFixed(0), volSpike: volSpike.toFixed(2),
      foreignFlow: ff5 > 0 ? "Net Buy" : "Net Sell", wyckoff, change1d,
    };
  });
}

// ─── COLORS & CONSTANTS ─────────────────────────────────────────────────────

const C = {
  bg: "#0D0F14", surface: "#13161E", border: "#1E2235", borderHi: "#2A3050",
  cyan: "#00C8F0", cyanDim: "#0099BB", text: "#E8EAF0", muted: "#7A8099", faint: "#3A4060",
  green: "#00D97E", red: "#FF4D6A", amber: "#FFB020", purple: "#8B6FFF",
};

const STRATEGIES = ["EMA Crossover", "RSI Mean Reversion", "MACD Signal", "Breakout", "Broker Flow"];
const SECTORS = ["All", ...new Set(Object.values(TICKERS).map(t => t.sector))];

// ─── MINI CHART ──────────────────────────────────────────────────────────────

function SparkLine({ data, color = C.cyan, height = 40 }) {
  const vals = data.map(d => d.close || d.equity);
  const min = Math.min(...vals), max = Math.max(...vals), range = max - min || 1;
  const w = 120;
  const points = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${height - ((v - min) / range) * (height - 4)}`).join(" ");
  return (
    <svg width={w} height={height} style={{ display: "block" }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

// ─── BACKTEST CHART ──────────────────────────────────────────────────────────

function EquityChart({ curve, ticker }) {
  const canvasRef = useRef();
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !curve.length) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.offsetWidth, H = canvas.offsetHeight;
    canvas.width = W * devicePixelRatio; canvas.height = H * devicePixelRatio;
    ctx.scale(devicePixelRatio, devicePixelRatio);

    const vals = curve.map(d => d.equity);
    const min = Math.min(...vals, 90), max = Math.max(...vals, 110);
    const pad = { t: 16, r: 16, b: 32, l: 48 };
    const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;

    ctx.fillStyle = C.surface;
    ctx.fillRect(0, 0, W, H);

    // grid
    ctx.strokeStyle = C.border; ctx.lineWidth = 0.5;
    for (let g = 0; g <= 4; g++) {
      const y = pad.t + ch - (g / 4) * ch;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      ctx.fillStyle = C.muted; ctx.font = "10px monospace"; ctx.textAlign = "right";
      ctx.fillText(((min + (g / 4) * (max - min))).toFixed(0), pad.l - 4, y + 4);
    }

    // baseline
    const baseY = pad.t + ch - ((100 - min) / (max - min)) * ch;
    ctx.strokeStyle = C.faint; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, baseY); ctx.lineTo(W - pad.r, baseY); ctx.stroke();
    ctx.setLineDash([]);

    // equity line
    const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
    grad.addColorStop(0, C.cyan + "33"); grad.addColorStop(1, C.cyan + "00");

    ctx.beginPath();
    curve.forEach((d, i) => {
      const x = pad.l + (i / (curve.length - 1)) * cw;
      const y = pad.t + ch - ((d.equity - min) / (max - min)) * ch;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = C.cyan; ctx.lineWidth = 2;
    ctx.stroke();

    const fillPath = new Path2D();
    curve.forEach((d, i) => {
      const x = pad.l + (i / (curve.length - 1)) * cw;
      const y = pad.t + ch - ((d.equity - min) / (max - min)) * ch;
      i === 0 ? fillPath.moveTo(x, y) : fillPath.lineTo(x, y);
    });
    fillPath.lineTo(pad.l + cw, pad.t + ch); fillPath.lineTo(pad.l, pad.t + ch); fillPath.closePath();
    ctx.fillStyle = grad; ctx.fill(fillPath);

    ctx.fillStyle = C.muted; ctx.font = "10px monospace"; ctx.textAlign = "center";
    const step = Math.max(1, Math.floor(curve.length / 5));
    for (let i = 0; i < curve.length; i += step) {
      const x = pad.l + (i / (curve.length - 1)) * cw;
      ctx.fillText(curve[i].date.slice(5), x, H - 8);
    }
  }, [curve]);

  return <canvas ref={canvasRef} style={{ width: "100%", height: 200, display: "block", borderRadius: 6, border: `1px solid ${C.border}` }} />;
}

// ─── CANDLE CHART ────────────────────────────────────────────────────────────

function CandleChart({ ticker, period = 60 }) {
  const canvasRef = useRef();
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const data = getPrices(ticker).slice(-period);
    const closes = data.map(d => d.close);
    const ema9 = calcEMA(closes, 9), ema21 = calcEMA(closes, 21);
    const boll = calcBollinger(closes);
    const ctx = canvas.getContext("2d");
    const W = canvas.offsetWidth, H = 220;
    canvas.width = W * devicePixelRatio; canvas.height = H * devicePixelRatio;
    ctx.scale(devicePixelRatio, devicePixelRatio);

    const allPrices = data.flatMap(d => [d.high, d.low]);
    const min = Math.min(...allPrices) * 0.99, max = Math.max(...allPrices) * 1.01;
    const pad = { t: 12, r: 12, b: 24, l: 60 };
    const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
    const toY = v => pad.t + ch - ((v - min) / (max - min)) * ch;
    const barW = Math.max(2, cw / period - 1);

    ctx.fillStyle = C.surface; ctx.fillRect(0, 0, W, H);

    for (let g = 0; g <= 3; g++) {
      const y = pad.t + (g / 3) * ch;
      ctx.strokeStyle = C.border; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
      const val = max - (g / 3) * (max - min);
      ctx.fillStyle = C.muted; ctx.font = "10px monospace"; ctx.textAlign = "right";
      ctx.fillText(val >= 1000 ? (val / 1000).toFixed(1) + "k" : val.toFixed(0), pad.l - 4, y + 4);
    }

    // Bollinger bands
    ctx.strokeStyle = C.purple + "55"; ctx.lineWidth = 1;
    const drawLine = (vals) => {
      ctx.beginPath();
      vals.forEach((v, i) => { if (!v) return; const x = pad.l + (i / (period - 1)) * cw; i === 0 || !vals[i-1] ? ctx.moveTo(x, toY(v)) : ctx.lineTo(x, toY(v)); });
      ctx.stroke();
    };
    drawLine(boll.map(b => b.upper)); drawLine(boll.map(b => b.lower));

    // EMA lines
    ctx.strokeStyle = C.amber; ctx.lineWidth = 1.2; drawLine(ema9);
    ctx.strokeStyle = C.cyan; ctx.lineWidth = 1.2; drawLine(ema21);

    // Candles
    data.forEach((d, i) => {
      const x = pad.l + (i / (period - 1)) * cw;
      const isGreen = d.close >= d.open;
      const color = isGreen ? C.green : C.red;
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, toY(d.high)); ctx.lineTo(x, toY(d.low)); ctx.stroke();
      const oY = toY(d.open), cY = toY(d.close);
      ctx.fillStyle = color;
      ctx.fillRect(x - barW / 2, Math.min(oY, cY), barW, Math.abs(oY - cY) || 1);
    });

    ctx.fillStyle = C.muted; ctx.font = "10px monospace"; ctx.textAlign = "center";
    [0, Math.floor(period / 3), Math.floor(period * 2 / 3), period - 1].forEach(i => {
      if (data[i]) ctx.fillText(data[i].date.slice(5), pad.l + (i / (period - 1)) * cw, H - 6);
    });
  }, [ticker, period]);

  return <canvas ref={canvasRef} style={{ width: "100%", height: 220, display: "block" }} />;
}

// ─── SIGNAL BADGE ────────────────────────────────────────────────────────────

function SignalBadge({ signal }) {
  const colors = {
    "Strong Buy": { bg: C.green + "22", text: C.green },
    "Buy": { bg: "#00D97E11", text: "#00B860" },
    "Neutral": { bg: C.faint, text: C.muted },
    "Watch": { bg: C.amber + "22", text: C.amber },
    "Sell": { bg: C.red + "22", text: "#FF6B80" },
    "Strong Sell": { bg: C.red + "33", text: C.red },
  };
  const c = colors[signal] || colors["Neutral"];
  return (
    <span style={{ background: c.bg, color: c.text, fontSize: 11, fontWeight: 500, padding: "2px 8px", borderRadius: 4, whiteSpace: "nowrap" }}>
      {signal}
    </span>
  );
}

// ─── STAT CARD ───────────────────────────────────────────────────────────────

function StatCard({ label, value, color }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 14px", minWidth: 90 }}>
      <div style={{ fontSize: 10, color: C.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: color || C.text, fontFamily: "monospace" }}>{value}</div>
    </div>
  );
}

// ─── AI RESEARCH AGENT ───────────────────────────────────────────────────────

async function callAgent(userQuery, signalData) {
  const tickerSummary = signalData.slice(0, 8).map(s =>
    `${s.ticker}: price=${s.price}, signal=${s.signal}, RSI=${s.rsi}, wyckoff=${s.wyckoff}, foreignFlow=${s.foreignFlow}`
  ).join("\n");

  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-6",
      max_tokens: 1000,
      system: `You are a professional IDX (Indonesia Stock Exchange) equity research agent. You provide concise, structured analysis combining technical signals, Wyckoff methodology, and broker flow interpretation. Respond in structured markdown with these sections when relevant: ## Tickers Identified, ## Signal Analysis, ## Strategy Recommendation, ## Backtest Trigger (format: BACKTEST:TICKER:STRATEGY where STRATEGY is one of: EMA Crossover, RSI Mean Reversion, MACD Signal, Breakout, Broker Flow). Keep responses under 400 words. Use IDX context (IHSG benchmark, foreign vs domestic flow, rupiah dynamics).`,
      messages: [
        { role: "user", content: `Current market signals:\n${tickerSummary}\n\nUser query: ${userQuery}` }
      ]
    })
  });
  const data = await res.json();
  return data.content?.[0]?.text || "Unable to process request.";
}

// ─── MAIN APP ────────────────────────────────────────────────────────────────

export default function App() {
  const [tab, setTab] = useState("agent");
  const [messages, setMessages] = useState([
    { role: "assistant", text: "IDX Research Agent ready. Ask me to analyze stocks, scan for signals, or backtest a strategy.\n\n**Try:** *\"Backtest RSI mean reversion on BBCA\"* or *\"Which banking stocks show accumulation?\"* or *\"Compare EMA crossover vs breakout on TLKM\"*" }
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [signals, setSignals] = useState([]);
  const [sectorFilter, setSectorFilter] = useState("All");
  const [sortKey, setSortKey] = useState("signal");
  const [btTicker, setBtTicker] = useState("BBCA");
  const [btStrategy, setBtStrategy] = useState("EMA Crossover");
  const [btResult, setBtResult] = useState(null);
  const [btPeriod, setBtPeriod] = useState(180);
  const [chartTicker, setChartTicker] = useState("BBCA");
  const [chartPeriod, setChartPeriod] = useState(60);
  const [inlineBt, setInlineBt] = useState(null);
  const chatEndRef = useRef();

  useEffect(() => {
    setSignals(computeSignals());
  }, []);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || loading) return;
    const query = input.trim();
    setInput("");
    setMessages(m => [...m, { role: "user", text: query }]);
    setLoading(true);
    try {
      const response = await callAgent(query, signals);
      setMessages(m => [...m, { role: "assistant", text: response }]);

      const btMatch = response.match(/BACKTEST:([A-Z]+):(.+)/);
      if (btMatch) {
        const [, ticker, strategy] = btMatch;
        const validTicker = TICKERS[ticker] ? ticker : "BBCA";
        const validStrategy = STRATEGIES.includes(strategy.trim()) ? strategy.trim() : "EMA Crossover";
        const result = runBacktest(validTicker, validStrategy, 180);
        setInlineBt({ ticker: validTicker, strategy: validStrategy, result });
      }
    } catch (e) {
      setMessages(m => [...m, { role: "assistant", text: `**Error:** ${e.message}. Check your Anthropic API key or network connection.` }]);
    }
    setLoading(false);
  };

  const runBt = () => {
    const result = runBacktest(btTicker, btStrategy, btPeriod);
    setBtResult({ ticker: btTicker, strategy: btStrategy, result });
  };

  const filteredSignals = signals
    .filter(s => sectorFilter === "All" || s.sector === sectorFilter)
    .sort((a, b) => {
      if (sortKey === "signal") {
        const order = ["Strong Buy", "Buy", "Watch", "Neutral", "Sell", "Strong Sell"];
        return order.indexOf(a.signal) - order.indexOf(b.signal);
      }
      if (sortKey === "rsi") return parseFloat(a.rsi) - parseFloat(b.rsi);
      if (sortKey === "volSpike") return parseFloat(b.volSpike) - parseFloat(a.volSpike);
      return 0;
    });

  const renderMarkdown = (text) => {
    return text
      .replace(/\*\*(.+?)\*\*/g, '<strong style="color:#E8EAF0">$1</strong>')
      .replace(/## (.+)/g, '<div style="font-size:13px;font-weight:600;color:#00C8F0;margin:12px 0 4px;text-transform:uppercase;letter-spacing:0.08em">$1</div>')
      .replace(/\*(.+?)\*/g, '<em style="color:#7A8099">$1</em>')
      .replace(/`(.+?)`/g, '<code style="background:#1E2235;padding:1px 5px;border-radius:3px;font-size:12px;color:#00C8F0">$1</code>')
      .replace(/BACKTEST:[A-Z]+:.+/g, '')
      .replace(/\n/g, '<br/>');
  };

  const tabStyle = (t) => ({
    padding: "8px 16px", fontSize: 12, fontWeight: 500, cursor: "pointer", border: "none",
    background: tab === t ? C.border : "transparent", color: tab === t ? C.cyan : C.muted,
    borderBottom: `2px solid ${tab === t ? C.cyan : "transparent"}`, fontFamily: "monospace",
    transition: "all 0.15s", textTransform: "uppercase", letterSpacing: "0.06em",
  });

  return (
    <div style={{ background: C.bg, color: C.text, fontFamily: "'Inter', system-ui, sans-serif", minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      {/* Header */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "12px 20px", display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: C.cyan, boxShadow: `0 0 8px ${C.cyan}` }} />
        <span style={{ fontFamily: "monospace", fontSize: 14, fontWeight: 600, color: C.cyan, letterSpacing: "0.1em" }}>IDX RESEARCH AGENT</span>
        <span style={{ color: C.faint, fontSize: 12 }}>|</span>
        <span style={{ color: C.muted, fontSize: 12 }}>Indonesia Stock Exchange · {Object.keys(TICKERS).length} tickers</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          {signals.filter(s => s.signal === "Strong Buy").length > 0 && (
            <span style={{ fontSize: 11, color: C.green, background: C.green + "22", padding: "2px 8px", borderRadius: 4 }}>
              {signals.filter(s => s.signal.includes("Buy")).length} BUY SIGNALS
            </span>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, display: "flex" }}>
        {[["agent", "Research Agent"], ["scanner", "Signal Scanner"], ["backtest", "Backtest Lab"], ["charts", "Charts"]].map(([t, label]) => (
          <button key={t} style={tabStyle(t)} onClick={() => setTab(t)}>{label}</button>
        ))}
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>

        {/* AGENT TAB */}
        {tab === "agent" && (
          <div style={{ maxWidth: 780, margin: "0 auto" }}>
            <div style={{ minHeight: 400, marginBottom: 16 }}>
              {messages.map((m, i) => (
                <div key={i} style={{ marginBottom: 16, display: "flex", gap: 10, flexDirection: m.role === "user" ? "row-reverse" : "row" }}>
                  <div style={{ width: 28, height: 28, borderRadius: "50%", background: m.role === "user" ? C.cyanDim : C.border, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: C.text, flexShrink: 0, fontFamily: "monospace" }}>
                    {m.role === "user" ? "U" : "AI"}
                  </div>
                  <div style={{ background: m.role === "user" ? C.cyanDim + "22" : C.surface, border: `1px solid ${m.role === "user" ? C.cyanDim + "44" : C.border}`, borderRadius: 10, padding: "10px 14px", maxWidth: "85%", fontSize: 13, lineHeight: 1.7, color: C.text }}>
                    <div dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text) }} />
                  </div>
                </div>
              ))}
              {loading && (
                <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
                  <div style={{ width: 28, height: 28, borderRadius: "50%", background: C.border, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: C.text, fontFamily: "monospace" }}>AI</div>
                  <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 14px", fontSize: 13, color: C.muted }}>
                    <span style={{ animation: "pulse 1s infinite" }}>Analyzing market data</span>
                    <span style={{ color: C.cyan }}>...</span>
                  </div>
                </div>
              )}
              {inlineBt && (
                <div style={{ background: C.surface, border: `1px solid ${C.borderHi}`, borderRadius: 10, padding: 16, marginBottom: 16 }}>
                  <div style={{ fontSize: 11, color: C.cyan, marginBottom: 10, fontFamily: "monospace", textTransform: "uppercase", letterSpacing: "0.08em" }}>Auto-Backtest: {inlineBt.ticker} · {inlineBt.strategy}</div>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                    <StatCard label="Return" value={`${inlineBt.result.totalReturn}%`} color={parseFloat(inlineBt.result.totalReturn) > 0 ? C.green : C.red} />
                    <StatCard label="Sharpe" value={inlineBt.result.sharpe} color={parseFloat(inlineBt.result.sharpe) > 1 ? C.green : C.amber} />
                    <StatCard label="Max DD" value={`${inlineBt.result.maxDD}%`} color={C.red} />
                    <StatCard label="Win Rate" value={`${inlineBt.result.winRate}%`} />
                    <StatCard label="Trades" value={inlineBt.result.numTrades} />
                  </div>
                  <EquityChart curve={inlineBt.result.curve} ticker={inlineBt.ticker} />
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => e.key === "Enter" && sendMessage()}
                placeholder="e.g. Backtest EMA crossover on ASII for 6 months..."
                style={{ flex: 1, background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 14px", color: C.text, fontSize: 13, fontFamily: "monospace", outline: "none" }}
              />
              <button onClick={sendMessage} disabled={loading || !input.trim()} style={{ background: C.cyan, border: "none", borderRadius: 8, padding: "10px 18px", color: "#000", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "monospace", opacity: loading || !input.trim() ? 0.5 : 1 }}>
                RUN
              </button>
            </div>
            <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
              {["Backtest RSI on BBCA", "Accumulation signals", "Compare BBRI vs BMRI", "Wyckoff phase GOTO", "Broker flow ASII"].map(q => (
                <button key={q} onClick={() => setInput(q)} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "4px 10px", color: C.muted, fontSize: 11, cursor: "pointer", fontFamily: "monospace" }}>{q}</button>
              ))}
            </div>
          </div>
        )}

        {/* SCANNER TAB */}
        {tab === "scanner" && (
          <div>
            <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
              <span style={{ fontSize: 12, color: C.muted, fontFamily: "monospace" }}>SECTOR:</span>
              {SECTORS.map(s => (
                <button key={s} onClick={() => setSectorFilter(s)} style={{ background: sectorFilter === s ? C.cyan + "22" : C.surface, border: `1px solid ${sectorFilter === s ? C.cyan : C.border}`, borderRadius: 6, padding: "4px 10px", color: sectorFilter === s ? C.cyan : C.muted, fontSize: 11, cursor: "pointer", fontFamily: "monospace" }}>{s}</button>
              ))}
              <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
                <span style={{ fontSize: 12, color: C.muted, fontFamily: "monospace" }}>SORT:</span>
                <select value={sortKey} onChange={e => setSortKey(e.target.value)} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "4px 8px", color: C.text, fontSize: 11, fontFamily: "monospace" }}>
                  <option value="signal">Signal</option>
                  <option value="rsi">RSI</option>
                  <option value="volSpike">Vol Spike</option>
                </select>
              </div>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, fontFamily: "monospace" }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                    {["Ticker", "Sector", "Price", "1D%", "Signal", "Str", "RSI", "Vol×", "Foreign", "Wyckoff", "Spark"].map(h => (
                      <th key={h} style={{ padding: "6px 10px", color: C.muted, textAlign: "left", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 500 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredSignals.map((s, i) => {
                    const spark = getPrices(s.ticker).slice(-30);
                    return (
                      <tr key={s.ticker} style={{ borderBottom: `1px solid ${C.border}22`, background: i % 2 === 0 ? "transparent" : C.surface + "44" }}>
                        <td style={{ padding: "8px 10px" }}>
                          <span style={{ color: C.cyan, fontWeight: 600, cursor: "pointer" }} onClick={() => { setChartTicker(s.ticker); setTab("charts"); }}>{s.ticker}</span>
                        </td>
                        <td style={{ padding: "8px 10px", color: C.muted }}>{s.sector}</td>
                        <td style={{ padding: "8px 10px", color: C.text }}>{Number(s.price).toLocaleString()}</td>
                        <td style={{ padding: "8px 10px", color: parseFloat(s.change1d) >= 0 ? C.green : C.red }}>{s.change1d > 0 ? "+" : ""}{s.change1d}%</td>
                        <td style={{ padding: "8px 10px" }}><SignalBadge signal={s.signal} /></td>
                        <td style={{ padding: "8px 10px", color: C.amber }}>{"★".repeat(s.strength)}</td>
                        <td style={{ padding: "8px 10px", color: parseFloat(s.rsi) < 30 ? C.green : parseFloat(s.rsi) > 70 ? C.red : C.text }}>{s.rsi}</td>
                        <td style={{ padding: "8px 10px", color: parseFloat(s.volSpike) > 1.5 ? C.amber : C.muted }}>{s.volSpike}×</td>
                        <td style={{ padding: "8px 10px", color: s.foreignFlow === "Net Buy" ? C.green : C.red }}>{s.foreignFlow}</td>
                        <td style={{ padding: "8px 10px", color: C.muted, fontSize: 11 }}>{s.wyckoff}</td>
                        <td style={{ padding: "4px 10px" }}>
                          <SparkLine data={spark} color={parseFloat(s.change1d) >= 0 ? C.green : C.red} height={32} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* BACKTEST TAB */}
        {tab === "backtest" && (
          <div style={{ maxWidth: 900, margin: "0 auto" }}>
            <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div>
                <div style={{ fontSize: 10, color: C.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "monospace" }}>Ticker</div>
                <select value={btTicker} onChange={e => setBtTicker(e.target.value)} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 12px", color: C.text, fontSize: 12, fontFamily: "monospace", minWidth: 100 }}>
                  {Object.keys(TICKERS).map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div>
                <div style={{ fontSize: 10, color: C.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "monospace" }}>Strategy</div>
                <select value={btStrategy} onChange={e => setBtStrategy(e.target.value)} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 12px", color: C.text, fontSize: 12, fontFamily: "monospace", minWidth: 160 }}>
                  {STRATEGIES.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <div style={{ fontSize: 10, color: C.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "monospace" }}>Lookback (days)</div>
                <select value={btPeriod} onChange={e => setBtPeriod(Number(e.target.value))} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 12px", color: C.text, fontSize: 12, fontFamily: "monospace" }}>
                  <option value={60}>60d</option>
                  <option value={90}>90d</option>
                  <option value={180}>180d</option>
                  <option value={365}>365d</option>
                </select>
              </div>
              <button onClick={runBt} style={{ background: C.cyan, border: "none", borderRadius: 6, padding: "9px 20px", color: "#000", fontWeight: 700, fontSize: 12, cursor: "pointer", fontFamily: "monospace" }}>
                RUN BACKTEST
              </button>
            </div>

            {btResult && (
              <div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
                  <StatCard label="Total Return" value={`${btResult.result.totalReturn}%`} color={parseFloat(btResult.result.totalReturn) > 0 ? C.green : C.red} />
                  <StatCard label="Ann. Return" value={`${btResult.result.annualized}%`} color={parseFloat(btResult.result.annualized) > 0 ? C.green : C.red} />
                  <StatCard label="Sharpe" value={btResult.result.sharpe} color={parseFloat(btResult.result.sharpe) > 1 ? C.green : parseFloat(btResult.result.sharpe) > 0 ? C.amber : C.red} />
                  <StatCard label="Max Drawdown" value={`${btResult.result.maxDD}%`} color={C.red} />
                  <StatCard label="Win Rate" value={`${btResult.result.winRate}%`} />
                  <StatCard label="Trades" value={btResult.result.numTrades} />
                </div>

                <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: 16, marginBottom: 16 }}>
                  <div style={{ fontSize: 11, color: C.muted, marginBottom: 10, fontFamily: "monospace" }}>EQUITY CURVE — {btResult.ticker} · {btResult.strategy} · {btPeriod}d</div>
                  <EquityChart curve={btResult.result.curve} ticker={btResult.ticker} />
                </div>

                {btResult.result.trades.length > 0 && (
                  <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: 16 }}>
                    <div style={{ fontSize: 11, color: C.muted, marginBottom: 10, fontFamily: "monospace" }}>TRADE LOG ({btResult.result.trades.length} trades)</div>
                    <div style={{ overflowX: "auto", maxHeight: 280, overflowY: "auto" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "monospace" }}>
                        <thead style={{ position: "sticky", top: 0, background: C.surface }}>
                          <tr>
                            {["#", "Entry Date", "Entry Price", "Exit Date", "Exit Price", "P&L %"].map(h => (
                              <th key={h} style={{ padding: "4px 10px", color: C.muted, textAlign: "left", borderBottom: `1px solid ${C.border}`, fontSize: 10, textTransform: "uppercase" }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {btResult.result.trades.map((t, i) => (
                            <tr key={i} style={{ borderBottom: `1px solid ${C.border}22` }}>
                              <td style={{ padding: "5px 10px", color: C.muted }}>{i + 1}</td>
                              <td style={{ padding: "5px 10px", color: C.text }}>{t.date}</td>
                              <td style={{ padding: "5px 10px", color: C.text }}>{Number(t.entry.toFixed(0)).toLocaleString()}</td>
                              <td style={{ padding: "5px 10px", color: C.text }}>{t.exitDate}</td>
                              <td style={{ padding: "5px 10px", color: C.text }}>{Number(t.exit.toFixed(0)).toLocaleString()}</td>
                              <td style={{ padding: "5px 10px", color: parseFloat(t.pnl) >= 0 ? C.green : C.red, fontWeight: 600 }}>{t.pnl > 0 ? "+" : ""}{t.pnl}%</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}

            {!btResult && (
              <div style={{ textAlign: "center", padding: "60px 0", color: C.faint, fontFamily: "monospace" }}>
                <div style={{ fontSize: 32, marginBottom: 12 }}>[ ]</div>
                <div style={{ fontSize: 12 }}>Select ticker and strategy, then run backtest</div>
              </div>
            )}
          </div>
        )}

        {/* CHARTS TAB */}
        {tab === "charts" && (
          <div style={{ maxWidth: 900, margin: "0 auto" }}>
            <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
              <select value={chartTicker} onChange={e => setChartTicker(e.target.value)} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6, padding: "8px 12px", color: C.text, fontSize: 12, fontFamily: "monospace" }}>
                {Object.entries(TICKERS).map(([t, info]) => <option key={t} value={t}>{t} — {info.name}</option>)}
              </select>
              <div style={{ display: "flex", gap: 4 }}>
                {[30, 60, 90, 120].map(p => (
                  <button key={p} onClick={() => setChartPeriod(p)} style={{ background: chartPeriod === p ? C.cyan + "22" : C.surface, border: `1px solid ${chartPeriod === p ? C.cyan : C.border}`, borderRadius: 6, padding: "6px 12px", color: chartPeriod === p ? C.cyan : C.muted, fontSize: 11, cursor: "pointer", fontFamily: "monospace" }}>{p}d</button>
                ))}
              </div>
              <div style={{ marginLeft: "auto", display: "flex", gap: 12, fontSize: 11, fontFamily: "monospace" }}>
                <span style={{ color: C.amber }}>— EMA9</span>
                <span style={{ color: C.cyan }}>— EMA21</span>
                <span style={{ color: C.purple }}>— BB</span>
              </div>
            </div>

            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: 12, marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: C.muted, marginBottom: 8, fontFamily: "monospace" }}>
                {chartTicker} · {TICKERS[chartTicker]?.name} · {chartPeriod}D OHLCV
              </div>
              <CandleChart ticker={chartTicker} period={chartPeriod} />
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
              {Object.entries(TICKERS).map(([t, info]) => {
                const sig = signals.find(s => s.ticker === t);
                const spark = getPrices(t).slice(-20);
                return (
                  <div key={t} onClick={() => setChartTicker(t)} style={{ background: C.surface, border: `1px solid ${chartTicker === t ? C.cyan : C.border}`, borderRadius: 8, padding: "10px 12px", cursor: "pointer", transition: "border-color 0.15s" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                      <span style={{ fontSize: 12, fontWeight: 600, color: C.cyan, fontFamily: "monospace" }}>{t}</span>
                      {sig && <SignalBadge signal={sig.signal} />}
                    </div>
                    <div style={{ fontSize: 11, color: C.muted, marginBottom: 6, fontFamily: "monospace" }}>{sig?.price ? Number(sig.price).toLocaleString() : ""}</div>
                    <SparkLine data={spark} color={sig && parseFloat(sig.change1d) >= 0 ? C.green : C.red} height={32} />
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        ::-webkit-scrollbar{width:4px;height:4px}
        ::-webkit-scrollbar-track{background:${C.bg}}
        ::-webkit-scrollbar-thumb{background:${C.border};border-radius:2px}
        select option{background:${C.surface};color:${C.text}}
        input::placeholder{color:${C.faint}}
        input:focus{border-color:${C.cyanDim}!important}
      `}</style>
    </div>
  );
}

"""
================================================================================
Web Dashboard Module
================================================================================
Serves a browser dashboard on 127.0.0.1:WEB_DASHBOARD_PORT. The engine keeps
running headless; the browser pulls a JSON snapshot once a second and renders:

    * sticky top bar: engine health, live clocks, desk identity
    * price hero: bid/ask/spread + current market session
    * KPI strip: balance, equity, free margin, day P&L, positions, drawdown
    * canvas sparklines for live price and the session equity curve
    * drawdown gauge vs the 6% stop / 7% emergency thresholds
    * open positions table and per-employee scorecard with W/L bars
    * recent desk activity (fills, blocks, faults)

Read-only. Bound to localhost only. All rendering is done in the browser via
textContent and canvas, so event messages can never inject markup.
"""

import json
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque, List, Optional

import MetaTrader5 as mt5

from config import (
    EMPLOYEES,
    EMERGENCY_DD_PCT,
    HARD_DAILY_LOSS_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_DAILY_LOSSES_PER_EMP,
    MAX_DAILY_TRADES_PER_EMP,
    MAX_OPEN_POSITIONS,
    MAX_POSITIONS_PER_EMP,
    MAX_SAME_DIRECTION_POSITIONS,
    MAX_SINGLE_TRADE_RISK_PCT,
    OVERALL_DD_STOP_PCT,
    SYMBOL,
    WEB_DASHBOARD_HOST,
    WEB_DASHBOARD_PORT,
)
from event_log import EventLog

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SLDPAX Desk · Funded Master Terminal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-deep: #050811;
    --bg-surface: #0a0f1d;
    --panel-bg: rgba(13, 20, 36, 0.72);
    --panel-bg-hover: rgba(18, 27, 48, 0.85);
    --panel-border: rgba(255, 255, 255, 0.07);
    --panel-border-glow: rgba(56, 189, 248, 0.22);
    
    --accent: #38bdf8;
    --accent-glow: rgba(56, 189, 248, 0.35);
    --accent-dim: rgba(56, 189, 248, 0.12);
    
    --ok: #10b981;
    --ok-glow: rgba(16, 185, 129, 0.35);
    --ok-bg: rgba(16, 185, 129, 0.12);
    
    --bad: #f43f5e;
    --bad-glow: rgba(244, 63, 94, 0.35);
    --bad-bg: rgba(244, 63, 94, 0.12);
    
    --warn: #f59e0b;
    --warn-glow: rgba(245, 158, 11, 0.35);
    --warn-bg: rgba(245, 158, 11, 0.12);
    
    --purple: #818cf8;
    --purple-bg: rgba(129, 140, 248, 0.12);
    
    --txt: #f1f5f9;
    --txt-dim: #94a3b8;
    --txt-faint: #64748b;
    --txt-subtle: #475569;
    
    --font-ui: "Plus Jakarta Sans", system-ui, -apple-system, sans-serif;
    --font-mono: "JetBrains Mono", "SF Mono", Consolas, monospace;
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --shadow: 0 16px 36px -8px rgba(0, 0, 0, 0.65), 0 0 0 1px rgba(255, 255, 255, 0.05);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  
  body {
    background-color: var(--bg-deep);
    background-image: 
      radial-gradient(1100px 600px at 85% -5%, rgba(56, 189, 248, 0.08) 0%, transparent 60%),
      radial-gradient(900px 500px at 15% 10%, rgba(99, 102, 241, 0.06) 0%, transparent 50%),
      radial-gradient(800px 600px at 50% 105%, rgba(16, 185, 129, 0.04) 0%, transparent 55%);
    background-attachment: fixed;
    color: var(--txt);
    font: 13px/1.5 var(--font-ui);
    padding: 0 20px 48px;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  .num { font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
  .mono { font-family: var(--font-mono); }

  /* ---------- Top Command Bar ---------- */
  .topbar {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; padding: 12px 18px; margin: 0 -20px 16px;
    background: rgba(6, 10, 18, 0.82);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
  }
  
  .topbar-left { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .topbar-right { display: flex; align-items: center; gap: 14px; }

  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-logo {
    display: flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 9px;
    background: linear-gradient(135deg, #0284c7 0%, #6366f1 100%);
    box-shadow: 0 0 16px rgba(56, 189, 248, 0.35);
    color: #fff; font-weight: 800; font-size: 16px;
  }
  .brand-title {
    font-weight: 800; font-size: 16px; letter-spacing: 1.5px;
    background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .brand-tag {
    font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
    background: rgba(56, 189, 248, 0.12); color: var(--accent);
    padding: 3px 8px; border-radius: 6px; border: 1px solid rgba(56, 189, 248, 0.25);
  }

  .health-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 4px 10px; border-radius: 20px;
    background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25);
    font-size: 11px; font-weight: 700; letter-spacing: 0.8px; color: var(--ok);
    transition: all 0.2s ease;
  }
  .health-badge i {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--ok); box-shadow: 0 0 10px var(--ok);
    position: relative;
  }
  .health-badge i::after {
    content: ""; position: absolute; inset: -3px; border-radius: 50%;
    border: 1.5px solid var(--ok); opacity: 0.7; animation: radarPing 2s infinite ease-out;
  }
  @keyframes radarPing {
    0% { transform: scale(0.8); opacity: 1; }
    100% { transform: scale(2.4); opacity: 0; }
  }
  .health-badge.off {
    background: rgba(244, 63, 94, 0.1); border-color: rgba(244, 63, 94, 0.3); color: var(--bad);
  }
  .health-badge.off i { background: var(--bad); box-shadow: 0 0 10px var(--bad); }
  .health-badge.off i::after { display: none; }

  .acct-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 8px;
    background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.06);
    font-size: 11px; font-weight: 600; color: var(--txt-dim); font-family: var(--font-mono);
  }

  .session-capsule {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 20px;
    font-size: 11px; font-weight: 700; letter-spacing: 1px;
    background: rgba(245, 158, 11, 0.1); color: var(--warn);
    border: 1px solid rgba(245, 158, 11, 0.3);
  }
  .session-capsule.london { background: rgba(245, 158, 11, 0.12); color: var(--warn); border-color: rgba(245, 158, 11, 0.3); }
  .session-capsule.ny { background: rgba(16, 185, 129, 0.12); color: var(--ok); border-color: rgba(16, 185, 129, 0.3); }
  .session-capsule.asia { background: rgba(129, 140, 248, 0.12); color: var(--purple); border-color: rgba(129, 140, 248, 0.3); }

  .time-capsule {
    display: flex; align-items: center; gap: 10px;
    padding: 4px 12px; border-radius: 8px;
    background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.06);
  }
  .time-col { display: flex; flex-direction: column; align-items: flex-end; }
  .time-col .t-lbl { font-size: 9px; font-weight: 700; letter-spacing: 1px; color: var(--txt-faint); }
  .time-col .t-val { font-size: 13px; font-weight: 700; color: var(--txt); }

  .ctrl-btn {
    background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08);
    color: var(--txt-dim); padding: 6px 10px; border-radius: 8px; cursor: pointer;
    font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px;
    transition: all 0.2s ease;
  }
  .ctrl-btn:hover { background: rgba(255, 255, 255, 0.08); color: var(--txt); }
  .ctrl-btn.active { background: rgba(56, 189, 248, 0.15); border-color: rgba(56, 189, 248, 0.4); color: var(--accent); }

  /* ---------- Panels & Cards ---------- */
  .panel {
    background: var(--panel-bg);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--panel-border);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow);
    transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
    position: relative;
    overflow: hidden;
  }
  .panel:hover {
    border-color: rgba(255, 255, 255, 0.12);
    box-shadow: 0 20px 42px -8px rgba(0, 0, 0, 0.75), 0 0 0 1px rgba(255, 255, 255, 0.08);
  }

  /* ---------- Hero Layout ---------- */
  .hero {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 14px;
    margin-bottom: 14px;
  }
  
  .ticker-box {
    padding: 20px 22px;
    display: flex; flex-direction: column; justify-content: space-between;
    background: linear-gradient(160deg, rgba(17, 28, 48, 0.85) 0%, rgba(10, 16, 28, 0.8) 100%);
    border: 1px solid rgba(56, 189, 248, 0.18);
    position: relative;
  }
  .ticker-box::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, var(--accent) 0%, var(--purple) 100%);
  }
  .ticker-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .sym-badge {
    font-size: 13px; font-weight: 800; letter-spacing: 2px; color: var(--accent);
    display: flex; align-items: center; gap: 8px;
  }
  .sym-badge span { color: #fff; }
  .sym-gold-icon {
    width: 18px; height: 18px; border-radius: 50%;
    background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%);
    box-shadow: 0 0 8px rgba(251, 191, 36, 0.4);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; color: #000; font-weight: 900;
  }

  .price-display {
    font-size: 46px; font-weight: 800; letter-spacing: 0.5px; line-height: 1;
    margin: 8px 0 12px; font-family: var(--font-mono); color: #fff;
    transition: color 0.3s ease, text-shadow 0.3s ease;
  }
  .price-display.up { animation: flashUp 0.7s ease-out; }
  .price-display.down { animation: flashDown 0.7s ease-out; }
  @keyframes flashUp {
    0% { color: var(--ok); text-shadow: 0 0 24px var(--ok-glow); }
    100% { color: #fff; }
  }
  @keyframes flashDown {
    0% { color: var(--bad); text-shadow: 0 0 24px var(--bad-glow); }
    100% { color: #fff; }
  }

  .ticker-sub {
    display: flex; align-items: center; justify-content: space-between;
    padding-top: 12px; border-top: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 12px; color: var(--txt-dim);
  }
  .ticker-sub-item { display: flex; align-items: center; gap: 6px; }
  .ticker-sub-item b { color: var(--txt); font-family: var(--font-mono); font-weight: 600; }

  .spr-badge {
    padding: 2px 8px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
  }
  .spr-tight { background: rgba(16, 185, 129, 0.12); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.25); }
  .spr-normal { background: rgba(56, 189, 248, 0.12); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.25); }
  .spr-wide { background: rgba(245, 158, 11, 0.15); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.3); }

  /* ---------- KPI Grid ---------- */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
  }
  .kpi-card {
    padding: 16px 18px;
    display: flex; flex-direction: column; justify-content: space-between;
    position: relative;
  }
  .kpi-card::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: var(--accent); opacity: 0.8;
  }
  .kpi-card.c-ok::before { background: var(--ok); }
  .kpi-card.c-warn::before { background: var(--warn); }
  .kpi-card.c-bad::before { background: var(--bad); }
  .kpi-card.c-purple::before { background: var(--purple); }

  .kpi-header {
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;
  }
  .kpi-lbl {
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--txt-faint);
  }
  .kpi-icon { font-size: 13px; opacity: 0.7; }
  .kpi-val {
    font-size: 22px; font-weight: 700; line-height: 1.2;
    color: var(--txt); margin-bottom: 4px;
  }
  .kpi-aux {
    font-size: 11px; font-weight: 500; color: var(--txt-dim);
    display: flex; align-items: center; gap: 6px;
  }
  .pos { color: var(--ok) !important; }
  .neg { color: var(--bad) !important; }

  /* ---------- Charts Row ---------- */
  .chartrow {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    margin-bottom: 14px;
  }
  .chart-panel { padding: 18px 20px; }
  .chart-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
  }
  .chart-title {
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--txt-dim); display: flex; align-items: center; gap: 8px;
  }
  .chart-title i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
  .chart-badge {
    font-size: 10px; font-weight: 700; letter-spacing: 0.8px; padding: 2px 7px;
    border-radius: 6px; background: rgba(255, 255, 255, 0.04); color: var(--txt-dim);
  }
  .chart-canvas-wrap {
    position: relative; width: 100%; height: 110px;
  }
  canvas { display: block; width: 100%; height: 100%; cursor: crosshair; }

  /* ---------- Two Columns (Drawdown & Positions) ---------- */
  .twocol {
    display: grid;
    grid-template-columns: 1.15fr 0.85fr;
    gap: 14px;
    margin-bottom: 14px;
  }
  .section { padding: 18px 20px; }
  .section-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }
  .section h2 {
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.8px;
    color: var(--txt-dim); display: flex; align-items: center; gap: 8px;
  }

  /* Drawdown Protection Metrics */
  .kv-list { display: flex; flex-direction: column; gap: 8px; }
  .kv-row {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12px; padding: 3px 0;
  }
  .kv-row span { color: var(--txt-dim); font-weight: 500; }
  .kv-row b { color: var(--txt); font-weight: 600; font-family: var(--font-mono); }

  .ddgauge { margin: 12px 0 14px; }
  .ddbar-track {
    height: 12px; border-radius: 6px; background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.07); overflow: hidden; position: relative;
  }
  .ddbar-fill {
    position: absolute; inset: 0 auto 0 0; border-radius: 5px;
    background: linear-gradient(90deg, var(--ok) 0%, var(--warn) 75%, var(--bad) 100%);
    box-shadow: 0 0 12px rgba(16, 185, 129, 0.3);
    transition: width 0.4s ease;
  }
  .dd-markers {
    position: relative; height: 16px; margin-top: 5px;
    display: flex; justify-content: space-between;
    font-size: 9px; font-weight: 700; color: var(--txt-faint); letter-spacing: 1px;
  }
  .dd-markers span { position: relative; }
  .dd-pin-stop { color: var(--warn); }
  .dd-pin-emerg { color: var(--bad); }

  /* ---------- Positions Deck & Empty State ---------- */
  .empty-state {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 32px 16px; text-align: center;
  }
  .radar-scanner {
    width: 54px; height: 54px; border-radius: 50%;
    border: 1.5px solid rgba(56, 189, 248, 0.25);
    position: relative; margin-bottom: 12px;
    display: flex; align-items: center; justify-content: center;
  }
  .radar-scanner::before {
    content: ""; position: absolute; inset: 6px; border-radius: 50%;
    border: 1px dashed rgba(56, 189, 248, 0.3);
  }
  .radar-sweep {
    position: absolute; inset: 0; border-radius: 50%;
    background: conic-gradient(from 0deg, transparent 0deg, rgba(56, 189, 248, 0.3) 60deg, transparent 60.1deg);
    animation: sweep 3s linear infinite;
  }
  .radar-center-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 8px var(--accent); z-index: 2;
  }
  @keyframes sweep { to { transform: rotate(360deg); } }

  .empty-title { font-size: 12px; font-weight: 700; letter-spacing: 1px; color: var(--txt-dim); }
  .empty-sub { font-size: 11px; color: var(--txt-faint); max-width: 260px; margin-top: 4px; }

  /* ---------- Tables ---------- */
  .table-responsive { width: 100%; overflow-x: auto; }
  table { width: 100%; border-collapse: separate; border-spacing: 0; }
  th {
    text-align: left; color: var(--txt-faint); font-weight: 700; font-size: 10px;
    text-transform: uppercase; letter-spacing: 1.2px; padding: 8px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08); background: rgba(0, 0, 0, 0.15);
  }
  td {
    padding: 9px 10px; border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    font-size: 12px; color: var(--txt); transition: background 0.15s ease;
  }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: rgba(255, 255, 255, 0.025); }
  .num { text-align: right; }

  /* Badges & Chips */
  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 8px; border-radius: 6px;
    font-weight: 700; font-size: 10px; letter-spacing: 0.5px;
  }
  .b-ok { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.25); }
  .b-warn { background: var(--warn-bg); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.25); }
  .b-bad { background: var(--bad-bg); color: var(--bad); border: 1px solid rgba(244, 63, 94, 0.25); }
  .b-dim { background: rgba(255, 255, 255, 0.05); color: var(--txt-dim); border: 1px solid rgba(255, 255, 255, 0.08); }
  .b-purple { background: var(--purple-bg); color: var(--purple); border: 1px solid rgba(129, 140, 248, 0.25); }

  .tf-pill {
    padding: 2px 6px; border-radius: 4px; font-weight: 700; font-size: 10px; font-family: var(--font-mono);
  }
  .tf-M5 { background: rgba(56, 189, 248, 0.12); color: var(--accent); }
  .tf-M15 { background: rgba(129, 140, 248, 0.12); color: var(--purple); }
  .tf-M30 { background: rgba(245, 158, 11, 0.12); color: var(--warn); }

  .side-pill {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 7px; border-radius: 5px; font-weight: 700; font-size: 11px; font-family: var(--font-mono);
  }
  .side-BUY { background: rgba(16, 185, 129, 0.12); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.3); }
  .side-SELL { background: rgba(244, 63, 94, 0.12); color: var(--bad); border: 1px solid rgba(244, 63, 94, 0.3); }

  .wl {
    width: 64px; height: 6px; border-radius: 3px;
    background: rgba(255, 255, 255, 0.06); overflow: hidden;
    display: inline-flex; vertical-align: middle;
  }
  .wl i { display: block; height: 100%; transition: width 0.3s ease; }
  .wl .wn { background: var(--ok); box-shadow: 0 0 6px var(--ok-glow); }
  .wl .ln { background: var(--bad); }

  /* ---------- Audit Activity Feed ---------- */
  .feed-tabs { display: flex; align-items: center; gap: 6px; }
  .tab-btn {
    padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.8px;
    background: transparent; border: 1px solid transparent; color: var(--txt-faint); cursor: pointer;
    transition: all 0.15s ease;
  }
  .tab-btn:hover { color: var(--txt-dim); }
  .tab-btn.active {
    background: rgba(255, 255, 255, 0.06); border-color: rgba(255, 255, 255, 0.1); color: var(--txt);
  }

  .events-wrap { max-height: 220px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.1) transparent; }
  .events-wrap::-webkit-scrollbar { width: 5px; }
  .events-wrap::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }

  .events { list-style: none; display: flex; flex-direction: column; gap: 4px; padding-top: 4px; }
  .events li {
    display: flex; align-items: center; gap: 10px; padding: 6px 8px;
    border-radius: 6px; background: rgba(255, 255, 255, 0.015);
    border: 1px solid rgba(255, 255, 255, 0.03); font-size: 12px;
    transition: background 0.15s ease;
  }
  .events li:hover { background: rgba(255, 255, 255, 0.04); }
  .events .t { color: var(--txt-faint); font-family: var(--font-mono); font-size: 11px; white-space: nowrap; }
  .events .tag {
    flex: 0 0 54px; text-align: center; font-weight: 800; font-size: 9px;
    letter-spacing: 1px; padding: 2px 4px; border-radius: 4px;
  }
  .ev-TRADE .tag { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.25); }
  .ev-BLOCK .tag { background: var(--warn-bg); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.25); }
  .ev-ERR .tag { background: var(--bad-bg); color: var(--bad); border: 1px solid rgba(244, 63, 94, 0.25); }
  .ev-INFO .tag { background: rgba(255, 255, 255, 0.05); color: var(--txt-dim); }
  .events .msg { color: var(--txt); word-break: break-word; font-family: var(--font-mono); font-size: 11px; }
  .events li.empty { justify-content: center; color: var(--txt-faint); font-style: italic; border-style: dashed; }

  /* ---------- Footer ---------- */
  .footer {
    margin-top: 20px; padding: 12px 16px; border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.04);
    color: var(--txt-faint); font-size: 11px; text-align: center; letter-spacing: 0.3px;
  }
  .footer b { color: var(--txt-dim); }

  @media (max-width: 1024px) {
    .hero { grid-template-columns: 1fr; }
    .twocol { grid-template-columns: 1fr; }
    .chartrow { grid-template-columns: 1fr; }
  }

  /* ---------- Audio Hub & Modal ---------- */
  .audio-ctrl-group { display: flex; align-items: center; gap: 6px; }
  .audio-modal-backdrop {
    position: fixed; inset: 0; background: rgba(5, 8, 17, 0.78);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    z-index: 10000; display: none; align-items: center; justify-content: center;
    padding: 16px; opacity: 0; transition: opacity 0.2s ease;
  }
  .audio-modal-backdrop.open { display: flex; opacity: 1; }
  
  .audio-modal {
    width: 100%; max-width: 520px; border-radius: var(--radius-lg);
    background: linear-gradient(165deg, rgba(18, 28, 48, 0.96) 0%, rgba(10, 16, 28, 0.98) 100%);
    border: 1px solid rgba(56, 189, 248, 0.25);
    box-shadow: 0 25px 60px -10px rgba(0, 0, 0, 0.8), 0 0 30px rgba(56, 189, 248, 0.15);
    overflow: hidden; transform: scale(0.95); transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .audio-modal-backdrop.open .audio-modal { transform: scale(1); }

  .modal-head {
    padding: 16px 20px; border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(255, 255, 255, 0.02);
  }
  .modal-head h3 {
    font-size: 13px; font-weight: 800; letter-spacing: 1px; color: #fff;
    display: flex; align-items: center; gap: 10px;
  }
  .modal-close-btn {
    background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1);
    color: var(--txt-dim); width: 28px; height: 28px; border-radius: 8px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    font-size: 14px; transition: all 0.15s ease;
  }
  .modal-close-btn:hover { background: rgba(255, 255, 255, 0.12); color: #fff; }

  .modal-body { padding: 20px; display: flex; flex-direction: column; gap: 14px; }

  .audio-setting-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.025); border: 1px solid rgba(255, 255, 255, 0.05);
  }
  .audio-setting-meta b { display: block; font-size: 12px; color: var(--txt); margin-bottom: 2px; }
  .audio-setting-meta span { font-size: 11px; color: var(--txt-faint); }

  .volume-slider-wrap { display: flex; align-items: center; gap: 12px; }
  .vol-slider {
    -webkit-appearance: none; appearance: none; width: 130px; height: 6px;
    border-radius: 3px; background: rgba(255, 255, 255, 0.12); outline: none; cursor: pointer;
  }
  .vol-slider::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; width: 16px; height: 16px;
    border-radius: 50%; background: var(--accent); box-shadow: 0 0 10px var(--accent);
    cursor: pointer;
  }
  .vol-val { font-family: var(--font-mono); font-size: 11px; font-weight: 700; width: 34px; text-align: right; color: var(--accent); }

  .preview-grid { display: flex; flex-direction: column; gap: 8px; margin-top: 4px; }
  .preview-card {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05);
    transition: all 0.15s ease;
  }
  .preview-card:hover {
    background: rgba(255, 255, 255, 0.04); border-color: rgba(255, 255, 255, 0.1);
  }
  .preview-meta { display: flex; align-items: center; gap: 10px; }
  .preview-icon-box {
    width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 14px;
  }
  .p-icon-entry { background: rgba(16, 185, 129, 0.12); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.3); }
  .p-icon-win   { background: rgba(56, 189, 248, 0.12); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); }
  .p-icon-loss  { background: rgba(245, 158, 11, 0.12); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.3); }
  .p-icon-risk  { background: rgba(244, 63, 94, 0.12); color: var(--bad); border: 1px solid rgba(244, 63, 94, 0.3); }

  .preview-text b { display: block; font-size: 12px; color: var(--txt); }
  .preview-text span { font-size: 10px; color: var(--txt-dim); }

  .preview-play-btn {
    background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1);
    color: var(--txt); padding: 5px 12px; border-radius: 6px; cursor: pointer;
    font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
    display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease;
  }
  .preview-play-btn:hover {
    background: rgba(56, 189, 248, 0.18); border-color: rgba(56, 189, 248, 0.4); color: var(--accent);
    transform: translateY(-1px);
  }
  .preview-play-btn:active { transform: translateY(0); }

  /* Visual Soundwave Indicator */
  .soundwave-box {
    display: inline-flex; align-items: center; justify-content: center; gap: 2.5px; height: 14px;
  }
  .soundwave-bar {
    width: 2.5px; height: 3px; border-radius: 2px; background: var(--accent);
    transition: height 0.1s ease;
  }
  .soundwave-box.anim .soundwave-bar:nth-child(1) { animation: wave 0.35s infinite ease-in-out alternate; }
  .soundwave-box.anim .soundwave-bar:nth-child(2) { animation: wave 0.35s infinite ease-in-out 0.08s alternate; }
  .soundwave-box.anim .soundwave-bar:nth-child(3) { animation: wave 0.35s infinite ease-in-out 0.16s alternate; }
  .soundwave-box.anim .soundwave-bar:nth-child(4) { animation: wave 0.35s infinite ease-in-out 0.12s alternate; }
  .soundwave-box.anim .soundwave-bar:nth-child(5) { animation: wave 0.35s infinite ease-in-out 0.04s alternate; }
  @keyframes wave { 0% { height: 3px; } 100% { height: 14px; } }

  /* ---------- HUD Toast Notifications ---------- */
  .hud-toasts {
    position: fixed; top: 68px; right: 20px; z-index: 10000;
    display: flex; flex-direction: column; gap: 8px; pointer-events: none;
  }
  .hud-toast {
    pointer-events: auto; width: 320px; padding: 12px 14px; border-radius: 10px;
    background: rgba(14, 22, 38, 0.95); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: 0 14px 30px rgba(0, 0, 0, 0.6), 0 0 16px rgba(56, 189, 248, 0.15);
    display: flex; align-items: center; gap: 12px;
    animation: toastIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    transition: all 0.3s ease;
  }
  .hud-toast.fade-out { opacity: 0; transform: translateX(40px); }
  @keyframes toastIn {
    0% { opacity: 0; transform: translateX(40px) scale(0.95); }
    100% { opacity: 1; transform: translateX(0) scale(1); }
  }
  .hud-toast-icon {
    width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 15px;
  }
  .hud-toast.toast-ok { border-color: rgba(16, 185, 129, 0.4); }
  .hud-toast.toast-ok .hud-toast-icon { background: rgba(16, 185, 129, 0.15); color: var(--ok); }
  .hud-toast.toast-warn { border-color: rgba(245, 158, 11, 0.4); }
  .hud-toast.toast-warn .hud-toast-icon { background: rgba(245, 158, 11, 0.15); color: var(--warn); }
  .hud-toast.toast-bad { border-color: rgba(244, 63, 94, 0.4); }
  .hud-toast.toast-bad .hud-toast-icon { background: rgba(244, 63, 94, 0.15); color: var(--bad); }

  .hud-toast-content { flex: 1; }
  .hud-toast-title { font-size: 11px; font-weight: 800; letter-spacing: 0.8px; color: #fff; }
  .hud-toast-desc { font-size: 11px; color: var(--txt-dim); font-family: var(--font-mono); margin-top: 2px; }
  /* ---------- Audio Test & Preview Bar (On-Dashboard Live Test) ---------- */
  .audio-test-bar {
    display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;
    padding: 8px 14px; margin-bottom: 14px; border-radius: var(--radius-sm);
    background: linear-gradient(90deg, rgba(16, 26, 46, 0.85) 0%, rgba(12, 19, 34, 0.8) 100%);
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(56, 189, 248, 0.22);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
  }
  .audio-test-label {
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; font-weight: 800; letter-spacing: 1px; color: var(--accent);
  }
  .audio-test-buttons {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .test-pill-btn {
    padding: 5px 12px; border-radius: 6px; font-size: 11px; font-weight: 700;
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08);
    color: var(--txt);
  }
  .test-pill-btn span { font-size: 9px; opacity: 0.8; }
  .test-pill-btn:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  }
  .test-pill-btn:active { transform: translateY(0); }
  .test-pill-btn.test-entry:hover {
    background: rgba(16, 185, 129, 0.16); border-color: rgba(16, 185, 129, 0.45); color: var(--ok);
  }
  .test-pill-btn.test-win:hover {
    background: rgba(56, 189, 248, 0.16); border-color: rgba(56, 189, 248, 0.45); color: var(--accent);
  }
  .test-pill-btn.test-loss:hover {
    background: rgba(245, 158, 11, 0.16); border-color: rgba(245, 158, 11, 0.45); color: var(--warn);
  }
  .test-pill-btn.test-risk:hover {
    background: rgba(244, 63, 94, 0.16); border-color: rgba(244, 63, 94, 0.45); color: var(--bad);
  }

  .audio-test-vol {
    display: flex; align-items: center; gap: 8px; margin-left: auto;
  }
  .vol-slider-mini {
    -webkit-appearance: none; appearance: none; width: 80px; height: 4px;
    border-radius: 2px; background: rgba(255, 255, 255, 0.14); outline: none; cursor: pointer;
  }
  .vol-slider-mini::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none; width: 13px; height: 13px;
    border-radius: 50%; background: var(--accent); box-shadow: 0 0 6px var(--accent);
    cursor: pointer;
  }
  .ctrl-btn-mini {
    background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08);
    color: var(--txt-dim); padding: 4px 8px; border-radius: 6px; cursor: pointer;
    font-size: 11px; display: inline-flex; align-items: center; justify-content: center;
    transition: all 0.2s ease;
  }
  .ctrl-btn-mini:hover { background: rgba(255, 255, 255, 0.08); color: #fff; }
  .ctrl-btn-mini.active { background: rgba(56, 189, 248, 0.15); border-color: rgba(56, 189, 248, 0.4); color: var(--accent); }
</style>
</head>
<body>

<div class="hud-toasts" id="hud_toasts"></div>

<!-- Audio Preview & Settings Modal -->
<div class="audio-modal-backdrop" id="audio_modal_backdrop" onclick="if(event.target===this)closeAudioModal()">
  <div class="audio-modal">
    <div class="modal-head">
      <h3>
        <span>🔊</span> Audio Alerts &amp; Sound Previews
        <div class="soundwave-box" id="modal_soundwave">
          <span class="soundwave-bar"></span>
          <span class="soundwave-bar"></span>
          <span class="soundwave-bar"></span>
          <span class="soundwave-bar"></span>
          <span class="soundwave-bar"></span>
        </div>
      </h3>
      <button class="modal-close-btn" onclick="closeAudioModal()" title="Close">✕</button>
    </div>
    
    <div class="modal-body">
      <div class="audio-setting-row">
        <div class="audio-setting-meta">
          <b>Master Sound Alerts</b>
          <span>Enable synthesized Web Audio notifications</span>
        </div>
        <button class="ctrl-btn" id="modal_audio_toggle_btn" onclick="toggleAudio()">
          <span id="modal_audio_icon">🔊</span> <span id="modal_audio_status">ENABLED</span>
        </button>
      </div>

      <div class="audio-setting-row">
        <div class="audio-setting-meta">
          <b>Spoken Voice Alerts (TTS)</b>
          <span>Announce "Trade executed", "Trade exited", and profit verbally</span>
        </div>
        <button class="ctrl-btn" id="modal_voice_toggle_btn" onclick="toggleVoice()">
          <span id="modal_voice_icon">🗣️</span> <span id="modal_voice_status">VOICE ON</span>
        </button>
      </div>

      <div class="audio-setting-row">
        <div class="audio-setting-meta">
          <b>Notification Volume</b>
          <span>Master gain level for all alerts</span>
        </div>
        <div class="volume-slider-wrap">
          <input type="range" class="vol-slider" id="vol_slider" min="0.05" max="1.0" step="0.05" value="0.7" oninput="setVolume(this.value)">
          <span class="vol-val" id="vol_disp">70%</span>
        </div>
      </div>

      <div class="preview-grid">
        <div class="preview-card">
          <div class="preview-meta">
            <div class="preview-icon-box p-icon-entry">⚡</div>
            <div class="preview-text">
              <b>Trade Execution (Entry)</b>
              <span>Ascending triad chime + "Trade executed"</span>
            </div>
          </div>
          <button class="preview-play-btn" onclick="previewAudio('EXECUTION')">
            ▶ Play Preview
          </button>
        </div>

        <div class="preview-card">
          <div class="preview-meta">
            <div class="preview-icon-box p-icon-win">💎</div>
            <div class="preview-text">
              <b>Trade Exit · Profit / TP</b>
              <span>Celebratory bell chime + "Trade exited, profit booked"</span>
            </div>
          </div>
          <button class="preview-play-btn" onclick="previewAudio('EXIT_WIN')">
            ▶ Play Preview
          </button>
        </div>

        <div class="preview-card">
          <div class="preview-meta">
            <div class="preview-icon-box p-icon-loss">🏁</div>
            <div class="preview-text">
              <b>Trade Exit · Loss / Flat</b>
              <span>Resolving tone + "Trade exited, position closed"</span>
            </div>
          </div>
          <button class="preview-play-btn" onclick="previewAudio('EXIT_LOSS')">
            ▶ Play Preview
          </button>
        </div>

        <div class="preview-card">
          <div class="preview-meta">
            <div class="preview-icon-box p-icon-risk">⚠️</div>
            <div class="preview-text">
              <b>Risk Limit / Circuit Guard</b>
              <span>Caution alert + "Caution: risk threshold reached"</span>
            </div>
          </div>
          <button class="preview-play-btn" onclick="previewAudio('RISK')">
            ▶ Play Preview
          </button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="topbar">
  <div class="topbar-left">
    <div class="brand">
      <div class="brand-logo">⚡</div>
      <div class="brand-title">SLDPAX</div>
      <div class="brand-tag">FUNDED MASTER DESK</div>
    </div>
    <div class="health-badge" id="health">
      <i></i><span id="health_txt">ONLINE</span>
    </div>
    <div class="acct-chip" id="acct_tag">CONNECTING...</div>
  </div>
  
  <div class="topbar-right">
    <div class="session-capsule" id="session">SCANNING</div>
    <div class="time-capsule">
      <div class="time-col">
        <span class="t-lbl">LOCAL</span>
        <span class="t-val num" id="clock">--:--:--</span>
      </div>
      <div style="width:1px;height:18px;background:rgba(255,255,255,0.1)"></div>
      <div class="time-col">
        <span class="t-lbl">UTC</span>
        <span class="t-val num" id="utc">--:--:--</span>
      </div>
    </div>

    <div class="audio-ctrl-group">
      <button class="ctrl-btn" id="audio_btn" onclick="toggleAudio()" title="Sound Notifications: ON / OFF">
        <span id="audio_icon">🔊</span> <span id="audio_status_lbl">AUDIO ON</span>
      </button>
      <button class="ctrl-btn" onclick="openAudioModal()" title="Open Audio Alerts & Sound Preview Panel">
        🎧 PREVIEW
      </button>
    </div>
  </div>
</div>

<!-- Prominent On-Dashboard Audio Test & Live Preview Strip -->
<div class="audio-test-bar">
  <div class="audio-test-label">
    <div class="soundwave-box" id="testbar_soundwave">
      <span class="soundwave-bar"></span>
      <span class="soundwave-bar"></span>
      <span class="soundwave-bar"></span>
      <span class="soundwave-bar"></span>
      <span class="soundwave-bar"></span>
    </div>
    <span>AUDIO TEST &amp; PREVIEW:</span>
  </div>
  
  <div class="audio-test-buttons">
    <button class="test-pill-btn test-entry" onclick="previewAudio('EXECUTION')" title="Test Trade Execution (Entry) Voice & Sound">
      <span>▶</span> ⚡ Trade Entry
    </button>
    <button class="test-pill-btn test-win" onclick="previewAudio('EXIT_WIN')" title="Test Trade Exit Take Profit Voice & Sound">
      <span>▶</span> 💎 Exit (TP / Win)
    </button>
    <button class="test-pill-btn test-loss" onclick="previewAudio('EXIT_LOSS')" title="Test Trade Exit Stop Loss Voice & Sound">
      <span>▶</span> 🏁 Exit (SL / Loss)
    </button>
    <button class="test-pill-btn test-risk" onclick="previewAudio('RISK')" title="Test Risk Limit Guard Voice & Sound">
      <span>▶</span> ⚠️ Risk Alert
    </button>
    <button class="test-pill-btn" id="voice_toggle_btn" onclick="toggleVoice()" title="Toggle Spoken Voice Alerts (Speech Synthesis)">
      <span id="voice_icon">🗣️</span> <span id="voice_status_lbl">VOICE ON</span>
    </button>
  </div>

  <div class="audio-test-vol">
    <span class="num mono" style="font-size:10px;color:var(--txt-faint)">VOL</span>
    <input type="range" class="vol-slider-mini" id="vol_slider_mini" min="0.05" max="1.0" step="0.05" value="0.7" oninput="setVolume(this.value)" title="Adjust Alert Volume">
    <span class="num mono" id="vol_disp_mini" style="font-size:11px;font-weight:700;color:var(--accent);width:28px">70%</span>
    <button class="ctrl-btn-mini active" id="audio_toggle_mini" onclick="toggleAudio()" title="Mute/Unmute Audio">
      <span id="audio_icon_mini">🔊</span>
    </button>
  </div>
</div>

<div class="hero">
  <div class="panel ticker-box">
    <div>
      <div class="ticker-head">
        <div class="sym-badge" id="sym">
          <div class="sym-gold-icon">G</div>
          <span>XAUUSD</span> · SPOT GOLD
        </div>
        <div id="netting"></div>
      </div>
      <div class="price-display num" id="bid">—</div>
    </div>
    
    <div class="ticker-sub">
      <div class="ticker-sub-item">
        <span>ASK</span>
        <b class="num" id="ask">—</b>
      </div>
      <div class="ticker-sub-item">
        <span>SPREAD</span>
        <span class="spr-badge spr-normal num" id="spread">—</span>
      </div>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="panel kpi-card c-purple">
      <div class="kpi-header"><span class="kpi-lbl">Balance</span><span class="kpi-icon">💼</span></div>
      <div class="kpi-val num" id="k_balance">—</div>
      <div class="kpi-aux" id="k_balance_aux"></div>
    </div>
    <div class="panel kpi-card">
      <div class="kpi-header"><span class="kpi-lbl">Equity</span><span class="kpi-icon">💎</span></div>
      <div class="kpi-val num" id="k_equity">—</div>
      <div class="kpi-aux" id="k_equity_aux"></div>
    </div>
    <div class="panel kpi-card">
      <div class="kpi-header"><span class="kpi-lbl">Free Margin</span><span class="kpi-icon">🛡️</span></div>
      <div class="kpi-val num" id="k_margin">—</div>
      <div class="kpi-aux" id="k_margin_aux"></div>
    </div>
    <div class="panel kpi-card c-ok" id="card_daypnl">
      <div class="kpi-header"><span class="kpi-lbl">Day P&amp;L</span><span class="kpi-icon">📈</span></div>
      <div class="kpi-val num" id="k_daypnl">—</div>
      <div class="kpi-aux" id="k_daypnl_aux"></div>
    </div>
    <div class="panel kpi-card">
      <div class="kpi-header"><span class="kpi-lbl">Positions</span><span class="kpi-icon">🎯</span></div>
      <div class="kpi-val num" id="k_pos">—</div>
      <div class="kpi-aux" id="k_pos_aux"></div>
    </div>
    <div class="panel kpi-card c-bad" id="card_dd">
      <div class="kpi-header"><span class="kpi-lbl">Drawdown</span><span class="kpi-icon">⚠️</span></div>
      <div class="kpi-val num" id="k_dd">—</div>
      <div class="kpi-aux" id="k_dd_aux"></div>
    </div>
  </div>
</div>

<div class="chartrow">
  <div class="panel chart-panel">
    <div class="chart-head">
      <div class="chart-title"><i></i> Price Telemetry · Last 3 Minutes</div>
      <span class="chart-badge">TICK FEED</span>
    </div>
    <div class="chart-canvas-wrap">
      <canvas id="c_price"></canvas>
    </div>
  </div>
  
  <div class="panel chart-panel">
    <div class="chart-head">
      <div class="chart-title"><i style="background:var(--ok)"></i> Equity Curve · Session Realtime</div>
      <span class="chart-badge">MOMENTUM</span>
    </div>
    <div class="chart-canvas-wrap">
      <canvas id="c_equity"></canvas>
    </div>
  </div>
</div>

<div class="twocol">
  <div class="panel section">
    <div class="section-head">
      <h2>Drawdown &amp; Risk Protection</h2>
      <span id="s_status"></span>
    </div>
    
    <div class="kv-list">
      <div class="kv-row"><span>Peak Equity</span><b class="num" id="dd_peak">—</b></div>
      <div class="kv-row"><span>Current Drawdown</span><b class="num" id="dd_now">—</b></div>
    </div>

    <div class="ddgauge">
      <div class="ddbar-track">
        <div class="ddbar-fill" id="ddbar_fill" style="width:0%"></div>
      </div>
      <div class="dd-markers">
        <span>0.0% BASE</span>
        <span class="dd-pin-stop" id="dd_stoplbl">STOP 6.0%</span>
        <span class="dd-pin-emerg" id="dd_emerglbl">EMERGENCY 7.0%</span>
      </div>
    </div>

    <!-- Hidden element kept for strict backward compatibility with existing reload() bindings -->
    <div id="ddbar" style="display:none"></div>

    <div class="kv-list" style="margin-top:6px">
      <div class="kv-row"><span>Latches Held</span><b id="s_latches" class="num">none</b></div>
      <div class="kv-row"><span>Operating Risk / Trade</span><b id="dd_risk" class="num">—</b></div>
      <div class="kv-row"><span>Desk Position Limits</span><b id="dd_cap" class="num">—</b></div>
    </div>
  </div>

  <div class="panel section">
    <div class="section-head">
      <h2>Active Positions</h2>
      <span class="badge b-dim" id="pos_count_chip">0 ACTIVE</span>
    </div>
    
    <div id="positions" class="empty-state">
      <div class="radar-scanner">
        <div class="radar-sweep"></div>
        <div class="radar-center-dot"></div>
      </div>
      <div class="empty-title">NO ACTIVE EXPOSURE</div>
      <div class="empty-sub">Desk scanning 8 algorithmic strategies across M5, M15, and M30 for high-probability setups</div>
    </div>

    <div id="pos_table" style="display:none" class="table-responsive">
      <table>
        <thead>
          <tr>
            <th>Desk</th>
            <th>Strategy</th>
            <th>Side</th>
            <th class="num">Lots</th>
            <th class="num">SL</th>
            <th class="num">TP</th>
            <th class="num">Float</th>
          </tr>
        </thead>
        <tbody id="positions_body"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="panel section" style="margin-bottom:14px">
  <div class="section-head">
    <h2>Algorithmic Employee Scorecard · Today's Session</h2>
    <span class="badge b-dim">8 CONCURRENT STRATEGIES</span>
  </div>
  <div class="table-responsive">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Strategy Desk</th>
          <th>TF</th>
          <th>Target R:R</th>
          <th>Status</th>
          <th>W / L Distribution</th>
          <th class="num">Trades / Cap</th>
          <th class="num">Win Rate</th>
        </tr>
      </thead>
      <tbody id="roster"></tbody>
    </table>
  </div>
</div>

<div class="panel section">
  <div class="section-head">
    <h2>Desk Audit &amp; Event Log</h2>
    <div class="feed-tabs">
      <button class="tab-btn active" onclick="setLogFilter('ALL')">ALL</button>
      <button class="tab-btn" onclick="setLogFilter('TRADE')">TRADES</button>
      <button class="tab-btn" onclick="setLogFilter('BLOCK')">BLOCKS</button>
      <button class="tab-btn" onclick="setLogFilter('ERR')">ERRORS</button>
    </div>
  </div>
  <div class="events-wrap">
    <ul class="events" id="events">
      <li class="empty"><span class="msg">Connecting to engine activity stream...</span></li>
    </ul>
  </div>
</div>

<div class="footer">
  SLDPAX Autonomous Desk · Operating on connected MetaTrader 5 account. Positions are <b>never closed automatically</b> on normal engine shutdown.
  The <b>7.0% emergency cutoff</b> is the only automated condition that liquidates the entire book. Press <b>Ctrl+C</b> in the console to halt safely.
</div>

<script>
const fmt = n => (n===null||n===undefined) ? "—" : Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtPct = n => (n===null||n===undefined) ? "—" : Number(n).toFixed(2) + "%";
const sgn = n => (n>0?"+":"") + fmt(n);

function badge(text, kind){ const s=document.createElement("span"); s.className="badge "+kind; s.textContent=text; return s; }

function statusFrame(st){
  if (st.emergency)  return badge("EMERGENCY HALT","b-bad");
  if (st.overall_dd) return badge("DD PROTECTION","b-bad");
  if (st.hard)       return badge("HARD CUTOFF","b-bad");
  if (st.breaker)    return badge("CIRCUIT BREAKER","b-warn");
  return badge("ACTIVE · ARMED","b-ok");
}

function empStatus(e, st){
  if (st.halted) return badge("Halted","b-warn");
  if (e.losses >= e.max_losses) return badge("Benched (1L)","b-bad");
  if (e.trades >= e.max_trades) return badge("Cap Hit","b-warn");
  if (e.in_trade) return badge("In Trade","b-ok");
  return badge("Scanning","b-dim");
}

let lastBid = null;
function setBid(b){
  const el = document.getElementById("bid");
  el.textContent = fmt(b);
  el.classList.remove("up","down");
  if (lastBid !== null && b !== null){
    if (b > lastBid) {
      el.classList.add("up");
    } else if (b < lastBid) {
      el.classList.add("down");
    }
  }
  if (b !== null) lastBid = b;
}

function sessionFor(h){
  if (h >= 7 && h < 14) return ["LONDON SESSION", "london"];
  if (h >= 14 && h < 21) return ["NEW YORK SESSION", "ny"];
  if (h >= 21 || h < 7) return ["ASIA SESSION", "asia"];
  return ["—", ""];
}

// ============================================================
// Web Audio & Spoken Voice Alert Engine (TTS + Synthesizer)
// ============================================================
let audioEnabled = localStorage.getItem("sldpax_audio") !== "false";
let voiceEnabled = localStorage.getItem("sldpax_voice") !== "false";
let audioVolume = parseFloat(localStorage.getItem("sldpax_vol") || "0.7");
let audioCtx = null;

function ensureAudioCtx() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === "suspended") {
    audioCtx.resume();
  }
  return audioCtx;
}

// Spoken Voice Alert Engine (Browser Web Speech API)
function toggleVoice() {
  voiceEnabled = !voiceEnabled;
  localStorage.setItem("sldpax_voice", voiceEnabled ? "true" : "false");
  updateAudioUI();
  if (voiceEnabled) {
    speak("Voice alerts enabled");
  }
}

function speak(text) {
  if (!audioEnabled || !voiceEnabled) return;
  if (!('speechSynthesis' in window)) return;
  try {
    window.speechSynthesis.cancel(); // clear previous utterance
    const utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1.05;
    utter.pitch = 1.0;
    utter.volume = Math.min(1.0, audioVolume * 1.15);

    const voices = window.speechSynthesis.getVoices();
    if (voices && voices.length) {
      const preferred = voices.find(v => (v.name.includes("Natural") || v.name.includes("Google") || v.name.includes("Zira") || v.name.includes("Samantha") || v.name.includes("David")) && v.lang.startsWith("en"))
                     || voices.find(v => v.lang.startsWith("en"));
      if (preferred) utter.voice = preferred;
    }
    window.speechSynthesis.speak(utter);
  } catch(e) {}
}

if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = () => {
    // voices ready
  };
}

function updateAudioUI() {
  const btn = document.getElementById("audio_btn");
  const icon = document.getElementById("audio_icon");
  const lbl = document.getElementById("audio_status_lbl");
  if (btn) {
    btn.classList.toggle("active", audioEnabled);
    btn.title = audioEnabled ? "Sound Alerts: ON (Click to mute)" : "Sound Alerts: OFF (Click to unmute)";
  }
  if (icon) icon.textContent = audioEnabled ? "🔊" : "🔇";
  if (lbl) lbl.textContent = audioEnabled ? "AUDIO ON" : "AUDIO OFF";

  const modalBtn = document.getElementById("modal_audio_toggle_btn");
  const modalIcon = document.getElementById("modal_audio_icon");
  const modalStatus = document.getElementById("modal_audio_status");
  if (modalBtn) modalBtn.classList.toggle("active", audioEnabled);
  if (modalIcon) modalIcon.textContent = audioEnabled ? "🔊" : "🔇";
  if (modalStatus) modalStatus.textContent = audioEnabled ? "ENABLED" : "MUTED";

  const modalVoiceBtn = document.getElementById("modal_voice_toggle_btn");
  const modalVoiceIcon = document.getElementById("modal_voice_icon");
  const modalVoiceStatus = document.getElementById("modal_voice_status");
  if (modalVoiceBtn) modalVoiceBtn.classList.toggle("active", voiceEnabled);
  if (modalVoiceIcon) modalVoiceIcon.textContent = voiceEnabled ? "🗣️" : "🤐";
  if (modalVoiceStatus) modalVoiceStatus.textContent = voiceEnabled ? "VOICE ON" : "VOICE OFF";

  const voiceBtn = document.getElementById("voice_toggle_btn");
  const voiceIcon = document.getElementById("voice_icon");
  const voiceLbl = document.getElementById("voice_status_lbl");
  if (voiceBtn) voiceBtn.classList.toggle("active", voiceEnabled);
  if (voiceIcon) voiceIcon.textContent = voiceEnabled ? "🗣️" : "🤐";
  if (voiceLbl) voiceLbl.textContent = voiceEnabled ? "VOICE ON" : "VOICE OFF";

  const miniToggle = document.getElementById("audio_toggle_mini");
  const miniIcon = document.getElementById("audio_icon_mini");
  if (miniToggle) miniToggle.classList.toggle("active", audioEnabled);
  if (miniIcon) miniIcon.textContent = audioEnabled ? "🔊" : "🔇";

  const slider = document.getElementById("vol_slider");
  if (slider) slider.value = audioVolume;
  const sliderMini = document.getElementById("vol_slider_mini");
  if (sliderMini) sliderMini.value = audioVolume;

  const pct = Math.round(audioVolume * 100) + "%";
  const disp = document.getElementById("vol_disp");
  if (disp) disp.textContent = pct;
  const dispMini = document.getElementById("vol_disp_mini");
  if (dispMini) dispMini.textContent = pct;
}

function toggleAudio() {
  ensureAudioCtx();
  audioEnabled = !audioEnabled;
  localStorage.setItem("sldpax_audio", audioEnabled ? "true" : "false");
  updateAudioUI();
  if (audioEnabled) {
    playNote(880, 0.08, "sine", 0.5);
    speak("Sound enabled");
  }
}

function setVolume(val) {
  audioVolume = parseFloat(val);
  localStorage.setItem("sldpax_vol", audioVolume.toString());
  const pct = Math.round(audioVolume * 100) + "%";
  const disp = document.getElementById("vol_disp");
  if (disp) disp.textContent = pct;
  const dispMini = document.getElementById("vol_disp_mini");
  if (dispMini) dispMini.textContent = pct;
  const slider = document.getElementById("vol_slider");
  if (slider && slider.value !== val) slider.value = val;
  const sliderMini = document.getElementById("vol_slider_mini");
  if (sliderMini && sliderMini.value !== val) sliderMini.value = val;
}

function openAudioModal() {
  ensureAudioCtx();
  updateAudioUI();
  const m = document.getElementById("audio_modal_backdrop");
  if (m) m.classList.add("open");
}

function closeAudioModal() {
  const m = document.getElementById("audio_modal_backdrop");
  if (m) m.classList.remove("open");
}

function triggerSoundWave() {
  const sw = document.querySelectorAll(".soundwave-box");
  sw.forEach(box => {
    box.classList.add("anim");
    setTimeout(() => box.classList.remove("anim"), 550);
  });
}

function showAudioToast(title, desc, kind = "ok") {
  const container = document.getElementById("hud_toasts");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "hud-toast toast-" + kind;
  const icon = kind === "ok" ? "⚡" : (kind === "warn" ? "🏁" : "⚠️");
  toast.innerHTML = `
    <div class="hud-toast-icon">${icon}</div>
    <div class="hud-toast-content">
      <div class="hud-toast-title">${title}</div>
      <div class="hud-toast-desc">${desc}</div>
    </div>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("fade-out");
    setTimeout(() => toast.remove(), 320);
  }, 3800);
}

// Low-level synthesized note with clean ADSR envelope
function playNote(freq, dur = 0.15, type = "sine", gainMul = 1.0, delay = 0) {
  if (!audioEnabled) return;
  try {
    const ctx = ensureAudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const startTime = ctx.currentTime + delay;
    const peakGain = 0.09 * audioVolume * gainMul;

    osc.type = type;
    osc.frequency.setValueAtTime(freq, startTime);

    gain.gain.setValueAtTime(0.0001, startTime);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, peakGain), startTime + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, startTime + dur);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start(startTime);
    osc.stop(startTime + dur + 0.05);
    triggerSoundWave();
  } catch(e) {}
}

// 1. Trade Execution (Entry Fill): Ascending triad arpeggio + spoken alert
function playTradeExecutionSound(p = null) {
  if (!audioEnabled) return;
  ensureAudioCtx();
  playNote(523.25, 0.12, "triangle", 0.9, 0.00); // C5
  playNote(659.25, 0.14, "sine",     0.9, 0.07); // E5
  playNote(1046.50, 0.32, "sine",    1.1, 0.14); // C6
  playNote(2093.00, 0.18, "sine",    0.35, 0.15); // Shimmer

  const spoken = p
    ? ("Trade executed: " + (p.side === "BUY" ? "Buy" : "Sell") + " Gold, " + (p.name || "Employee " + p.emp) + ".")
    : "Trade executed.";
  setTimeout(() => speak(spoken), 280);
}

// 2. Trade Exit (Close): Celebratory bell on win, soft mellow resolving tone on flat/loss + spoken alert
function playTradeExitSound(isWin = true, oldP = null) {
  if (!audioEnabled) return;
  ensureAudioCtx();
  if (isWin) {
    playNote(587.33, 0.12, "triangle", 0.9, 0.00); // D5
    playNote(880.00, 0.14, "sine",     0.9, 0.07); // A5
    playNote(1174.66, 0.20, "sine",    1.0, 0.14); // D6
    playNote(1479.98, 0.35, "sine",    0.8, 0.21); // F#6

    const pnlPart = (oldP && oldP.pnl) ? (", profit " + Math.round(oldP.pnl) + " dollars") : ", profit booked";
    const spoken = "Trade exited: " + (oldP && oldP.name ? oldP.name : "Position") + pnlPart + ".";
    setTimeout(() => speak(spoken), 320);
  } else {
    playNote(659.25, 0.18, "sine",     0.9, 0.00); // E5
    playNote(440.00, 0.32, "triangle", 0.8, 0.10); // A4

    const spoken = "Trade exited: " + (oldP && oldP.name ? oldP.name : "Position") + " closed.";
    setTimeout(() => speak(spoken), 300);
  }
}

// 3. Risk / Circuit Caution Alert
function playRiskAlertSound() {
  if (!audioEnabled) return;
  ensureAudioCtx();
  playNote(440.00, 0.12, "sawtooth", 0.7, 0.00);
  playNote(349.23, 0.24, "sawtooth", 0.8, 0.14);
  setTimeout(() => speak("Caution: risk threshold reached."), 250);
}

// Interactive Preview Function (Tests both Synthesized Tone & Voice Speech)
function previewAudio(type) {
  ensureAudioCtx();
  const prevEnabled = audioEnabled;
  const prevVoice = voiceEnabled;
  audioEnabled = true; // allow preview even if muted
  voiceEnabled = true;
  if (type === "EXECUTION") {
    playTradeExecutionSound({ side: "BUY", name: "London Breakout", emp: 1 });
    showAudioToast("PREVIEW: TRADE ENTRY", "Chime + Voice: 'Trade executed: Buy Gold, London Breakout'", "ok");
  } else if (type === "EXIT_WIN") {
    playTradeExitSound(true, { name: "London Breakout", pnl: 320 });
    showAudioToast("PREVIEW: EXIT (PROFIT)", "Chime + Voice: 'Trade exited: London Breakout, profit 320 dollars'", "ok");
  } else if (type === "EXIT_LOSS") {
    playTradeExitSound(false, { name: "Break & Retest", pnl: -120 });
    showAudioToast("PREVIEW: EXIT (LOSS)", "Chime + Voice: 'Trade exited: Break & Retest closed'", "warn");
  } else if (type === "RISK") {
    playRiskAlertSound();
    showAudioToast("PREVIEW: RISK ALERT", "Tone + Voice: 'Caution: risk threshold reached'", "bad");
  }
  audioEnabled = prevEnabled;
  voiceEnabled = prevVoice;
}

// Track previous open positions to accurately trigger entry and exit alerts
let prevPositionsMap = new Map();
let isFirstSnapshot = true;

function checkPositionAlerts(positions) {
  const currentMap = new Map();
  (positions || []).forEach(p => {
    currentMap.set(p.emp, { ...p });
  });

  if (isFirstSnapshot) {
    prevPositionsMap = currentMap;
    isFirstSnapshot = false;
    return;
  }

  // 1. Detect Trade Execution (New Position)
  for (const [empId, p] of currentMap.entries()) {
    if (!prevPositionsMap.has(empId)) {
      playTradeExecutionSound(p);
      showAudioToast("TRADE EXECUTED", `#${p.emp} ${p.name} · ${p.side} ${fmt(p.lots)} lots @ ${fmt(p.open || p.sl)}`, "ok");
    }
  }

  // 2. Detect Trade Exit (Closed Position)
  for (const [empId, oldP] of prevPositionsMap.entries()) {
    if (!currentMap.has(empId)) {
      const wasWin = (oldP.pnl || 0) >= 0;
      playTradeExitSound(wasWin, oldP);
      const signStr = (oldP.pnl || 0) >= 0 ? "+$" + fmt(oldP.pnl) : "-$" + fmt(Math.abs(oldP.pnl));
      showAudioToast("TRADE EXITED", `#${oldP.emp} ${oldP.name} · ${oldP.side} Closed (${signStr})`, wasWin ? "ok" : "warn");
    }
  }

  prevPositionsMap = currentMap;
}

// Canvas Sparklines with Retina Resolution & Hover Cursor
function setupCanvas(canvasId) {
  const c = document.getElementById(canvasId);
  if (!c || c._initialized) return;
  c._initialized = true;
  c._hoverIdx = -1;
  c.addEventListener("mousemove", e => {
    const rect = c.getBoundingClientRect();
    c._hoverX = e.clientX - rect.left;
    c._hoverY = e.clientY - rect.top;
  });
  c.addEventListener("mouseleave", () => {
    c._hoverX = undefined;
    c._hoverY = undefined;
  });
}

function spark(canvasId, points, color){
  setupCanvas(canvasId);
  const c = document.getElementById(canvasId);
  if (!c) return;
  const ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  if (w <= 0 || h <= 0) return;

  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
  }
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (!points || points.length < 2) {
    ctx.restore();
    return;
  }

  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < points.length; i++) {
    const v = points[i];
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const span = hi - lo;
  const pad = (span > 0 ? span * 0.12 : 1);
  const minVal = lo - pad;
  const maxVal = hi + pad;
  const range = maxVal - minVal || 1;

  const X = i => (i / (points.length - 1)) * (w - 20) + 10;
  const Y = v => h - 14 - ((v - minVal) / range) * (h - 28);

  // Grid Lines
  ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(10, Math.round(h * 0.25)); ctx.lineTo(w - 10, Math.round(h * 0.25));
  ctx.moveTo(10, Math.round(h * 0.75)); ctx.lineTo(w - 10, Math.round(h * 0.75));
  ctx.stroke();
  ctx.setLineDash([]);

  // Smooth Area & Line Path
  ctx.beginPath();
  for (let i = 0; i < points.length; i++) {
    const xi = X(i), yi = Y(points[i]);
    if (i === 0) ctx.moveTo(xi, yi);
    else ctx.lineTo(xi, yi);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.shadowColor = color;
  ctx.shadowBlur = 8;
  ctx.stroke();
  ctx.shadowBlur = 0;

  // Gradient Fill
  ctx.lineTo(X(points.length - 1), h);
  ctx.lineTo(X(0), h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "33");
  grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad;
  ctx.fill();

  // Head Pulse Dot
  const lastX = X(points.length - 1);
  const lastY = Y(points[points.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(lastX, lastY, 8, 0, Math.PI * 2);
  ctx.strokeStyle = color + "66";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // High/Low Labels
  ctx.font = "10px 'JetBrains Mono', Consolas, monospace";
  ctx.fillStyle = "rgba(148, 163, 184, 0.6)";
  ctx.textAlign = "left";
  ctx.fillText("HIGH " + fmt(hi), 12, 14);
  ctx.fillText("LOW  " + fmt(lo), 12, h - 4);

  // Hover Tooltip
  if (c._hoverX !== undefined && c._hoverX >= 10 && c._hoverX <= w - 10) {
    const idx = Math.min(points.length - 1, Math.max(0, Math.round(((c._hoverX - 10) / (w - 20)) * (points.length - 1))));
    const hx = X(idx);
    const hy = Y(points[idx]);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(hx, 0); ctx.lineTo(hx, h);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.beginPath();
    ctx.arc(hx, hy, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff";
    ctx.fill();

    const txt = fmt(points[idx]);
    const tw = ctx.measureText(txt).width + 14;
    const bx = Math.min(w - tw - 4, Math.max(4, hx - tw / 2));
    const by = Math.max(18, hy - 8);
    ctx.fillStyle = "rgba(10, 15, 28, 0.95)";
    ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(bx, by - 14, tw, 18, 4); }
    else { ctx.rect(bx, by - 14, tw, 18); }
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.fillText(txt, bx + 7, by - 1);
  }

  ctx.restore();
}

let activeLogFilter = "ALL";
function setLogFilter(level) {
  activeLogFilter = level;
  document.querySelectorAll(".feed-tabs .tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.textContent === level || (level === "ALL" && btn.textContent === "ALL"));
  });
  renderEvents();
}

let currentEvents = [];
function renderEvents() {
  const ev = document.getElementById("events");
  ev.replaceChildren();
  const filtered = activeLogFilter === "ALL"
    ? currentEvents
    : currentEvents.filter(e => e.level === activeLogFilter);

  if (!filtered.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.innerHTML = '<span class="msg">No events matching ' + activeLogFilter + '</span>';
    ev.appendChild(li);
    return;
  }

  filtered.forEach(e => {
    const li = document.createElement("li");
    li.className = "ev-" + e.level;
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = e.time;
    const tg = document.createElement("span");
    tg.className = "tag";
    tg.textContent = e.level;
    const m = document.createElement("span");
    m.className = "msg";
    m.textContent = e.message;
    li.appendChild(t);
    li.appendChild(tg);
    li.appendChild(m);
    ev.appendChild(li);
  });
}

let prevOpenPos = 0;
function reload(s){
  const now = new Date();
  document.getElementById("clock").textContent = now.toLocaleTimeString("en-GB", {hour12: false});
  document.getElementById("utc").textContent = s.utc ? s.utc.slice(11, 19) : "--:--:--";
  document.getElementById("acct_tag").textContent = s.login ? (s.login + " · " + s.company) : "OFFLINE";

  setBid(s.bid);
  document.getElementById("ask").textContent = fmt(s.ask);
  const spr = s.spread;
  const sprEl = document.getElementById("spread");
  sprEl.textContent = fmt(spr);
  if (spr !== null && spr !== undefined) {
    sprEl.className = "spr-badge num " + (spr <= 0.30 ? "spr-tight" : (spr <= 0.75 ? "spr-normal" : "spr-wide"));
  } else {
    sprEl.className = "spr-badge num spr-normal";
  }

  const [sn, sc] = sessionFor(s.utc ? (s.utc.slice(11, 13) | 0) : 0);
  const se = document.getElementById("session");
  se.textContent = sn;
  se.className = "session-capsule " + sc;

  const nt = document.getElementById("netting");
  nt.textContent = "";
  if (s.netting) {
    nt.appendChild(badge("NETTING ACCOUNT — DEGRADED", "b-bad"));
  }

  kpi("k_balance", s.balance, "k_balance_aux", "Anchor $" + fmt(s.day_anchor));
  kpi("k_equity", s.equity, "k_equity_aux", "Peak $" + fmt(s.peak));
  kpi("k_margin", s.margin_free, "k_margin_aux", "Used $" + fmt(s.margin_used));

  const dPnl = s.day_pnl;
  const dayPnlEl = document.getElementById("k_daypnl");
  dayPnlEl.textContent = sgn(dPnl);
  dayPnlEl.classList.toggle("pos", dPnl >= 0);
  dayPnlEl.classList.toggle("neg", dPnl < 0);
  document.getElementById("k_daypnl_aux").textContent = s.day_pnl_pct != null ? sgn(s.day_pnl_pct) + "% Day" : "";
  const cardPnl = document.getElementById("card_daypnl");
  if (cardPnl) {
    cardPnl.className = "panel kpi-card " + (dPnl >= 0 ? "c-ok" : "c-bad");
  }

  const netL = s.net_l !== undefined ? s.net_l : (s.positions ? s.positions.filter(p=>p.side==="BUY").length : 0);
  const netS = s.net_s !== undefined ? s.net_s : (s.positions ? s.positions.filter(p=>p.side==="SELL").length : 0);
  document.getElementById("k_pos").textContent = s.open_pos + " / " + s.open_cap;
  document.getElementById("k_pos_aux").textContent = "Net L:" + netL + " S:" + netS;

  const ddv = s.overall_dd;
  const ddEl = document.getElementById("k_dd");
  ddEl.textContent = "-" + fmtPct(ddv);
  ddEl.classList.toggle("neg", ddv >= s.stop_pct);
  document.getElementById("k_dd_aux").textContent = "~$" + fmt(s.loss_budget) + " Day Budget";

  document.getElementById("dd_peak").textContent = "$" + fmt(s.peak);
  document.getElementById("dd_now").textContent = "-" + fmtPct(ddv);

  const fillPct = Math.min(100, (ddv / s.emergency_pct) * 100).toFixed(1);
  const ddbarFill = document.getElementById("ddbar_fill");
  if (ddbarFill) ddbarFill.style.width = fillPct + "%";

  document.getElementById("dd_stoplbl").textContent = "STOP " + s.stop_pct + "%";
  document.getElementById("dd_emerglbl").textContent = "EMERGENCY " + s.emergency_pct + "%";

  const sb = statusFrame(s.desklock);
  const sc2 = document.getElementById("s_status");
  sc2.textContent = "";
  sc2.appendChild(sb);

  const lat = [];
  if (s.desklock.breaker) lat.push("breaker");
  if (s.desklock.hard) lat.push("hard cutoff");
  if (s.desklock.overall_dd) lat.push("overall DD");
  if (s.desklock.emergency) lat.push("emergency");
  document.getElementById("s_latches").textContent = lat.length
      ? (s.desklock.emergency ? "RESET risk_state to resume" : lat.join(", ")) : "none";
  document.getElementById("dd_risk").textContent = fmtPct(s.risk_pct) + " (cap " + fmtPct(s.risk_cap) + ")";
  document.getElementById("dd_cap").textContent = s.open_cap + " max / " + s.same_dir + " per direction";

  // Active Positions
  const posCountChip = document.getElementById("pos_count_chip");
  if (posCountChip) posCountChip.textContent = s.positions.length + " ACTIVE";
  const pt = document.getElementById("pos_table");
  const posEmpty = document.getElementById("positions");
  if (!s.positions.length){
    pt.style.display = "none";
    posEmpty.style.display = "";
  } else {
    posEmpty.style.display = "none";
    pt.style.display = "";
    const tbody = document.getElementById("positions_body");
    tbody.replaceChildren();
    s.positions.forEach(p => {
      const tr = document.createElement("tr");
      tcell(tr, "#" + p.emp, "num mono");
      tcell(tr, p.name);
      
      const sd = document.createElement("td");
      const sidePill = document.createElement("span");
      sidePill.className = "side-pill side-" + p.side;
      sidePill.textContent = (p.side === "BUY" ? "▲ " : "▼ ") + p.side;
      sd.appendChild(sidePill);
      tr.appendChild(sd);

      tcell(tr, fmt(p.lots), "num mono");
      tcell(tr, fmt(p.sl), "num mono");
      tcell(tr, fmt(p.tp), "num mono");

      const fl = document.createElement("td");
      fl.textContent = sgn(p.pnl);
      fl.className = "num mono " + (p.pnl >= 0 ? "pos" : "neg");
      tr.appendChild(fl);
      tbody.appendChild(tr);
    });
  }

  // Check and trigger audio alerts on trade execution (entry) & trade exit (close)
  checkPositionAlerts(s.positions);

  // Employee Scorecard Roster
  const tbody = document.getElementById("roster");
  tbody.replaceChildren();
  s.employees.forEach(e => {
    const tr = document.createElement("tr");
    tcell(tr, e.id, "num mono");
    tcell(tr, e.name);

    const tfTd = document.createElement("td");
    const tfPill = document.createElement("span");
    tfPill.className = "tf-pill tf-" + e.tf;
    tfPill.textContent = e.tf;
    tfTd.appendChild(tfPill);
    tr.appendChild(tfTd);

    tcell(tr, "1 : " + e.rr, "mono");

    const st = document.createElement("td");
    st.appendChild(empStatus(e, s.desklock));
    tr.appendChild(st);

    const wlcell = document.createElement("td");
    const wl = document.createElement("span");
    wl.className = "wl";
    const tot = Math.max(1, e.wins + e.losses);
    const wp = (e.wins / tot * 100).toFixed(0);
    wl.innerHTML = '<i class="wn" style="width:'+wp+'%"></i><i class="ln" style="width:'+(100-wp)+'%"></i>';
    wlcell.appendChild(wl);
    tr.appendChild(wlcell);

    tcell(tr, e.trades + " / " + e.max_trades, "num mono");
    
    const wpCell = document.createElement("td");
    wpCell.className = "num mono";
    if (e.wins + e.losses > 0) {
      wpCell.textContent = wp + "%";
      wpCell.classList.toggle("pos", wp >= 50);
      wpCell.classList.toggle("neg", wp < 50);
    } else {
      wpCell.textContent = "—";
      wpCell.style.color = "var(--txt-faint)";
    }
    tr.appendChild(wpCell);

    tbody.appendChild(tr);
  });

  // Desk Events
  currentEvents = s.events || [];
  renderEvents();

  // Telemetry Sparklines
  if (s.history && s.history.length){
    spark("c_price", s.history.map(p => p.b), "#38bdf8", true);
    spark("c_equity", s.history.map(p => p.e), "#10b981", false);
  }
}

function kpi(id, val, auxId, auxTxt){
  const el = document.getElementById(id);
  if (el) el.textContent = fmt(val);
  if (auxId) {
    const aux = document.getElementById(auxId);
    if (aux) aux.textContent = auxTxt || "";
  }
}

function tcell(tr, text, cls){
  const td = document.createElement("td");
  td.textContent = text;
  td.className = cls || "";
  tr.appendChild(td);
}

let online = true;
async function tick(){
  try{
    const r = await fetch("/api/state", {cache: "no-store"});
    if (r.ok){
      const data = await r.json();
      try { reload(data); } catch(e) { if (window.console) console.error("reload() error:", e); }
      if(!online){ health(true); }
    } else {
      if (online) health(false);
    }
  }catch(e){
    if (online){ health(false); }
  }
}

function health(on){
  online = on;
  const h = document.getElementById("health");
  if (h) {
    h.classList.toggle("off", !on);
    const txt = document.getElementById("health_txt");
    if (txt) txt.textContent = on ? "ONLINE" : "OFFLINE";
  }
}

updateAudioUI();
tick();
setInterval(tick, 1000);
setInterval(()=>{
  const clk = document.getElementById("clock");
  if (clk) clk.textContent = new Date().toLocaleTimeString("en-GB", {hour12: false});
}, 1000);

window.addEventListener("resize", () => {
  const p = document.getElementById("c_price");
  const e = document.getElementById("c_equity");
  if (p) p.width = p.clientWidth;
  if (e) e.width = e.clientWidth;
});
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    dashboard: "WebDashboard" = None

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            body = json.dumps(self.dashboard.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # keep the console clean
        pass

    def handle_error(self, request, client_address):  # noqa: N802
        """Silence client aborts (tab closed mid-response) - otherwise socketserver
        dumps a full traceback to the engine console on every one."""
        exc_type, _exc, _tb = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True
            return
        super().handle_error(request, client_address)


class WebDashboard:
    """Serves the browser dashboard on a background thread. Starts/stop in engine."""

    def __init__(self, risk_governor, event_log: EventLog,
                 host: str = WEB_DASHBOARD_HOST, port: int = WEB_DASHBOARD_PORT,
                 history_points: int = 180):
        self.risk_governor = risk_governor
        self.events = event_log
        self._host = host
        self._port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._history: Deque[dict] = deque(maxlen=history_points)

    # ------------------------------------------------------------ data snapshot
    def _employee_rows(self, halted: bool, positions: list) -> list:
        rows = []
        for emp_id, emp in EMPLOYEES.items():
            trades = self.risk_governor.daily_emp_trades[emp_id]
            wins = self.risk_governor.daily_emp_wins[emp_id]
            losses = self.risk_governor.daily_emp_losses[emp_id]
            rows.append({
                "id": emp_id, "name": emp.name, "tf": emp.timeframe, "rr": emp.rr_ratio,
                "trades": trades, "wins": wins, "losses": losses,
                "max_trades": MAX_DAILY_TRADES_PER_EMP, "max_losses": MAX_DAILY_LOSSES_PER_EMP,
                "in_trade": any(e == emp_id for _, e in positions),
            })
        return rows

    def snapshot(self) -> dict:
        risk = self.risk_governor
        acc = mt5.account_info()
        tick = mt5.symbol_info_tick(SYMBOL)

        balance = acc.balance if acc else 0.0
        equity = acc.equity if acc else 0.0
        margin_free = acc.margin_free if acc else 0.0
        margin_used = getattr(acc, "margin", 0.0) if acc else 0.0

        day_start = risk.day_start_equity
        day_pnl = risk.day_pnl
        pnl_pct = (day_pnl / day_start * 100.0) if day_start > 0 else 0.0
        positions = risk.get_open_positions()
        buys, sells = risk.net_exposure()

        halted = any((risk.circuit_breaker_triggered, risk.hard_cutoff_triggered,
                      risk.overall_dd_stopped, risk.emergency_stop))
        overall_dd = risk.overall_dd_pct()

        if tick is not None:
            self._history.append({"b": tick.bid, "a": tick.ask, "e": equity})
        history = list(self._history)

        pos_rows = []
        for p, emp_id in sorted(positions, key=lambda x: x[1]):
            pos_rows.append({
                "emp": emp_id, "name": EMPLOYEES[emp_id].name if emp_id in EMPLOYEES else "Unknown",
                "side": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "lots": p.volume, "open": p.price_open, "sl": p.sl, "tp": p.tp, "pnl": p.profit,
            })

        events = [
            {"time": e["time"], "level": e["level"], "message": e["message"]}
            for e in self.events.recent(30, min_level="BLOCK")
        ]

        return {
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "symbol": SYMBOL,
            "bid": tick.bid if tick else None,
            "ask": tick.ask if tick else None,
            "spread": (tick.ask - tick.bid) if tick else None,
            "login": acc.login if acc else None,
            "company": acc.company if acc else None,
            "netting": not risk.hedging_mode,
            "balance": balance, "equity": equity,
            "margin_free": margin_free, "margin_used": margin_used,
            "day_anchor": day_start, "day_pnl": day_pnl, "day_pnl_pct": pnl_pct,
            "loss_budget": risk.max_daily_loss_usd,
            "soft_day": MAX_DAILY_LOSS_PCT, "hard_day": HARD_DAILY_LOSS_PCT,
            "risk_pct": risk.current_risk_percent(), "risk_cap": MAX_SINGLE_TRADE_RISK_PCT,
            "open_pos": len(positions), "open_cap": MAX_OPEN_POSITIONS,
            "same_dir": MAX_SAME_DIRECTION_POSITIONS,
            "net_l": buys, "net_s": sells,
            "peak": risk.peak_equity, "overall_dd": overall_dd,
            "stop_pct": OVERALL_DD_STOP_PCT, "emergency_pct": EMERGENCY_DD_PCT,
            "desklock": {
                "breaker": risk.circuit_breaker_triggered,
                "hard": risk.hard_cutoff_triggered,
                "overall_dd": risk.overall_dd_stopped,
                "emergency": risk.emergency_stop,
                "halted": halted,
            },
            "positions": pos_rows,
            "employees": self._employee_rows(halted, positions),
            "events": events,
            "history": history,
        }

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        _Handler.dashboard = self
        self._server = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="web-dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _open_browser() -> None:
    """Launch the default browser at the dashboard. Non-fatal if it fails."""
    try:
        import webbrowser
        webbrowser.open(f"http://{WEB_DASHBOARD_HOST}:{WEB_DASHBOARD_PORT}/")
    except Exception:
        pass


if __name__ == "__main__":
    import time

    class _Stub:
        hedging_mode = True
        circuit_breaker_triggered = hard_cutoff_triggered = False
        overall_dd_stopped = emergency_stop = False
        day_start_equity = 10000.0
        day_pnl = 0.0
        peak_equity = 10000.0
        daily_emp_trades = {i: 0 for i in range(1, 9)}
        daily_emp_wins = dict(daily_emp_trades)
        daily_emp_losses = dict(daily_emp_trades)

        def overall_dd_pct(self):
            return 0.0

        def current_risk_percent(self):
            return 0.35

        def get_open_positions(self):
            return []

        def net_exposure(self):
            return 0, 0

    _d = WebDashboard(_Stub(), EventLog(), port=8080)
    _d.start()
    _open_browser()
    print("Web dashboard running at http://127.0.0.1:8080/  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _d.stop()
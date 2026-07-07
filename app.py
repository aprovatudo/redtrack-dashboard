import os
import re
import requests
import time
import threading
import concurrent.futures
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from flask import Flask, render_template_string, jsonify, request as flask_request

load_dotenv()

REDTRACK_API_KEY = os.getenv("REDTRACK_API_KEY")
VTURB_API_KEY = "beee18b32e5b5ccd7dcfb1907a62e97babbbeb7cf878ba92313211773392b3d1"
FEGSYS_API_KEY = os.getenv("FEGSYS_API_KEY")
FEGSYS_PROJECT_ID = os.getenv("FEGSYS_PROJECT_ID")
FEGSYS_REFRESH_TOKEN = os.getenv("FEGSYS_REFRESH_TOKEN")

OFFER_SOURCES = {"clickbank", "pagamerican"}

# ── FEGSYS / Firebase ─────────────────────────────────────────────────────────

_fegsys_token_cache = {"token": None, "expires_at": 0}

def get_fegsys_token() -> str:
    """Retorna um access token válido, renovando via refresh_token se necessário."""
    if _fegsys_token_cache["token"] and time.time() < _fegsys_token_cache["expires_at"] - 60:
        return _fegsys_token_cache["token"]
    r = requests.post(
        f"https://securetoken.googleapis.com/v1/token?key={FEGSYS_API_KEY}",
        json={"grant_type": "refresh_token", "refresh_token": FEGSYS_REFRESH_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _fegsys_token_cache["token"] = data["access_token"]
    _fegsys_token_cache["expires_at"] = time.time() + int(data["expires_in"])
    return _fegsys_token_cache["token"]


def _normalize_offer_name(raw: str) -> str:
    """Normaliza o nome da oferta removendo hífens/underscores e versões."""
    name = raw.replace("-", " ").replace("_", " ").strip()
    # Remove sufixos de versão como "2.0", "3.0"
    name = re.sub(r'\s*\d+\.\d+\s*$', '', name).strip()
    return name.upper()


def _categorize_campaign(name: str) -> tuple:
    """Retorna (categoria, oferta). Categoria: 'offer', 'white', 'aquecimento', 'other'."""
    name_lower = name.lower()

    if 'white' in name_lower:
        return ('white', 'White')

    if 'aquecimento' in name_lower or 'aquec' in name_lower:
        return ('aquecimento', 'Aquecimento')

    if 'teste' in name_lower:
        return ('other', 'Teste')

    # Padrão: último [OFERTA] antes de -CXXXX no final
    m = re.search(r'\[([^\]]+)\]-C\d+\s*$', name, re.IGNORECASE)
    if m:
        return ('offer', _normalize_offer_name(m.group(1)))

    return ('other', 'Outros')


def get_fegsys_costs(date_from: str, date_to: str) -> dict:
    """Retorna {offer_name: {brl, usd}} agregado para uso na aba principal."""
    detailed = get_fegsys_detailed(date_from, date_to)
    result = {}
    for offer, data in detailed["offers"].items():
        result[offer] = {"brl": data["total_brl"], "usd": data["total_usd"]}
    return result


def get_fegsys_detailed(date_from: str, date_to: str) -> dict:
    """Retorna dados completos do FEGSYS por oferta, conta e campanha."""
    token = get_fegsys_token()
    r = requests.get(
        "https://fegsys.com/api/trafego/google-ads-costs",
        params={"startDate": date_from, "endDate": date_to},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()

    offers = {}    # {offer_name: {total_brl, total_usd, accounts: [...]}}
    whites = []    # [{conta_id, moeda, campaigns, total}]
    aquecimento = []
    others = []

    for account in data.get("accounts", []):
        conta_id = account.get("conta_id", "")
        moeda = account.get("moeda", "BRL")

        # Agrupar campanhas desta conta por categoria
        acc_offers = {}   # {offer: [{name, cost}]}
        acc_white = []
        acc_aquec = []
        acc_other = []

        for camp in account.get("campaigns", []):
            camp_name = camp.get("campaign_name", "")
            cost = float(camp.get("total_cost", 0))
            if cost <= 0:
                continue
            cat, offer = _categorize_campaign(camp_name)
            entry = {"name": camp_name, "cost": round(cost, 2)}
            if cat == "offer":
                acc_offers.setdefault(offer, []).append(entry)
            elif cat == "white":
                acc_white.append(entry)
            elif cat == "aquecimento":
                acc_aquec.append(entry)
            else:
                acc_other.append(entry)

        # Adicionar ao resultado global por oferta
        for offer, camps in acc_offers.items():
            total = sum(c["cost"] for c in camps)
            if offer not in offers:
                offers[offer] = {"total_brl": 0.0, "total_usd": 0.0, "accounts": []}
            if moeda == "USD":
                offers[offer]["total_usd"] += total
            else:
                offers[offer]["total_brl"] += total
            offers[offer]["accounts"].append({
                "conta_id": conta_id,
                "moeda": moeda,
                "campaigns": camps,
                "total": round(total, 2),
            })

        # White
        if acc_white:
            total = sum(c["cost"] for c in acc_white)
            whites.append({"conta_id": conta_id, "moeda": moeda, "campaigns": acc_white, "total": round(total, 2)})

        # Aquecimento
        if acc_aquec:
            total = sum(c["cost"] for c in acc_aquec)
            aquecimento.append({"conta_id": conta_id, "moeda": moeda, "campaigns": acc_aquec, "total": round(total, 2)})

        # Outros
        if acc_other:
            total = sum(c["cost"] for c in acc_other)
            others.append({"conta_id": conta_id, "moeda": moeda, "campaigns": acc_other, "total": round(total, 2)})

    # Ordenar contas por custo desc dentro de cada oferta
    for offer in offers.values():
        offer["accounts"].sort(key=lambda x: x["total"], reverse=True)
        offer["total_brl"] = round(offer["total_brl"], 2)
        offer["total_usd"] = round(offer["total_usd"], 2)

    whites.sort(key=lambda x: x["total"], reverse=True)
    aquecimento.sort(key=lambda x: x["total"], reverse=True)

    return {
        "offers": dict(sorted(offers.items(), key=lambda x: x[1]["total_brl"] + x[1]["total_usd"], reverse=True)),
        "white": whites,
        "aquecimento": aquecimento,
        "others": others,
    }

app = Flask(__name__)

# Cache de dados para não bloquear o browser
_data_cache = {}
_data_loading = {}
_cache_lock = threading.Lock()

# ── Redtrack ──────────────────────────────────────────────────────────────────

def fetch_redtrack_report(date_from: str, date_to: str) -> list:
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.redtrack.io/report",
                params={
                    "api_key": REDTRACK_API_KEY,
                    "date_from": date_from,
                    "date_to": date_to,
                    "group": "offer",
                    "total": "true",
                    "limit": 100,
                },
                timeout=60,
            )
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return data.get("items", []) if isinstance(data, dict) else data
        except requests.exceptions.Timeout:
            time.sleep(15 * (attempt + 1))
    return []


def fetch_campaigns():
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.redtrack.io/campaigns",
                params={"api_key": REDTRACK_API_KEY},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()
        except:
            time.sleep(10)
    return []


def extract_product_name(title: str):
    parts = title.split("|")
    for i, part in enumerate(parts):
        if part.strip().lower() in OFFER_SOURCES and i + 1 < len(parts):
            return parts[i + 1].strip()
    return None


def is_youtube_campaign(title: str) -> bool:
    t = title.lower()
    return "youtube" in t or "| yt |" in t


def get_redtrack_data(date_from: str, date_to: str) -> dict:
    """Returns {product_name: {cost, revenue, profit, vendas, ic}} using campaign grouping."""
    campaigns = fetch_campaigns()

    # Map campaign_id -> product name
    cid_to_product = {}
    for camp in campaigns:
        if not is_youtube_campaign(camp["title"]):
            continue
        product = extract_product_name(camp["title"])
        if product:
            cid_to_product[camp["id"]] = product

    # Fetch report grouped by campaign (single request)
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.redtrack.io/report",
                params={
                    "api_key": REDTRACK_API_KEY,
                    "date_from": date_from,
                    "date_to": date_to,
                    "group": "campaign",
                    "total": "true",
                    "limit": 100,
                    "sortby": "cost",
                    "direction": "desc",
                },
                timeout=60,
            )
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            break
        except requests.exceptions.Timeout:
            time.sleep(15 * (attempt + 1))
            items = []

    # Aggregate by product
    results = {}
    for item in items:
        cid = item.get("campaign_id") or item.get("id", "")
        product = cid_to_product.get(cid)
        if not product:
            continue
        cost = float(item.get("cost", 0))
        if cost <= 0:
            continue
        if product not in results:
            results[product] = {"cost": 0.0, "revenue": 0.0, "profit": 0.0, "vendas": 0, "ic": 0, "_cpc_weighted": 0.0}
        results[product]["cost"] += cost
        results[product]["revenue"] += float(item.get("total_revenue", 0))
        results[product]["profit"] += float(item.get("profit", 0))
        results[product]["vendas"] += int(item.get("convtype1", 0))
        results[product]["ic"] += int(item.get("convtype2", 0))
        results[product]["_cpc_weighted"] += float(item.get("cpc", 0)) * cost

    for p in results.values():
        cw = p.pop("_cpc_weighted", 0.0)
        p["cpc"] = round(cw / p["cost"], 4) if p["cost"] > 0 else 0.0

    return results


# ── Vturb ─────────────────────────────────────────────────────────────────────

# Keywords to match Vturb player names to offer names
VTURB_OFFER_KEYWORDS = {
    "Prime Pulse": ["prime pulse"],
    "CocoBurn":    ["coco burn", "cocoburn"],
    "GlycoFlush":  ["glycoflush", "glyco flush"],
    "GlycoLean":   ["glycolean", "glyco lean"],
    "MounjaGummy": ["mounja gummy", "mounjagummy"],
}

# Additional filter: only players with these keywords are considered YouTube players
VTURB_YOUTUBE_FILTER = ["google", "youtube", "yt"]

_vturb_players_cache = None

def get_vturb_players():
    global _vturb_players_cache
    if _vturb_players_cache is not None:
        return _vturb_players_cache
    try:
        r = requests.get(
            "https://analytics.vturb.net/players/list",
            headers={"X-Api-Token": VTURB_API_KEY, "X-Api-Version": "v1"},
            timeout=30,
        )
        r.raise_for_status()
        _vturb_players_cache = r.json()
        return _vturb_players_cache
    except Exception as e:
        print(f"Vturb players error: {e}")
        return []


def get_vturb_player_stats(player_id: str, date_from: str, date_to: str) -> dict:
    try:
        r = requests.post(
            "https://analytics.vturb.net/sessions/stats",
            headers={"X-Api-Token": VTURB_API_KEY, "X-Api-Version": "v1", "Content-Type": "application/json"},
            json={
                "player_id": player_id,
                "start_date": f"{date_from} 00:00:00",
                "end_date": f"{date_to} 23:59:59",
                "timezone": "America/Sao_Paulo",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Vturb stats error ({player_id}): {e}")
        return {}


def get_vturb_data(date_from: str, date_to: str) -> dict:
    """Returns {offer_name: {views, plays, pitch}} aggregated across all players."""
    players = get_vturb_players()

    # Map offer -> list of player IDs
    offer_players = {offer: [] for offer in VTURB_OFFER_KEYWORDS}
    for p in players:
        name_lower = p["name"].lower()
        # Only include YouTube players
        if not any(yt in name_lower for yt in VTURB_YOUTUBE_FILTER):
            continue
        for offer, keywords in VTURB_OFFER_KEYWORDS.items():
            if any(kw in name_lower for kw in keywords):
                offer_players[offer].append(p["id"])
                break

    # Fetch all player stats in parallel
    all_pids = [(offer, pid) for offer, pids in offer_players.items() for pid in pids]
    stats_map = {}

    def fetch_one(offer_pid):
        offer, pid = offer_pid
        stats = get_vturb_player_stats(pid, date_from, date_to)
        return offer, pid, stats

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_one, op): op for op in all_pids}
        for future in concurrent.futures.as_completed(futures):
            try:
                offer, pid, stats = future.result(timeout=30)
                if offer not in stats_map:
                    stats_map[offer] = {"views": 0, "views_uniq": 0, "plays_uniq": 0, "_pitch_views": 0, "_pitch_rate_sum": 0.0}
                views = int(stats.get("total_viewed", 0))
                stats_map[offer]["views"] += views
                stats_map[offer]["views_uniq"] += int(stats.get("total_viewed_device_uniq", 0))
                stats_map[offer]["plays_uniq"] += int(stats.get("total_started_device_uniq", 0))
                # Weighted sum for over_pitch_rate
                rate = float(stats.get("over_pitch_rate", 0) or 0)
                stats_map[offer]["_pitch_views"] += views
                stats_map[offer]["_pitch_rate_sum"] += rate * views
            except Exception:
                pass

    # Compute weighted average retention rate across players
    for offer, totals in stats_map.items():
        pv = totals.pop("_pitch_views", 0)
        rs = totals.pop("_pitch_rate_sum", 0.0)
        totals["retention_pct"] = round(rs / pv, 2) if pv > 0 else 0

    return {offer: totals for offer, totals in stats_map.items()
            if totals["views"] > 0 or totals["plays_uniq"] > 0}


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard - F&G Media</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d1a; color: #e8e8f0; font-family: 'Segoe UI', sans-serif; min-height: 100vh; }
  header { background: #12122a; padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #2a2a50; }
  header h1 { color: #00bfff; font-size: 1.4rem; font-weight: 700; }
  header .date { color: #888; font-size: 0.9rem; }
  .tabs { display: flex; gap: 8px; padding: 20px 32px 0; border-bottom: 1px solid #2a2a50; }
  .tab { padding: 10px 20px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.9rem; color: #888; background: #12122a; border: 1px solid #2a2a50; border-bottom: none; transition: all 0.2s; }
  .tab.active { color: #00bfff; background: #1a1a35; border-color: #00bfff; }
  .tab-content { display: none; padding: 24px 32px; }
  .tab-content.active { display: block; }
  .period-selector { display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; align-items: center; }
  .period-btn { padding: 8px 16px; border-radius: 6px; border: 1px solid #2a2a50; background: #12122a; color: #888; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }
  .period-btn.active, .period-btn:hover { border-color: #00bfff; color: #00bfff; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }
  .card { background: #12122a; border: 1px solid #2a2a50; border-radius: 12px; padding: 20px; }
  .card .label { color: #888; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
  .card .value { font-size: 1.5rem; font-weight: 700; color: #e8e8f0; }
  .card .value.green { color: #00dd77; }
  .card .value.red { color: #ff4455; }
  .card .value.blue { color: #00bfff; }
  table { width: 100%; border-collapse: collapse; background: #12122a; border-radius: 12px; overflow: hidden; }
  th { background: #0a0a20; color: #00bfff; font-size: 0.8rem; text-transform: uppercase; padding: 12px 16px; text-align: right; }
  th:first-child { text-align: left; }
  td { padding: 12px 16px; border-top: 1px solid #2a2a50; font-size: 0.9rem; text-align: right; }
  td:first-child { text-align: left; font-weight: 600; }
  tr:hover td { background: #1a1a35; }
  .green { color: #00dd77; }
  .red { color: #ff4455; }
  .loading { text-align: center; padding: 60px; color: #888; }
  .spinner { width: 40px; height: 40px; border: 3px solid #2a2a50; border-top-color: #00bfff; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .offer-section { margin-bottom: 32px; }
  .offer-title { color: #00bfff; font-size: 1.1rem; font-weight: 700; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #2a2a50; }
  .last-updated { color: #555; font-size: 0.8rem; margin-top: 16px; }
  .section-title { color: #e8e8f0; font-size: 1rem; font-weight: 700; margin: 28px 0 12px; padding: 10px 16px; background: #12122a; border-left: 3px solid #00bfff; border-radius: 0 6px 6px 0; display: flex; justify-content: space-between; align-items: center; }
  .section-title .section-total { font-size: 0.85rem; color: #888; font-weight: 400; }
  .offer-block { margin-bottom: 8px; border: 1px solid #2a2a50; border-radius: 10px; overflow: hidden; }
  .offer-header { background: #0f0f28; padding: 12px 16px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; user-select: none; }
  .offer-header:hover { background: #1a1a35; }
  .offer-header .offer-name { color: #00bfff; font-weight: 700; font-size: 0.95rem; }
  .offer-header .offer-meta { display: flex; gap: 20px; align-items: center; font-size: 0.85rem; }
  .offer-header .chevron { color: #555; transition: transform 0.2s; }
  .offer-body { display: none; }
  .offer-body.open { display: block; }
  .account-row { padding: 10px 16px; border-top: 1px solid #1a1a35; background: #12122a; }
  .account-row:hover { background: #16163a; }
  .account-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; cursor: pointer; }
  .account-id { color: #aaa; font-size: 0.82rem; font-family: monospace; }
  .account-total { font-size: 0.9rem; font-weight: 600; }
  .campaigns-list { display: none; padding-left: 16px; }
  .campaigns-list.open { display: block; }
  .campaign-entry { display: flex; justify-content: space-between; padding: 3px 0; font-size: 0.8rem; color: #888; border-bottom: 1px solid #1a1a35; }
  .campaign-entry:last-child { border-bottom: none; }
  .diff-pos { color: #ff4455; }
  .diff-neg { color: #00dd77; }
  .diff-zero { color: #888; }
  .summary-row { display: flex; gap: 20px; flex-wrap: wrap; padding: 12px 16px; background: #0a0a20; border-top: 1px solid #2a2a50; font-size: 0.85rem; }
  .summary-item { display: flex; flex-direction: column; gap: 2px; }
  .summary-item .lbl { color: #555; font-size: 0.75rem; text-transform: uppercase; }
  .summary-item .val { color: #e8e8f0; font-weight: 600; }
</style>
</head>
<body>

<header>
  <h1>📊 Dashboard F&G Media</h1>
  <span class="date" id="current-date"></span>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('today')">Hoje</div>
  <div class="tab" onclick="switchTab('yesterday')">Ontem</div>
  <div class="tab" onclick="switchTab('month')">Mês Atual</div>
  <div class="tab" onclick="switchTab('gads')">Google Ads</div>
</div>

<div class="tab-content active" id="tab-today">
  <div class="period-selector"></div>
  <div id="content-today" class="loading"><div class="spinner"></div>Carregando dados...</div>
</div>
<div class="tab-content" id="tab-yesterday">
  <div id="content-yesterday" class="loading"><div class="spinner"></div>Carregando dados...</div>
</div>
<div class="tab-content" id="tab-month">
  <div id="content-month" class="loading"><div class="spinner"></div>Carregando dados...</div>
</div>
<div class="tab-content" id="tab-gads">
  <div class="period-selector" id="gads-period-selector">
    <button class="period-btn" onclick="switchGadsPeriod('today')">Hoje</button>
    <button class="period-btn active" onclick="switchGadsPeriod('yesterday')">Ontem</button>
    <button class="period-btn" onclick="switchGadsPeriod('month')">Mês Atual</button>
  </div>
  <div id="content-gads" class="loading"><div class="spinner"></div>Carregando dados...</div>
</div>

<script>
let cache = {};
let currentTab = 'today';

function loadData(tab) {
  const el = document.getElementById('content-' + tab);
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Buscando dados do Redtrack... (pode levar alguns minutos)</div>';
  pollData(tab, 0);
}

function pollData(tab, attempts) {
  const el = document.getElementById('content-' + tab);
  fetch('/api/data?period=' + tab)
    .then(r => r.json())
    .then(data => {
      if (data.status === 'loading') {
        if (attempts > 120) {
          el.innerHTML = '<div class="loading">Timeout ao carregar dados. Tente novamente.</div>';
          return;
        }
        setTimeout(() => pollData(tab, attempts + 1), 5000);
      } else if (data.status === 'error') {
        el.innerHTML = '<div class="loading">Erro ao carregar dados: ' + (data.error || '') + '</div>';
      } else {
        cache[tab] = data;
        renderData(tab, data);
      }
    })
    .catch(() => {
      if (attempts > 120) {
        el.innerHTML = '<div class="loading">Erro ao carregar dados.</div>';
        return;
      }
      setTimeout(() => pollData(tab, attempts + 1), 5000);
    });
}

function fmtBRL(v) {
  if (v == null) return '-';
  const neg = v < 0;
  const s = 'R$ ' + Math.abs(v).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return neg ? '-' + s : s;
}
function fmtNum(v, dec=2) { return v == null ? '-' : v.toLocaleString('pt-BR', {minimumFractionDigits: dec, maximumFractionDigits: dec}); }
function fmtPct(v) { return v == null ? '-' : fmtNum(v) + '%'; }

function renderData(tab, data) {
  const el = document.getElementById('content-' + tab);
  if (!data.offers || data.offers.length === 0) {
    el.innerHTML = '<div class="loading">Nenhum dado disponível para este período.</div>';
    return;
  }

  // Summary cards
  let totalCost = 0, totalRevenue = 0, totalProfit = 0, totalVendas = 0, totalIC = 0;
  data.offers.forEach(o => {
    totalCost += o.cost || 0;
    totalRevenue += o.revenue || 0;
    totalProfit += o.profit || 0;
    totalVendas += o.vendas || 0;
    totalIC += o.ic || 0;
  });
  const totalROAS = totalCost > 0 ? totalRevenue / totalCost : 0;
  const totalCPA  = totalVendas > 0 ? totalCost / totalVendas : 0;
  const totalAOV  = totalVendas > 0 ? totalRevenue / totalVendas : 0;
  const totalCPIC = totalIC > 0 ? totalCost / totalIC : 0;

  let html = `
  <div class="cards">
    <div class="card"><div class="label">Gastos</div><div class="value red">${fmtBRL(totalCost)}</div></div>
    <div class="card"><div class="label">Faturamento</div><div class="value blue">${fmtBRL(totalRevenue)}</div></div>
    <div class="card"><div class="label">Lucro</div><div class="value ${totalProfit >= 0 ? 'green' : 'red'}">${fmtBRL(totalProfit)}</div></div>
    <div class="card"><div class="label">Vendas</div><div class="value">${totalVendas}</div></div>
    <div class="card"><div class="label">IC</div><div class="value">${totalIC}</div></div>
    <div class="card"><div class="label">ROAS</div><div class="value ${totalROAS >= 1 ? 'green' : 'red'}">${fmtNum(totalROAS)}</div></div>
    <div class="card"><div class="label">CPA</div><div class="value">${fmtBRL(totalCPA)}</div></div>
    <div class="card"><div class="label">AOV</div><div class="value">${fmtBRL(totalAOV)}</div></div>
    <div class="card"><div class="label">CP/IC</div><div class="value">${fmtBRL(totalCPIC)}</div></div>
    <div class="card"><div class="label">Purchase CR</div><div class="value">${fmtPct(totalIC > 0 ? totalVendas / totalIC * 100 : 0)}</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Oferta</th>
        <th>Gastos</th>
        <th>Faturamento</th>
        <th>Lucro</th>
        <th>Vendas</th>
        <th>IC</th>
        <th>CPC</th>
        <th>CPA</th>
        <th>AOV</th>
        <th>CP/IC</th>
        <th>Purchase CR</th>
        <th>ROAS</th>
        <th>Custo Google (BRL)</th>
        <th>Custo Google (USD)</th>
      </tr>
    </thead>
    <tbody>
  `;

  data.offers.forEach(o => {
    const roas  = o.cost > 0 ? o.revenue / o.cost : 0;
    const cpa   = o.vendas > 0 ? o.cost / o.vendas : 0;
    const aov   = o.vendas > 0 ? o.revenue / o.vendas : 0;
    const cpic  = o.ic > 0 ? o.cost / o.ic : 0;
    const profitClass = o.profit >= 0 ? 'green' : 'red';
    const roasClass   = roas >= 1 ? 'green' : 'red';
    html += `
      <tr>
        <td>${o.name}</td>
        <td>${fmtBRL(o.cost)}</td>
        <td>${fmtBRL(o.revenue)}</td>
        <td class="${profitClass}">${fmtBRL(o.profit)}</td>
        <td>${o.vendas}</td>
        <td>${o.ic}</td>
        <td>${fmtBRL(o.cpc)}</td>
        <td>${fmtBRL(cpa)}</td>
        <td>${fmtBRL(aov)}</td>
        <td>${fmtBRL(cpic)}</td>
        <td>${fmtPct(o.ic > 0 ? o.vendas / o.ic * 100 : 0)}</td>
        <td class="${roasClass}">${fmtNum(roas)}</td>
        <td>${o.fg_cost_brl > 0 ? fmtBRL(o.fg_cost_brl) : '-'}</td>
        <td>${o.fg_cost_usd > 0 ? 'US$ ' + fmtNum(o.fg_cost_usd) : '-'}</td>
      </tr>
    `;
  });

  html += `</tbody></table><div class="last-updated">Atualizado: ${data.updated_at}</div>`;
  el.innerHTML = html;
}

// Set current date
document.getElementById('current-date').textContent = new Date().toLocaleDateString('pt-BR', {weekday:'long', day:'numeric', month:'long', year:'numeric'});

// Load initial tab
loadData('today');

// ── Google Ads Tab ────────────────────────────────────────────────────────────
let gadsPeriod = 'yesterday';
let gadsCache = {};

function switchGadsPeriod(period) {
  gadsPeriod = period;
  document.querySelectorAll('#gads-period-selector .period-btn').forEach((b, i) => {
    b.classList.toggle('active', ['today','yesterday','month'][i] === period);
  });
  if (!gadsCache[period]) loadGads(period);
  else renderGads(gadsCache[period]);
}

function loadGads(period) {
  const el = document.getElementById('content-gads');
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Buscando dados do Google Ads... (pode levar alguns minutos)</div>';
  pollGads(period, 0);
}

function pollGads(period, attempts) {
  const el = document.getElementById('content-gads');
  fetch('/api/google-ads?period=' + period)
    .then(r => r.json())
    .then(data => {
      if (data.status === 'loading') {
        if (attempts > 120) { el.innerHTML = '<div class="loading">Timeout. Tente novamente.</div>'; return; }
        setTimeout(() => pollGads(period, attempts + 1), 5000);
      } else if (data.status === 'error') {
        el.innerHTML = '<div class="loading">Erro: ' + (data.error || '') + '</div>';
      } else {
        gadsCache[period] = data;
        renderGads(data);
      }
    })
    .catch(() => {
      if (attempts > 120) return;
      setTimeout(() => pollGads(period, attempts + 1), 5000);
    });
}

function fmtCost(v, moeda) {
  if (!v) return '-';
  const abs = Math.abs(v).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const sym = moeda === 'USD' ? 'US$ ' : 'R$ ';
  return (v < 0 ? '-' : '') + sym + abs;
}

function fmtDiff(diff) {
  if (diff === 0) return '<span class="diff-zero">—</span>';
  const cls = diff > 0 ? 'diff-pos' : 'diff-neg';
  const sign = diff > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${diff.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</span>`;
}

function renderAccountsBlock(accounts, moeda) {
  return accounts.map((acc, ai) => `
    <div class="account-row">
      <div class="account-header" onclick="toggleCampaigns('acc-${ai}-${Math.random().toString(36).slice(2)}', this)">
        <span class="account-id">${acc.conta_id}</span>
        <span class="account-total">${fmtCost(acc.total, acc.moeda)}</span>
      </div>
    </div>
  `).join('');
}

function renderGads(data) {
  const el = document.getElementById('content-gads');
  if (!data.offers || Object.keys(data.offers).length === 0) {
    el.innerHTML = '<div class="loading">Nenhum dado disponível.</div>';
    return;
  }

  // Totais gerais
  let totalBRL = 0, totalUSD = 0, totalRT = 0, totalDiff = 0;
  Object.values(data.offers).forEach(o => {
    totalBRL += o.total_brl || 0;
    totalUSD += o.total_usd || 0;
    totalRT += o.rt_cost || 0;
    totalDiff += o.diff || 0;
  });
  // White
  const whiteBRL = data.white.filter(a=>a.moeda==='BRL').reduce((s,a)=>s+a.total,0);
  const whiteUSD = data.white.filter(a=>a.moeda==='USD').reduce((s,a)=>s+a.total,0);
  const aquecBRL = data.aquecimento.filter(a=>a.moeda==='BRL').reduce((s,a)=>s+a.total,0);
  const aquecUSD = data.aquecimento.filter(a=>a.moeda==='USD').reduce((s,a)=>s+a.total,0);

  let html = `
  <div class="cards">
    <div class="card"><div class="label">Ofertas (BRL)</div><div class="value red">${fmtBRL(totalBRL)}</div></div>
    <div class="card"><div class="label">Ofertas (USD)</div><div class="value red">US$ ${fmtNum(totalUSD)}</div></div>
    <div class="card"><div class="label">White (BRL)</div><div class="value">${fmtBRL(whiteBRL)}</div></div>
    <div class="card"><div class="label">White (USD)</div><div class="value">US$ ${fmtNum(whiteUSD)}</div></div>
    <div class="card"><div class="label">Aquecimento (USD)</div><div class="value">US$ ${fmtNum(aquecUSD)}</div></div>
  </div>`;

  // ── Por Oferta ──
  html += '<div class="section-title">Por Oferta</div>';
  Object.entries(data.offers).forEach(([offer, d], oi) => {
    const bodyId = 'offer-body-' + oi;
    const nContas = d.accounts.length;
    html += `
    <div class="offer-block">
      <div class="offer-header" onclick="toggleOfferBody('${bodyId}', this)">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span class="offer-name">${offer}</span>
          <span style="font-size:0.78rem;color:#555;">${nContas} conta(s)</span>
        </div>
        <div class="offer-meta">
          ${d.total_brl > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">BRL</span><b style="color:#e8e8f0;">${fmtBRL(d.total_brl)}</b></div>` : ''}
          ${d.total_usd > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">USD</span><b style="color:#e8e8f0;">US$ ${fmtNum(d.total_usd)}</b></div>` : ''}
          <span class="chevron">▶</span>
        </div>
      </div>
      <div class="offer-body" id="${bodyId}">
        <table style="width:100%;border-collapse:collapse;">
          <thead>
            <tr style="background:#0a0a20;">
              <th style="padding:8px 16px;text-align:left;color:#00bfff;font-size:0.78rem;">Conta</th>
              <th style="padding:8px 16px;text-align:left;color:#00bfff;font-size:0.78rem;">Moeda</th>
              <th style="padding:8px 16px;text-align:right;color:#00bfff;font-size:0.78rem;">Custo Google</th>
              <th style="padding:8px 16px;text-align:left;color:#00bfff;font-size:0.78rem;">Campanhas</th>
            </tr>
          </thead>
          <tbody>
            ${d.accounts.map((acc, ai) => {
              const campId = 'camps-' + oi + '-' + ai;
              return `
              <tr style="border-top:1px solid #1a1a35;">
                <td style="padding:8px 16px;font-family:monospace;font-size:0.82rem;color:#aaa;">${acc.conta_id}</td>
                <td style="padding:8px 16px;font-size:0.82rem;color:#888;">${acc.moeda}</td>
                <td style="padding:8px 16px;text-align:right;font-weight:600;">${fmtCost(acc.total, acc.moeda)}</td>
                <td style="padding:8px 16px;">
                  <span style="cursor:pointer;color:#00bfff;font-size:0.8rem;" onclick="toggleEl('${campId}')">▶ ${acc.campaigns.length} campanha(s)</span>
                  <div id="${campId}" style="display:none;margin-top:6px;">
                    ${acc.campaigns.map(c => `
                      <div class="campaign-entry">
                        <span>${c.name}</span>
                        <span>${fmtCost(c.cost, acc.moeda)}</span>
                      </div>`).join('')}
                  </div>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
          <tfoot>
            <tr style="background:#0a0a20;border-top:2px solid #2a2a50;">
              <td colspan="2" style="padding:10px 16px;color:#555;font-size:0.82rem;">Total (${nContas} contas)</td>
              <td style="padding:10px 16px;text-align:right;font-weight:700;">
                ${d.total_brl > 0 ? `<span style="color:#e8e8f0;">${fmtBRL(d.total_brl)}</span>` : ''}
                ${d.total_brl > 0 && d.total_usd > 0 ? ' &nbsp;|&nbsp; ' : ''}
                ${d.total_usd > 0 ? `<span style="color:#e8e8f0;">US$ ${fmtNum(d.total_usd)}</span>` : ''}
              </td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`;
  });

  // ── White ──
  if (data.white.length > 0) {
    const wBodyId = 'white-body';
    html += `
    <div class="section-title" style="border-left-color:#888;">
      White
      <span class="section-total">BRL: ${fmtBRL(whiteBRL)} | USD: US$ ${fmtNum(whiteUSD)}</span>
    </div>
    <div class="offer-block">
      <div class="offer-header" onclick="toggleOfferBody('${wBodyId}', this)">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span class="offer-name" style="color:#aaa;">Campanhas White</span>
          <span style="font-size:0.78rem;color:#555;">${data.white.length} conta(s)</span>
        </div>
        <div class="offer-meta">
          ${whiteBRL > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">BRL</span><b style="color:#e8e8f0;">${fmtBRL(whiteBRL)}</b></div>` : ''}
          ${whiteUSD > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">USD</span><b style="color:#e8e8f0;">US$ ${fmtNum(whiteUSD)}</b></div>` : ''}
          <span class="chevron">▶</span>
        </div>
      </div>
      <div class="offer-body" id="${wBodyId}">
        <table style="width:100%;border-collapse:collapse;">
          <thead><tr style="background:#0a0a20;">
            <th style="padding:8px 16px;text-align:left;color:#888;font-size:0.78rem;">Conta</th>
            <th style="padding:8px 16px;text-align:left;color:#888;font-size:0.78rem;">Moeda</th>
            <th style="padding:8px 16px;text-align:right;color:#888;font-size:0.78rem;">Total</th>
            <th style="padding:8px 16px;text-align:left;color:#888;font-size:0.78rem;">Campanhas</th>
          </tr></thead>
          <tbody>
            ${data.white.map((acc, ai) => {
              const cid = 'wcamps-' + ai;
              return `<tr style="border-top:1px solid #1a1a35;">
                <td style="padding:8px 16px;font-family:monospace;font-size:0.82rem;color:#aaa;">${acc.conta_id}</td>
                <td style="padding:8px 16px;font-size:0.82rem;color:#888;">${acc.moeda}</td>
                <td style="padding:8px 16px;text-align:right;font-weight:600;">${fmtCost(acc.total, acc.moeda)}</td>
                <td style="padding:8px 16px;">
                  <span style="cursor:pointer;color:#888;font-size:0.8rem;" onclick="toggleEl('${cid}')">▶ ${acc.campaigns.length} campanha(s)</span>
                  <div id="${cid}" style="display:none;margin-top:6px;">
                    ${acc.campaigns.map(c => `<div class="campaign-entry"><span>${c.name}</span><span>${fmtCost(c.cost, acc.moeda)}</span></div>`).join('')}
                  </div>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
          <tfoot>
            <tr style="background:#0a0a20;border-top:2px solid #2a2a50;">
              <td colspan="2" style="padding:10px 16px;color:#555;font-size:0.82rem;">Total (${data.white.length} contas)</td>
              <td style="padding:10px 16px;text-align:right;font-weight:700;">
                ${whiteBRL > 0 ? `<span style="color:#e8e8f0;">${fmtBRL(whiteBRL)}</span>` : ''}
                ${whiteBRL > 0 && whiteUSD > 0 ? ' &nbsp;|&nbsp; ' : ''}
                ${whiteUSD > 0 ? `<span style="color:#e8e8f0;">US$ ${fmtNum(whiteUSD)}</span>` : ''}
              </td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`;
  }

  // ── Aquecimento ──
  if (data.aquecimento.length > 0) {
    const aBodyId = 'aquec-body';
    html += `
    <div class="section-title" style="border-left-color:#f59e0b;">
      Aquecimento
      <span class="section-total">BRL: ${fmtBRL(aquecBRL)} | USD: US$ ${fmtNum(aquecUSD)}</span>
    </div>
    <div class="offer-block">
      <div class="offer-header" onclick="toggleOfferBody('${aBodyId}', this)">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span class="offer-name" style="color:#f59e0b;">Campanhas de Aquecimento</span>
          <span style="font-size:0.78rem;color:#555;">${data.aquecimento.length} conta(s)</span>
        </div>
        <div class="offer-meta">
          ${aquecBRL > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">BRL</span><b style="color:#e8e8f0;">${fmtBRL(aquecBRL)}</b></div>` : ''}
          ${aquecUSD > 0 ? `<div style="display:flex;flex-direction:column;align-items:flex-end;"><span style="font-size:0.7rem;color:#555;">USD</span><b style="color:#e8e8f0;">US$ ${fmtNum(aquecUSD)}</b></div>` : ''}
          <span class="chevron">▶</span>
        </div>
      </div>
      <div class="offer-body" id="${aBodyId}">
        <table style="width:100%;border-collapse:collapse;">
          <thead><tr style="background:#0a0a20;">
            <th style="padding:8px 16px;text-align:left;color:#f59e0b;font-size:0.78rem;">Conta</th>
            <th style="padding:8px 16px;text-align:left;color:#f59e0b;font-size:0.78rem;">Moeda</th>
            <th style="padding:8px 16px;text-align:right;color:#f59e0b;font-size:0.78rem;">Total</th>
            <th style="padding:8px 16px;text-align:left;color:#f59e0b;font-size:0.78rem;">Campanhas</th>
          </tr></thead>
          <tbody>
            ${data.aquecimento.map((acc, ai) => {
              const cid = 'acamps-' + ai;
              return `<tr style="border-top:1px solid #1a1a35;">
                <td style="padding:8px 16px;font-family:monospace;font-size:0.82rem;color:#aaa;">${acc.conta_id}</td>
                <td style="padding:8px 16px;font-size:0.82rem;color:#888;">${acc.moeda}</td>
                <td style="padding:8px 16px;text-align:right;font-weight:600;">${fmtCost(acc.total, acc.moeda)}</td>
                <td style="padding:8px 16px;">
                  <span style="cursor:pointer;color:#f59e0b;font-size:0.8rem;" onclick="toggleEl('${cid}')">▶ ${acc.campaigns.length} campanha(s)</span>
                  <div id="${cid}" style="display:none;margin-top:6px;">
                    ${acc.campaigns.map(c => `<div class="campaign-entry"><span>${c.name}</span><span>${fmtCost(c.cost, acc.moeda)}</span></div>`).join('')}
                  </div>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
          <tfoot>
            <tr style="background:#0a0a20;border-top:2px solid #2a2a50;">
              <td colspan="2" style="padding:10px 16px;color:#555;font-size:0.82rem;">Total (${data.aquecimento.length} contas)</td>
              <td style="padding:10px 16px;text-align:right;font-weight:700;">
                ${aquecBRL > 0 ? `<span style="color:#e8e8f0;">${fmtBRL(aquecBRL)}</span>` : ''}
                ${aquecBRL > 0 && aquecUSD > 0 ? ' &nbsp;|&nbsp; ' : ''}
                ${aquecUSD > 0 ? `<span style="color:#e8e8f0;">US$ ${fmtNum(aquecUSD)}</span>` : ''}
              </td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`;
  }

  html += `<div class="last-updated">Atualizado: ${data.updated_at}</div>`;
  el.innerHTML = html;
}

function toggleOfferBody(id, header) {
  const body = document.getElementById(id);
  const chevron = header.querySelector('.chevron');
  const isOpen = body.classList.toggle('open');
  if (chevron) chevron.textContent = isOpen ? '▼' : '▶';
}

function toggleEl(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ── Tab switching (updated to handle gads) ───────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    t.classList.toggle('active', ['today','yesterday','month','gads'][i] === tab);
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  currentTab = tab;
  if (tab === 'gads') {
    if (!gadsCache[gadsPeriod]) loadGads(gadsPeriod);
    else renderGads(gadsCache[gadsPeriod]);
  } else {
    if (!cache[tab]) loadData(tab);
  }
}

// Auto-refresh a cada 5 minutos
setInterval(() => {
  cache = {};
  gadsCache = {};
  fetch('/api/cache/clear').finally(() => {
    if (currentTab === 'gads') loadGads(gadsPeriod);
    else loadData(currentTab);
  });
}, 5 * 60 * 1000);
</script>
</body>
</html>
"""

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


def load_data_async(period: str, date_from: str, date_to: str):
    try:
        print(f"[{period}] Buscando Redtrack e FEGSYS em paralelo...")
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_rt = ex.submit(get_redtrack_data, date_from, date_to)
            f_fg = ex.submit(get_fegsys_costs, date_from, date_to)
            rt_data = f_rt.result()
            fegsys_data = f_fg.result()
        print(f"[{period}] Redtrack OK ({len(rt_data)} ofertas). FEGSYS OK ({len(fegsys_data)} ofertas).")

        offers = []
        for name, metrics in rt_data.items():
            fg = fegsys_data.get(name.upper(), fegsys_data.get(name, {}))
            # Tentar match case-insensitive
            if not fg:
                for k, v in fegsys_data.items():
                    if k.lower() == name.lower():
                        fg = v
                        break
            offers.append({
                "name": name,
                "cost": round(metrics["cost"], 2),
                "revenue": round(metrics["revenue"], 2),
                "profit": round(metrics["profit"], 2),
                "vendas": metrics["vendas"],
                "ic": metrics["ic"],
                "cpc": metrics["cpc"],
                "fg_cost_brl": round(fg.get("brl", 0), 2),
                "fg_cost_usd": round(fg.get("usd", 0), 2),
            })
        offers.sort(key=lambda x: x["cost"], reverse=True)
        with _cache_lock:
            _data_cache[period] = {
                "offers": offers,
                "period": {"from": date_from, "to": date_to},
                "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "status": "ready",
            }
    except Exception as e:
        with _cache_lock:
            _data_cache[period] = {"status": "error", "offers": [], "error": str(e)}
    finally:
        with _cache_lock:
            _data_loading.pop(period, None)


@app.route("/api/cache/clear")
def api_cache_clear():
    with _cache_lock:
        _data_cache.clear()
        _gads_cache.clear()
    return jsonify({"ok": True})


_gads_cache = {}
_gads_loading = {}

def load_gads_async(period: str, date_from: str, date_to: str):
    try:
        print(f"[gads/{period}] Buscando FEGSYS e Redtrack em paralelo...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_fg = ex.submit(get_fegsys_detailed, date_from, date_to)
            f_rt = ex.submit(get_redtrack_data, date_from, date_to)
            fegsys = f_fg.result()
            rt_data = f_rt.result()
        print(f"[gads/{period}] OK.")

        # Normalizar nomes do Redtrack para comparação
        rt_normalized = {}
        for name, metrics in rt_data.items():
            rt_normalized[_normalize_offer_name(name)] = round(metrics["cost"], 2)

        # Adicionar custo Redtrack a cada oferta do FEGSYS
        offers_out = {}
        for offer, data in fegsys["offers"].items():
            rt_cost = rt_normalized.get(offer, 0)
            fg_total = data["total_brl"] + data["total_usd"]
            diff = round(fg_total - rt_cost, 2)
            offers_out[offer] = {
                "total_brl": data["total_brl"],
                "total_usd": data["total_usd"],
                "fg_total": round(fg_total, 2),
                "rt_cost": rt_cost,
                "diff": diff,
                "accounts": data["accounts"],
            }

        with _cache_lock:
            _gads_cache[period] = {
                "offers": offers_out,
                "white": fegsys["white"],
                "aquecimento": fegsys["aquecimento"],
                "others": fegsys["others"],
                "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "status": "ready",
            }
    except Exception as e:
        import traceback; traceback.print_exc()
        with _cache_lock:
            _gads_cache[period] = {"status": "error", "error": str(e)}
    finally:
        with _cache_lock:
            _gads_loading.pop(period, None)


@app.route("/api/google-ads")
def api_google_ads():
    period = flask_request.args.get("period", "today")
    today = date.today()
    if period == "today":
        date_from = date_to = today.isoformat()
    elif period == "yesterday":
        yesterday = today - timedelta(days=1)
        date_from = date_to = yesterday.isoformat()
    elif period == "month":
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()
    else:
        date_from = date_to = today.isoformat()

    with _cache_lock:
        cached = _gads_cache.get(period)
        loading = _gads_loading.get(period, False)

    if cached and cached.get("status") == "ready":
        return jsonify(cached)

    if not loading:
        with _cache_lock:
            _gads_loading[period] = True
        t = threading.Thread(target=load_gads_async, args=(period, date_from, date_to), daemon=True)
        t.start()

    return jsonify({"status": "loading"})


@app.route("/api/data")
def api_data():
    period = flask_request.args.get("period", "today")
    today = date.today()

    if period == "today":
        date_from = date_to = today.isoformat()
    elif period == "yesterday":
        yesterday = today - timedelta(days=1)
        date_from = date_to = yesterday.isoformat()
    elif period == "month":
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()
    else:
        date_from = date_to = today.isoformat()

    with _cache_lock:
        cached = _data_cache.get(period)
        loading = _data_loading.get(period, False)

    if cached and cached.get("status") == "ready":
        return jsonify(cached)

    if not loading:
        with _cache_lock:
            _data_loading[period] = True
        t = threading.Thread(target=load_data_async, args=(period, date_from, date_to), daemon=True)
        t.start()

    return jsonify({"status": "loading", "offers": []})


@app.route("/api/debug/vturb")
def api_debug_vturb():
    """Lista todos os players Vturb encontrados por oferta e seus stats de ontem."""
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    players = get_vturb_players()
    matched = []  # list of (offer, player)

    for p in players:
        name_lower = p["name"].lower()
        if not any(yt in name_lower for yt in VTURB_YOUTUBE_FILTER):
            continue
        for offer, keywords in VTURB_OFFER_KEYWORDS.items():
            if any(kw in name_lower for kw in keywords):
                matched.append((offer, p))
                break

    def fetch_debug(offer_player):
        offer, p = offer_player
        stats = get_vturb_player_stats(p["id"], yesterday, yesterday)
        return offer, {
            "id": p["id"],
            "name": p["name"],
            "views": stats.get("total_viewed", 0),
            "views_uniq": stats.get("total_viewed_device_uniq", 0),
            "plays_uniq": stats.get("total_started_device_uniq", 0),
            "pitch": stats.get("total_over_pitch", 0),
            "over_pitch_rate": stats.get("over_pitch_rate", 0),
        }

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_debug, op) for op in matched]
        for f in concurrent.futures.as_completed(futures):
            try:
                offer, entry = f.result(timeout=30)
                result.setdefault(offer, []).append(entry)
            except Exception:
                pass

    return jsonify(result)


@app.route("/api/debug/fegsys/collections")
def api_debug_fegsys_collections():
    """Lista as coleções raiz do Firestore do FEGSYS."""
    try:
        token = get_fegsys_token()
        url = f"https://firestore.googleapis.com/v1/projects/{FEGSYS_PROJECT_ID}/databases/(default)/documents:listCollectionIds"
        r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json={}, timeout=15)
        return jsonify({"status": r.status_code, "body": r.json()})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug/vturb/players/sample")
def api_debug_vturb_players_sample():
    """Testa filtro por folder_id na listagem de players."""
    try:
        r = requests.get(
            "https://analytics.vturb.net/players/list",
            headers={"X-Api-Token": VTURB_API_KEY, "X-Api-Version": "v1"},
            params={"folder_id": "69cbe4c4c602028f65c64b11"},  # pasta CocoBurn
            timeout=30,
        )
        return jsonify({"status": r.status_code, "count": len(r.json()) if isinstance(r.json(), list) else None, "sample": r.json()[:3] if isinstance(r.json(), list) else r.json()})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug/vturb/folder/<folder_id>")
def api_debug_vturb_folder(folder_id):
    """Testa consulta de stats por pasta no Vturb."""
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        r = requests.post(
            "https://analytics.vturb.net/sessions/stats",
            headers={"X-Api-Token": VTURB_API_KEY, "X-Api-Version": "v1", "Content-Type": "application/json"},
            json={
                "folder_id": folder_id,
                "start_date": f"{yesterday} 00:00:00",
                "end_date": f"{yesterday} 23:59:59",
                "timezone": "America/Sao_Paulo",
            },
            timeout=30,
        )
        return jsonify({"status": r.status_code, "body": r.json() if "application/json" in r.headers.get("content-type","") else r.text})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug/vturb/raw/<player_id>")
def api_debug_vturb_raw(player_id):
    """Mostra a resposta bruta da API Vturb para um player específico (ontem)."""
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        r = requests.post(
            "https://analytics.vturb.net/sessions/stats",
            headers={"X-Api-Token": VTURB_API_KEY, "X-Api-Version": "v1", "Content-Type": "application/json"},
            json={
                "player_id": player_id,
                "start_date": f"{yesterday} 00:00:00",
                "end_date": f"{yesterday} 23:59:59",
                "timezone": "America/Sao_Paulo",
            },
            timeout=30,
        )
        return jsonify({"status": r.status_code, "body": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)

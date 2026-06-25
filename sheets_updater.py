import os
import requests
import time
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

REDTRACK_API_KEY = os.getenv("REDTRACK_API_KEY")
SPREADSHEET_ID = "1QHpah9TOF40mFetcCdUHCG8dUleQTkSR9yK7WpyzlAA"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")

IGNORED_SHEETS = {"MODELO [DUPLICAR] 🚨", "Teste Automação"}

# Configuração por aba: filtra por campanha e/ou offer source (network_id)
SHEET_CONFIG = {
    "Jellytide": {
        "campaign_pattern": "JELLYTIDE",
        "network_id": "6a020b598852d70d4f74a905",
    },
    "Prime Pulse 1.2 [Vigor boost]": {
        "campaign_pattern": "VIGOR BOOST",
        "name_col": "Q",
        "name_mapping": {
            "ML 1.1 PP 1.2-YT-CV": "ML 1.1 PP 1.2-YT-SD",
            "ML 1.2 PP 1.2-YT-CV": "ML 1.2 PP 1.2-YT-SD",
            "ML 1.3 PP 1.2-YT-CV": "ML 1.3 PP 1.2-YT-SD",
            "ML 2.2 PP 1.2-YT-CV": "ML 2.2 PP 1.2-YT-SD",
        },
    },
    # Ambas as grafias apontam para a mesma config
    "RockBoost": {
        "campaign_pattern": ["ROCKBOOST", "ROCK BOOST"],
        "currency_format": "$#,##0.00",
    },
    "Rock Boost": {
        "campaign_pattern": ["ROCKBOOST", "ROCK BOOST"],
        "currency_format": "$#,##0.00",
    },
}


def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def _fetch_week(date_from: str, date_to: str, campaign_pattern: str = None, network_id: str = None) -> list:
    """Busca criativos de uma semana. Suporta filtro por campanha e offer source."""
    use_campaign_group = bool(campaign_pattern)
    group = "rt_campaign,rt_ad" if use_campaign_group else "rt_ad"

    params = {
        "api_key": REDTRACK_API_KEY,
        "date_from": date_from,
        "date_to": date_to,
        "group": group,
        "sortby": "cost",
        "direction": "desc",
    }
    if network_id:
        params["network_id"] = network_id

    for attempt in range(3):
        try:
            response = requests.get(
                "https://api.redtrack.io/report",
                params=params,
                timeout=60,
            )
            if response.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"  Rate limit, aguardando {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            items = data if isinstance(data, list) else data.get("items", [])

            if campaign_pattern:
                patterns = list(campaign_pattern) if isinstance(campaign_pattern, (list, tuple)) else [campaign_pattern]
                items = [
                    i for i in items
                    if any(p.upper() in str(i.get("rt_campaign", "")).upper() for p in patterns)
                ]

            return items
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout {date_from}~{date_to}, aguardando {wait}s...")
            time.sleep(wait)
    return []


def fetch_all_creatives(date_from: str, date_to: str, campaign_pattern: str = None, network_id: str = None) -> dict:
    """Retorna dict {rt_ad_name: metrics} agregado por splits semanais."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    aggregated = {}

    current = start
    while current <= end:
        week_end = min(current + timedelta(days=6), end)
        wfrom = current.isoformat()
        wto = week_end.isoformat()
        print(f"  Semana {wfrom} → {wto}...")
        items = _fetch_week(wfrom, wto, campaign_pattern=campaign_pattern, network_id=network_id)

        for item in items:
            name = str(item.get("rt_ad", "")).strip()
            if not name:
                continue
            name = name.replace(".mp4", "").replace(".MP4", "").rstrip(".").strip()
            if not name:
                continue
            if name not in aggregated:
                aggregated[name] = {
                    "cost": 0.0,
                    "total_revenue": 0.0,
                    "convtype1": 0,
                    "convtype2": 0,
                    "_clicks": 0,
                }
            aggregated[name]["cost"] += float(item.get("cost", 0))
            aggregated[name]["total_revenue"] += float(item.get("total_revenue", 0))
            aggregated[name]["convtype1"] += int(item.get("convtype1", 0))
            aggregated[name]["convtype2"] += int(item.get("convtype2", 0))
            aggregated[name]["_clicks"] += int(item.get("ok", 0))

        current = week_end + timedelta(days=1)
        time.sleep(1)

    for name, data in aggregated.items():
        clicks = data.pop("_clicks", 0)
        data["cpc"] = round(data["cost"] / clicks, 4) if clicks > 0 else 0

    print(f"  Total: {len(aggregated)} criativos únicos encontrados")
    return aggregated


def idx_to_col(idx: int) -> str:
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def update_product_sheet(sheets, sheet_name: str, redtrack_data: dict, name_col: str = None, name_mapping: dict = None, sheet_id: int = None, currency_format: str = None):
    print(f"\n  → Atualizando aba '{sheet_name}'...")

    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet_name}'!A:AZ",
    ).execute()
    values = result.get("values", [])

    # Coluna de nome do criativo: usa name_col se definido, senão "CRIATIVO"
    search_col = (name_col or "CRIATIVO").upper().strip()

    header_row_idx = None
    col_map = {}
    for i, row in enumerate(values):
        row_upper = [c.upper().strip() for c in row]
        if search_col in row_upper or "CRIATIVO" in row_upper:
            header_row_idx = i
            for j, cell in enumerate(row_upper):
                if cell not in col_map:
                    col_map[cell] = j
            break

    if header_row_idx is None:
        print(f"    Cabeçalho não encontrado, pulando.")
        return

    criativo_col = col_map.get(search_col, col_map.get("CRIATIVO", -1))
    if criativo_col < 0:
        print(f"    Coluna '{search_col}' não encontrada, pulando.")
        return

    metric_cols = {}
    for col_name in ["CPC", "GASTOS", "IC", "CP/IC", "VENDAS", "FATURAMENTO", "CPA", "ROAS"]:
        if col_name in col_map:
            metric_cols[col_name] = col_map[col_name]
        # Also handle "ROAs" written with lowercase 'as'
    for key in col_map:
        if key.upper() == "ROAS" and key not in metric_cols:
            metric_cols["ROAS"] = col_map[key]

    # Build normalized lookup: hyphens and spaces treated as equivalent
    def _norm(s: str) -> str:
        return s.replace("-", " ").replace("  ", " ").strip().upper()

    normalized_index = {_norm(k): k for k in redtrack_data}

    updates = []
    matched = 0
    not_found = []

    for i, row in enumerate(values):
        if i <= header_row_idx:
            continue
        if len(row) <= criativo_col:
            continue
        raw_name = row[criativo_col].strip()
        if not raw_name:
            continue

        # Remove .mp4 extension for matching
        lookup_name = raw_name.replace(".mp4", "").replace(".MP4", "").strip()

        mapped_name = (name_mapping or {}).get(lookup_name, lookup_name)
        if mapped_name in redtrack_data:
            rt_key = mapped_name
        elif _norm(mapped_name) in normalized_index:
            rt_key = normalized_index[_norm(mapped_name)]
        else:
            not_found.append(lookup_name)
            continue

        matched += 1
        data = redtrack_data[rt_key]
        print(f"      ✓ {lookup_name}")
        cost = data["cost"]
        revenue = data["total_revenue"]
        vendas = data["convtype1"]
        ic = data["convtype2"]
        cpc = data["cpc"]
        cpa = round(cost / vendas, 2) if vendas > 0 else 0
        cp_ic = round(cost / ic, 2) if ic > 0 else 0
        roas = round(revenue / cost, 2) if cost > 0 else 0

        row_num = i + 1
        metric_values = {
            "CPC": round(cpc, 2),
            "GASTOS": round(cost, 2),
            "IC": ic,
            "VENDAS": vendas,
            "FATURAMENTO": round(revenue, 2),
        }

        for col_name, value in metric_values.items():
            if col_name in metric_cols:
                col_letter = idx_to_col(metric_cols[col_name])
                updates.append({
                    "range": f"'{sheet_name}'!{col_letter}{row_num}",
                    "values": [[value]],
                })

    print(f"    {matched} criativos atualizados | {len(not_found)} sem dados no Redtrack")
    if not_found:
        for name in not_found:
            print(f"      - {name}")

    if not updates:
        return

    chunk_size = 1000
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        sheets.values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "RAW", "data": chunk},
        ).execute()

    print(f"    {len(updates)} células escritas")

    # Aplica formato de moeda nas colunas monetárias, se configurado
    if currency_format and sheet_id is not None and metric_cols:
        monetary = [metric_cols[c] for c in ["CPC", "GASTOS", "FATURAMENTO", "CPA", "CP/IC"] if c in metric_cols]
        fmt_requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": header_row_idx + 1,
                        "endRowIndex": len(values),
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "CURRENCY", "pattern": currency_format}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
            for col_idx in monetary
        ]
        if fmt_requests:
            sheets.batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": fmt_requests},
            ).execute()
            print(f"    Formato {currency_format} aplicado em {len(monetary)} colunas monetárias")


def run(date_from: str = "2026-01-01", date_to: str = None, sheet_filter: str = None):
    if date_to is None:
        date_to = datetime.now().strftime("%Y-%m-%d")

    creds = get_google_credentials()
    service = build("sheets", "v4", credentials=creds)
    sheets = service.spreadsheets()

    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id_map = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    sheets_to_update = [
        s["properties"]["title"]
        for s in meta["sheets"]
        if not s["properties"].get("hidden", False)
        and s["properties"]["title"] not in IGNORED_SHEETS
    ]

    if sheet_filter:
        sheets_to_update = [s for s in sheets_to_update if sheet_filter.lower() in s.lower()]

    print(f"  Abas ativas: {sheets_to_update}")

    # Agrupa abas por configuração de filtro para evitar buscas redundantes
    # Abas com mesma config compartilham a mesma busca
    from collections import defaultdict
    config_groups = defaultdict(list)
    for sheet_name in sheets_to_update:
        cfg = SHEET_CONFIG.get(sheet_name, {})
        pat = cfg.get("campaign_pattern")
        key = (tuple(pat) if isinstance(pat, list) else pat, cfg.get("network_id"))
        config_groups[key].append(sheet_name)

    for (campaign_pattern, network_id), sheet_names in config_groups.items():
        filter_desc = []
        if campaign_pattern:
            filter_desc.append(f"campanha={campaign_pattern}")
        if network_id:
            filter_desc.append(f"offer_source={network_id[:8]}...")
        desc = f" [{', '.join(filter_desc)}]" if filter_desc else ""

        print(f"\n  Buscando criativos do Redtrack ({date_from} → {date_to}){desc}...")
        redtrack_data = fetch_all_creatives(
            date_from, date_to,
            campaign_pattern=campaign_pattern,
            network_id=network_id,
        )

        for sheet_name in sheet_names:
            try:
                cfg = SHEET_CONFIG.get(sheet_name, {})
                update_product_sheet(sheets, sheet_name, redtrack_data,
                                     name_col=cfg.get("name_col"),
                                     name_mapping=cfg.get("name_mapping"),
                                     sheet_id=sheet_id_map.get(sheet_name),
                                     currency_format=cfg.get("currency_format"))
            except Exception as e:
                print(f"    Erro na aba '{sheet_name}': {e}")

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Concluído!")


if __name__ == "__main__":
    import sys
    sheet_filter = None
    date_from = "2026-03-01"
    date_to = datetime.now().strftime("%Y-%m-%d")

    for arg in sys.argv[1:]:
        if arg.startswith("--sheet="):
            sheet_filter = arg.split("=", 1)[1]
        elif arg.startswith("--from="):
            date_from = arg.split("=", 1)[1]
        elif arg.startswith("--to="):
            date_to = arg.split("=", 1)[1]

    run(date_from=date_from, date_to=date_to, sheet_filter=sheet_filter)

"""
create_dg_campaigns.py — Criador de campanhas de Geração de Demanda (DG)

Lê a aba 'Campanhas DG' na planilha do Google Sheets.
Para cada linha com STATUS = PENDENTE:
  1. Cria campaign budget
  2. Cria campanha DG (inicia PAUSADA)
  3. Adiciona targeting de país e idioma
  4. Cria ad group
  5. Cria/encontra asset de vídeo do YouTube
  6. Faz upload do logo (via URL)
  7. Cria DG Video Responsive Ad
  8. Escreve STATUS, CAMPAIGN_ID e OBSERVACAO de volta na planilha

Uso:
  python create_dg_campaigns.py
  python create_dg_campaigns.py --dry-run   # valida sem criar nada
"""

import base64
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ── Planilha ─────────────────────────────────────────────────────────────────
SPREADSHEET_ID   = os.getenv("DG_SPREADSHEET_ID", "1blU5ClfHkW51kGpdG6eioVto99_0_29Ci6PjTajcSv0")
SHEET_NAME       = "Campanhas DG"
SCOPES           = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token_dg.json")

# ── Google Ads API ───────────────────────────────────────────────────────────
DEV_TOKEN     = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
CLIENT_ID     = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN")
MCC_ID        = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")
BASE          = "https://googleads.googleapis.com/v21"

# ── Mapeamentos de targeting ─────────────────────────────────────────────────
COUNTRY_CRITERIA = {
    "BR": "geoTargetConstants/2076",
    "US": "geoTargetConstants/2840",
    "MX": "geoTargetConstants/2484",
    "AR": "geoTargetConstants/2032",
    "CO": "geoTargetConstants/2170",
    "CL": "geoTargetConstants/2152",
    "PE": "geoTargetConstants/2604",
    "GB": "geoTargetConstants/2826",
    "CA": "geoTargetConstants/2124",
    "AU": "geoTargetConstants/2036",
}
LANGUAGE_CRITERIA = {
    "pt": "languageConstants/1014",
    "en": "languageConstants/1000",
    "es": "languageConstants/1003",
}

# ── Colunas da aba (0-indexed, mesma ordem do cabeçalho) ─────────────────────
COL = {
    "ACCOUNT_ID":       0,
    "NOME_CAMPANHA":    1,
    "ORCAMENTO_DIARIO": 2,
    "VIDEO_URL":        3,
    "URL_FINAL":        4,
    "TITULO_1":         5,
    "TITULO_2":         6,
    "DESCRICAO":        7,
    "CTA":              8,
    "LOGO_URL":         9,
    "PAIS":             10,
    "ESTRATEGIA":       11,
    "TARGET_CPA":       12,
    "STATUS":           13,
    "CAMPAIGN_ID":      14,
    "OBSERVACAO":       15,
}
HEADER = list(COL.keys())


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheets_client():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()


def ensure_sheet_header(sheets):
    """Cria o cabeçalho se a aba estiver vazia."""
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:P1",
    ).execute()
    if not result.get("values"):
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        print(f"  Cabeçalho criado na aba '{SHEET_NAME}'.")


def read_pending_rows(sheets) -> list[dict]:
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A:P",
    ).execute()
    rows = result.get("values", [])
    pending = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        while len(row) < len(COL):
            row.append("")
        status = row[COL["STATUS"]].strip().upper()
        if status == "PENDENTE":
            pending.append({"row_index": i + 1, "data": row})
    return pending


def update_row(sheets, row_num: int, status: str, campaign_id: str = "", obs: str = ""):
    updates = [
        {"range": f"'{SHEET_NAME}'!N{row_num}", "values": [[status]]},
        {"range": f"'{SHEET_NAME}'!O{row_num}", "values": [[campaign_id]]},
        {"range": f"'{SHEET_NAME}'!P{row_num}", "values": [[obs]]},
    ]
    sheets.values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()


# ── Google Ads API helpers ─────────────────────────────────────────────────────

def get_access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def ads_headers(token: str, customer_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }


def ads_post(token: str, customer_id: str, endpoint: str, body: dict) -> dict:
    url = f"{BASE}/customers/{customer_id}/{endpoint}"
    r = requests.post(url, headers=ads_headers(token, customer_id), json=body, timeout=60)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def ads_search(token: str, customer_id: str, query: str) -> list:
    url = f"{BASE}/customers/{customer_id}/googleAds:searchStream"
    r = requests.post(
        url,
        headers=ads_headers(token, customer_id),
        json={"query": query},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    results = []
    for batch in r.json():
        results.extend(batch.get("results", []))
    return results


# ── Utilitários ───────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str:
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url.strip()):
        return url.strip()
    raise ValueError(f"ID do YouTube não encontrado em: {url}")


def normalize_customer_id(raw: str) -> str:
    return raw.replace("-", "").strip()


# ── Passos da criação ─────────────────────────────────────────────────────────

def create_budget(token, customer_id, name, daily_usd) -> str:
    body = {"operations": [{"create": {
        "name": f"Budget — {name}",
        "amountMicros": str(int(float(daily_usd) * 1_000_000)),
        "deliveryMethod": "STANDARD",
    }}]}
    resp = ads_post(token, customer_id, "campaignBudgets:mutate", body)
    return resp["results"][0]["resourceName"]


def create_campaign(token, customer_id, name, budget_resource, strategy, target_cpa_usd) -> str:
    campaign = {
        "name": name,
        "advertisingChannelType": "DEMAND_GEN",
        "status": "PAUSED",
        "campaignBudget": budget_resource,
    }
    strategy = (strategy or "MAXIMIZE_CONVERSIONS").upper().strip()
    if strategy == "TARGET_CPA" and target_cpa_usd:
        campaign["targetCpa"] = {
            "targetCpaMicros": str(int(float(target_cpa_usd) * 1_000_000))
        }
    else:
        campaign["maximizeConversions"] = {}

    body = {"operations": [{"create": campaign}]}
    resp = ads_post(token, customer_id, "campaigns:mutate", body)
    return resp["results"][0]["resourceName"]


def add_campaign_criteria(token, customer_id, campaign_resource, country, language):
    ops = []
    country_key = (country or "").upper().strip()
    if country_key in COUNTRY_CRITERIA:
        ops.append({"create": {
            "campaign": campaign_resource,
            "location": {"geoTargetConstant": COUNTRY_CRITERIA[country_key]},
        }})
    lang_key = (language or "").lower().strip()
    if lang_key in LANGUAGE_CRITERIA:
        ops.append({"create": {
            "campaign": campaign_resource,
            "language": {"languageConstant": LANGUAGE_CRITERIA[lang_key]},
        }})
    if not ops:
        return
    ads_post(token, customer_id, "campaignCriteria:mutate", {"operations": ops})


def create_ad_group(token, customer_id, campaign_resource, name) -> str:
    body = {"operations": [{"create": {
        "campaign": campaign_resource,
        "name": f"{name} — AdGroup",
        "status": "ENABLED",
        "type": "DEMAND_GEN",
    }}]}
    resp = ads_post(token, customer_id, "adGroups:mutate", body)
    return resp["results"][0]["resourceName"]


def get_or_create_video_asset(token, customer_id, video_url) -> str:
    video_id = extract_video_id(video_url)

    # Verifica se o asset já existe na conta
    rows = ads_search(token, customer_id,
        f"SELECT asset.resource_name FROM asset "
        f"WHERE asset.type = 'YOUTUBE_VIDEO' "
        f"AND asset.youtube_video_asset.youtube_video_id = '{video_id}'"
    )
    if rows:
        resource = rows[0]["asset"]["resourceName"]
        print(f"      Vídeo asset já existe: {resource}")
        return resource

    body = {"operations": [{"create": {
        "name": f"YT Video — {video_id}",
        "type": "YOUTUBE_VIDEO",
        "youtubeVideoAsset": {"youtubeVideoId": video_id},
    }}]}
    resp = ads_post(token, customer_id, "assets:mutate", body)
    return resp["results"][0]["resourceName"]


def upload_logo_asset(token, customer_id, logo_url) -> str:
    r = requests.get(logo_url, timeout=30)
    r.raise_for_status()
    image_data = base64.b64encode(r.content).decode()
    body = {"operations": [{"create": {
        "name": f"Logo — {int(time.time())}",
        "type": "IMAGE",
        "imageAsset": {"data": image_data},
    }}]}
    resp = ads_post(token, customer_id, "assets:mutate", body)
    return resp["results"][0]["resourceName"]


def create_dg_video_ad(token, customer_id, ad_group_resource, row_data,
                       video_asset, logo_asset, url_final) -> str:
    headlines = []
    for col in ["TITULO_1", "TITULO_2"]:
        text = row_data[COL[col]].strip()
        if text:
            headlines.append({"text": text})

    desc = row_data[COL["DESCRICAO"]].strip()
    cta  = row_data[COL["CTA"]].strip() or "Saiba mais"
    name = row_data[COL["NOME_CAMPANHA"]].strip()

    ad = {
        "name": f"{name} — Video Ad",
        "finalUrls": [url_final],
        "demandGenVideoResponsiveAd": {
            "headlines": headlines,
            "descriptions": [{"text": desc}],
            "callToActions": [{"text": cta}],
            "videos": [{"asset": video_asset}],
            "logoImages": [{"asset": logo_asset}],
        },
    }
    body = {"operations": [{"create": {
        "adGroup": ad_group_resource,
        "status": "ENABLED",
        "ad": ad,
    }}]}
    resp = ads_post(token, customer_id, "adGroupAds:mutate", body)
    return resp["results"][0]["resourceName"]


# ── Pipeline principal ────────────────────────────────────────────────────────

def process_row(token, sheets, row_num, row_data, dry_run=False) -> bool:
    customer_id  = normalize_customer_id(row_data[COL["ACCOUNT_ID"]])
    name         = row_data[COL["NOME_CAMPANHA"]].strip()
    budget       = row_data[COL["ORCAMENTO_DIARIO"]].strip()
    video_url    = row_data[COL["VIDEO_URL"]].strip()
    url_final    = row_data[COL["URL_FINAL"]].strip()
    logo_url     = row_data[COL["LOGO_URL"]].strip()
    country      = row_data[COL["PAIS"]].strip()
    language     = "pt" if country == "BR" else "en"
    strategy     = row_data[COL["ESTRATEGIA"]].strip()
    target_cpa   = row_data[COL["TARGET_CPA"]].strip()

    # Validação mínima
    missing = []
    for field, val in [
        ("ACCOUNT_ID", customer_id), ("NOME_CAMPANHA", name),
        ("ORCAMENTO_DIARIO", budget), ("VIDEO_URL", video_url),
        ("URL_FINAL", url_final), ("TITULO_1", row_data[COL["TITULO_1"]].strip()),
        ("DESCRICAO", row_data[COL["DESCRICAO"]].strip()),
        ("LOGO_URL", logo_url),
    ]:
        if not val:
            missing.append(field)
    if missing:
        msg = f"Campos obrigatórios ausentes: {', '.join(missing)}"
        print(f"    ERRO: {msg}")
        if not dry_run:
            update_row(sheets, row_num, "ERRO", obs=msg)
        return False

    print(f"\n  [{row_num}] {name} → conta {customer_id}")

    if dry_run:
        print(f"    DRY RUN — tudo válido, nada criado.")
        return True

    try:
        print(f"    1/6 Budget ${budget}/dia...")
        budget_res = create_budget(token, customer_id, name, budget)

        print(f"    2/6 Campanha DG...")
        campaign_res = create_campaign(token, customer_id, name, budget_res, strategy, target_cpa)
        campaign_id  = campaign_res.split("/")[-1]

        print(f"    3/6 Targeting ({country} / {language})...")
        add_campaign_criteria(token, customer_id, campaign_res, country, language)

        print(f"    4/6 Ad Group...")
        ad_group_res = create_ad_group(token, customer_id, campaign_res, name)

        print(f"    5/6 Video asset ({video_url})...")
        video_asset = get_or_create_video_asset(token, customer_id, video_url)

        print(f"    5/6 Logo asset ({logo_url[:60]}...)...")
        logo_asset = upload_logo_asset(token, customer_id, logo_url)

        print(f"    6/6 DG Video Responsive Ad...")
        create_dg_video_ad(token, customer_id, ad_group_res, row_data,
                           video_asset, logo_asset, url_final)

        update_row(sheets, row_num, "CRIADA", campaign_id=campaign_id)
        print(f"    ✅ CRIADA — campaign_id={campaign_id}")
        return True

    except Exception as e:
        msg = str(e)[:200]
        print(f"    ❌ ERRO: {msg}")
        update_row(sheets, row_num, "ERRO", obs=msg)
        return False


def main():
    dry_run = "--dry-run" in sys.argv

    sheets = get_sheets_client()
    ensure_sheet_header(sheets)

    pending = read_pending_rows(sheets)
    if not pending:
        print("Nenhuma linha com STATUS=PENDENTE encontrada.")
        return

    print(f"{'[DRY RUN] ' if dry_run else ''}{len(pending)} campanha(s) para processar...")

    token = get_access_token()
    ok = err = 0
    for item in pending:
        success = process_row(token, sheets, item["row_index"], item["data"], dry_run)
        if success:
            ok += 1
        else:
            err += 1
        time.sleep(1)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Concluído: {ok} criada(s), {err} erro(s).")


if __name__ == "__main__":
    main()

"""
pull_account_data.py — Puxa métricas de todas as contas autenticadas via token individual.

Não usa login-customer-id (sem referência à MCC).
Cada conta é acessada com o próprio refresh_token salvo em account_tokens.json.

Uso:
  python pull_account_data.py                        # todas as contas
  python pull_account_data.py --account=123-456-7890 # conta específica
  python pull_account_data.py --days=7               # últimos N dias (padrão: 30)
"""

import argparse
import json
import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

TOKENS_FILE = os.path.join(os.path.dirname(__file__), "account_tokens.json")
DEV_TOKEN   = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
BASE        = "https://googleads.googleapis.com/v21"

# Query GAQL — ajuste as métricas conforme necessário
QUERY = """
    SELECT
      campaign.id,
      campaign.name,
      campaign.status,
      metrics.cost_micros,
      metrics.conversions,
      metrics.conversions_value,
      metrics.impressions,
      metrics.clicks,
      segments.date,
      customer.currency_code
    FROM campaign
    WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
      AND campaign.status != 'REMOVED'
    ORDER BY metrics.cost_micros DESC
"""


def get_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def pull_account(account_id: str, token_data: dict, date_from: str, date_to: str) -> list:
    customer_id = account_id.replace("-", "")

    try:
        access_token = get_access_token(
            token_data["refresh_token"],
            token_data["client_id"],
            token_data["client_secret"],
        )
    except Exception as e:
        print(f"  ❌ {account_id} — erro ao obter token: {e}")
        return []

    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": DEV_TOKEN,
        # SEM login-customer-id — sem referência à MCC
        "Content-Type": "application/json",
    }

    query = QUERY.format(date_from=date_from, date_to=date_to)
    url = f"{BASE}/customers/{customer_id}/googleAds:searchStream"

    try:
        r = requests.post(url, headers=headers, json={"query": query}, timeout=60)
        if r.status_code == 401:
            print(f"  ⚠️  {account_id} — token expirado ou revogado (re-autentique com auth_account.py)")
            return []
        if not r.ok:
            print(f"  ❌ {account_id} — HTTP {r.status_code}: {r.text[:200]}")
            return []
    except Exception as e:
        print(f"  ❌ {account_id} — {e}")
        return []

    rows = []
    for batch in r.json():
        for result in batch.get("results", []):
            campaign  = result.get("campaign", {})
            metrics   = result.get("metrics", {})
            segments  = result.get("segments", {})
            customer  = result.get("customer", {})
            cost      = int(metrics.get("costMicros", 0)) / 1_000_000
            rows.append({
                "account_id":    account_id,
                "currency":      customer.get("currencyCode", "???"),
                "date":          segments.get("date"),
                "campaign_id":   campaign.get("id"),
                "campaign":      campaign.get("name"),
                "status":        campaign.get("status"),
                "cost":          round(cost, 2),
                "impressions":   int(metrics.get("impressions", 0)),
                "clicks":        int(metrics.get("clicks", 0)),
                "conversions":   float(metrics.get("conversions", 0)),
                "revenue":       float(metrics.get("conversionsValue", 0)),
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", default=None, help="Conta específica (opcional)")
    parser.add_argument("--days",    type=int, default=30, help="Últimos N dias (padrão: 30)")
    args = parser.parse_args()

    if not os.path.exists(TOKENS_FILE):
        print("Nenhuma conta autenticada ainda. Use auth_account.py primeiro.")
        return

    with open(TOKENS_FILE, encoding="utf-8") as f:
        all_tokens = json.load(f)

    if args.account:
        acc = args.account.strip()
        if acc not in all_tokens:
            print(f"Conta {acc} não encontrada em {TOKENS_FILE}.")
            return
        accounts = {acc: all_tokens[acc]}
    else:
        accounts = all_tokens

    date_to   = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=args.days)).isoformat()
    print(f"Período: {date_from} → {date_to} | Contas: {len(accounts)}\n")

    all_rows = []
    for account_id, token_data in accounts.items():
        print(f"  Puxando {account_id}...")
        rows = pull_account(account_id, token_data, date_from, date_to)
        all_rows.extend(rows)
        if rows:
            total_cost = sum(r["cost"] for r in rows)
            currency   = rows[0].get("currency", "???")
            print(f"    ✅ {len(rows)} linhas | gasto total: {currency} {total_cost:,.2f}")

    print(f"\nTotal: {len(all_rows)} linhas de {len(accounts)} conta(s).")

    # Salva resultado em JSON para uso posterior
    out_file = os.path.join(os.path.dirname(__file__), "ads_data.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2, ensure_ascii=False)
    print(f"Dados salvos em: {out_file}")


if __name__ == "__main__":
    main()

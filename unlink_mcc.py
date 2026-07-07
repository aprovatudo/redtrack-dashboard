"""
unlink_mcc.py — Remove o vínculo MCC de todas as contas clientes ativas.

Uso:
  python unlink_mcc.py            # dry-run: mostra quais seriam desvinculadas
  python unlink_mcc.py --confirm  # executa a desvinculação de fato
"""

import argparse
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

DEV_TOKEN     = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
CLIENT_ID     = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN")
MCC_ID        = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")

API_BASE = "https://googleads.googleapis.com/v21"


def get_access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def list_active_links(token: str) -> list[dict]:
    """Lista todos os vínculos ACTIVE ou PENDING da MCC."""
    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }
    query = """
        SELECT
          customer_client_link.resource_name,
          customer_client_link.client_customer,
          customer_client_link.status,
          customer_client_link.manager_link_id
        FROM customer_client_link
        WHERE customer_client_link.status IN ('ACTIVE', 'PENDING')
    """
    resp = requests.post(
        f"{API_BASE}/customers/{MCC_ID}/googleAds:searchStream",
        headers=headers,
        json={"query": query},
        timeout=30,
    )
    resp.raise_for_status()

    links = []
    for batch in resp.json():
        for row in batch.get("results", []):
            link = row.get("customerClientLink", {})
            # client_customer vem como "customers/XXXXXXXXXX"
            client_id = link.get("clientCustomer", "").replace("customers/", "")
            links.append({
                "resource_name": link.get("resourceName", ""),
                "client_id": client_id,
                "status": link.get("status", ""),
            })
    return links


def _client_manager_resource_name(client_link_rn: str) -> tuple[str, str] | None:
    """
    Converte resource_name do lado do manager para o lado do cliente.
    'customers/MCC/customerClientLinks/CLIENT~LINK_ID'
    → client_id, 'customers/CLIENT/customerManagerLinks/MCC~LINK_ID'
    """
    # Extrai partes: customers/{mcc}/customerClientLinks/{client}~{link_id}
    import re
    m = re.match(
        r'customers/(\d+)/customerClientLinks/(\d+)~(\d+)',
        client_link_rn,
    )
    if not m:
        return None
    mcc_id, client_id, link_id = m.group(1), m.group(2), m.group(3)
    rn = f"customers/{client_id}/customerManagerLinks/{mcc_id}~{link_id}"
    return client_id, rn


def unlink(token: str, resource_name: str, status: str = "INACTIVE") -> bool:
    """
    Remove o vínculo MCC via customerClientLinks:mutate (v21).
    ACTIVE → INACTIVE | PENDING → CANCELED
    O endpoint v21 usa "operation" (singular) em vez de "operations" (array).
    """
    if not resource_name or "customerClientLinks" not in resource_name:
        print(f"    ⚠️  resource_name inválido — pulando")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }
    body = {
        "operation": {
            "update": {
                "resourceName": resource_name,
                "status": status,
            },
            "updateMask": "status",
        }
    }

    for attempt in range(3):
        resp = requests.post(
            f"{API_BASE}/customers/{MCC_ID}/customerClientLinks:mutate",
            headers=headers,
            json=body,
            timeout=15,
        )
        if resp.ok:
            return True
        if resp.status_code in (429, 500, 503):
            time.sleep((attempt + 1) * 3)
            continue
        print(f"\n    Erro {resp.status_code}: {resp.text[:250]}")
        return False
    return False


def debug_one(token: str, resource_name: str):
    """Faz 1 chamada com debug completo para diagnóstico."""
    import json as _json

    body = {
        "mutateOperations": [{
            "customerClientLinkOperation": {
                "update": {"resourceName": resource_name, "status": "INACTIVE"},
                "updateMask": "status",
            }
        }]
    }
    body_str = _json.dumps(body)

    print(f"\nResource name: {resource_name}")
    print(f"Body JSON:\n{_json.dumps(body, indent=2)}")

    hdrs = {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }

    base = f"https://googleads.googleapis.com/v21/customers/{MCC_ID}/customerClientLinks:mutate"
    tests = [
        # formato resource direto (flat)
        ("resource direto",       {"resourceName": resource_name, "status": "INACTIVE"}),
        # singular operation
        ("operation singular",    {"operation": {"update": {"resourceName": resource_name, "status": "INACTIVE"}, "updateMask": "status"}}),
        # update direto
        ("update direto",         {"update": {"resourceName": resource_name, "status": "INACTIVE"}, "updateMask": "status"}),
        # customerClientLink wrapper
        ("customerClientLink",    {"customerClientLink": {"resourceName": resource_name, "status": "INACTIVE"}, "updateMask": "status"}),
    ]
    for label, test_body in tests:
        resp = requests.post(base, headers=hdrs, json=test_body, timeout=15)
        snippet = resp.text[:200].replace("\n", " ")
        print(f"\n[{resp.status_code}] {label}  →  {snippet}")


def main(confirm: bool, test: bool = False):
    print("Obtendo token...")
    token = get_access_token()
    print(f"Token OK | MCC: {MCC_ID}\n")

    print("Listando vínculos ativos...")
    links = list_active_links(token)
    print(f"{len(links)} vínculo(s) encontrado(s)\n")

    if not links:
        print("Nenhum vínculo ativo. Nada a fazer.")
        return

    if test:
        debug_one(token, links[0]["resource_name"])
        return

    if not confirm:
        print("[DRY RUN — use --confirm para executar de fato]\n")
        for lk in links[:5]:
            print(f"  client={lk['client_id']}  status={lk['status']}")
            print(f"    resource_name={lk['resource_name']}")
        print(f"  ... (+{len(links)-5} mais)")
        print(f"\nTotal: {len(links)} vínculos seriam removidos.")
        return

    ok = err = 0
    for i, lk in enumerate(links, 1):
        client = lk["client_id"]
        rname  = lk["resource_name"]
        print(f"[{i}/{len(links)}] {client}  ", end="", flush=True)
        # PENDING → CANCELED; ACTIVE → INACTIVE
        target_status = "CANCELED" if lk["status"] == "PENDING" else "INACTIVE"
        if unlink(token, rname, status=target_status):
            print("✅")
            ok += 1
        else:
            print("❌")
            err += 1
        time.sleep(0.5)

    print(f"\n{'─'*45}")
    print(f"  Desvinculados: {ok}  |  Erros: {err}")
    print(f"{'─'*45}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="Executa a desvinculação (sem essa flag é dry-run)")
    parser.add_argument("--test", action="store_true",
                        help="Debug: testa uma chamada com output completo")
    args = parser.parse_args()
    main(confirm=args.confirm, test=args.test)

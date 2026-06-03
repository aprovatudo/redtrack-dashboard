"""
Envia convites de vinculação MCC para contas do Google Ads.
Processa em lotes de 20 (limite da API) e pausa entre lotes.

Uso:
    python invite_accounts.py
    python invite_accounts.py --dry-run   (simula sem enviar)
    python invite_accounts.py --batch=20  (tamanho do lote, default 20)
"""

import os
import sys
import time
import argparse
import requests as http
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
INVITED_FILE = os.path.join(os.path.dirname(__file__), "accounts_invited.txt")
FAILED_FILE = os.path.join(os.path.dirname(__file__), "accounts_failed.txt")

MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")


def get_access_token() -> str:
    resp = http.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def load_accounts():
    """Retorna apenas IDs de contas sem status (📤 Não enviado), ignorando ✅ ⏳ 🚫."""
    accounts = []
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Pula contas com status já definido
            if line.startswith(("✅", "⏳", "🚫")):
                continue
            # Extrai só o ID (parte antes do " | ")
            raw = line.lstrip()
            acc_id = raw.split(" | ")[0].strip()
            if acc_id:
                accounts.append(acc_id)
    return accounts


def load_already_invited():
    if not os.path.exists(INVITED_FILE):
        return set()
    with open(INVITED_FILE) as f:
        return set(line.strip() for line in f if line.strip())


def send_invite(access_token: str, account_id: str) -> tuple[bool, str]:
    customer_id_clean = account_id.replace("-", "")
    url = f"https://googleads.googleapis.com/v20/customers/{MCC_ID}/customerClientLinks:mutate"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }
    body = {
        "operation": {
            "create": {
                "clientCustomer": f"customers/{customer_id_clean}",
                "status": "PENDING",
            }
        }
    }
    try:
        resp = http.post(url, headers=headers, json=body, timeout=15)
        raw = resp.text
        if not raw:
            return False, f"HTTP {resp.status_code} resposta vazia"
        data = resp.json()
        if resp.status_code == 200:
            resource = data.get("result", {}).get("resourceName", "ok")
            return True, resource
        msg = data.get("error", {}).get("message", str(data))
        return False, f"HTTP {resp.status_code}: {msg}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)} | raw: {resp.text[:100] if 'resp' in dir() else '?'}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", type=int, default=20)
    args = parser.parse_args()

    accounts = load_accounts()
    already_invited = load_already_invited()

    pending = [a for a in accounts if a not in already_invited]

    print(f"MCC: {MCC_ID}")
    print(f"Total na lista: {len(accounts)}")
    print(f"Já convidados:  {len(already_invited)}")
    print(f"Pendentes:      {len(pending)}")

    if not pending:
        print("\nTodos já foram convidados!")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Simulando envio de {len(pending)} convites em lotes de {args.batch}:")
        for i, acc in enumerate(pending[:args.batch * 2]):
            print(f"  {i+1:3}. {acc}")
        return

    print("\nObtendo access token...")
    access_token = get_access_token()

    invited_log = open(INVITED_FILE, "a")
    failed_log = open(FAILED_FILE, "a")

    total_sent = 0
    total_failed = 0

    for batch_start in range(0, len(pending), args.batch):
        batch = pending[batch_start:batch_start + args.batch]
        batch_num = batch_start // args.batch + 1
        total_batches = (len(pending) + args.batch - 1) // args.batch

        print(f"\n--- Lote {batch_num}/{total_batches} ({len(batch)} contas) ---")

        for acc_id in batch:
            ok, msg = send_invite(access_token, acc_id)
            if ok:
                print(f"  ✓ {acc_id}")
                invited_log.write(acc_id + "\n")
                invited_log.flush()
                total_sent += 1
            else:
                print(f"  ✗ {acc_id}: {msg[:80]}")
                failed_log.write(f"{acc_id} | {msg}\n")
                failed_log.flush()
                total_failed += 1
            time.sleep(0.3)

        if batch_start + args.batch < len(pending):
            print(f"\n  {total_sent} enviados até agora. Aguarde aceitar os convites pendentes.")
            print(f"  Pressione ENTER para enviar o próximo lote ou CTRL+C para pausar.")
            input()

    invited_log.close()
    failed_log.close()

    print(f"\n{'='*50}")
    print(f"Concluído: {total_sent} enviados, {total_failed} falhas")
    if total_failed:
        print(f"Veja accounts_failed.txt para detalhes dos erros")


if __name__ == "__main__":
    main()

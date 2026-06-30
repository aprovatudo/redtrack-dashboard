"""
auth_account.py — Autentica uma conta Google Ads individualmente, sem MCC.

O dono da conta abre o browser, faz login com as credenciais DELE e autoriza.
O refresh_token é salvo em account_tokens.json para uso futuro.

Uso:
  python auth_account.py --account=123-456-7890
"""

import argparse
import json
import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

TOKENS_FILE   = os.path.join(os.path.dirname(__file__), "account_tokens.json")
CLIENT_ID     = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
SCOPES        = ["https://www.googleapis.com/auth/adwords"]


def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True,
                        help="ID da conta Google Ads (ex: 123-456-7890)")
    args = parser.parse_args()

    account_id = args.account.strip()

    tokens = load_tokens()
    if account_id in tokens:
        print(f"Conta {account_id} já autenticada. Use --force para reatenticar.")
        return

    print(f"\nAutenticando conta: {account_id}")
    print("O browser vai abrir — faça login com as credenciais DESTA conta (não da MCC).\n")

    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=8080,
        authorization_prompt_message="Abrindo browser para autenticação...",
        success_message="Autorizado! Pode fechar esta aba.",
        open_browser=True,
    )

    tokens[account_id] = {
        "refresh_token": creds.refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    save_tokens(tokens)

    total = len(tokens)
    print(f"\n✅ Conta {account_id} autenticada e salva.")
    print(f"   Total de contas autenticadas: {total}")
    print(f"   Arquivo: {TOKENS_FILE}")


if __name__ == "__main__":
    main()

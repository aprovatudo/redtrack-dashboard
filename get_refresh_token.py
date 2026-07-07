"""
Gera o refresh_token para a Google Ads API.
Execute uma única vez — o token gerado não expira.

Uso:
    python get_refresh_token.py
"""

import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")

SCOPES = ["https://www.googleapis.com/auth/adwords"]

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
credentials = flow.run_local_server(
    host="localhost",
    port=8080,
    authorization_prompt_message="Abrindo navegador para autenticação...",
    success_message="Autenticação concluída! Pode fechar esta aba.",
    open_browser=True,
)

print("\n" + "="*50)
print("REFRESH TOKEN GERADO COM SUCESSO:")
print("="*50)
print(f"\nGOOGLE_ADS_REFRESH_TOKEN={credentials.refresh_token}")
print("\nCopie o valor acima e adicione no seu .env")
print("="*50)

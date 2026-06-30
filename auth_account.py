"""
auth_account.py — Autentica contas Google Ads via AdsPower, sem MCC.

Abre o perfil AdsPower da conta, navega para a URL de OAuth do Google,
captura o token automaticamente e salva em account_tokens.json.

Uso:
  python auth_account.py --profile=PROFILE_ID          # ID do perfil no AdsPower
  python auth_account.py --gmail=conta@gmail.com        # busca perfil pelo Gmail
  python auth_account.py --gmail=conta@gmail.com --account=123-456-7890
  python auth_account.py --all                          # autentica todos os perfis do AdsPower
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TOKENS_FILE   = os.path.join(os.path.dirname(__file__), "account_tokens.json")
CLIENT_ID     = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
ADSPOWER_URL  = "http://local.adspower.net:50325"
REDIRECT_URI  = "http://localhost:8080"
SCOPES        = "https://www.googleapis.com/auth/adwords"
AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL     = "https://oauth2.googleapis.com/token"

# Resultado capturado pelo servidor HTTP
_auth_code: str | None = None
_auth_event = threading.Event()


# ── Servidor local para capturar o callback OAuth ────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code   = params.get("code", [None])[0]
        if code:
            _auth_code = code
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Autorizado! Pode fechar esta aba.</h2>")
            _auth_event.set()
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>Erro: codigo nao encontrado.</h2>")

    def log_message(self, *args):
        pass  # silencia os logs do servidor


def start_callback_server() -> HTTPServer:
    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ── AdsPower ──────────────────────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    profiles = []
    page = 1
    while True:
        r = requests.get(f"{ADSPOWER_URL}/api/v1/user/list",
                         params={"page": page, "page_size": 100}, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            break
        users = data["data"].get("list", [])
        if not users:
            break
        profiles.extend(users)
        page += 1
        if page > 50:
            break
    return profiles


def find_profile_by_gmail(gmail: str) -> dict | None:
    for p in list_profiles():
        if p.get("username", "").lower() == gmail.lower():
            return p
    return None


def open_profile(profile_id: str) -> str:
    r = requests.get(f"{ADSPOWER_URL}/api/v1/browser/start",
                     params={"user_id": profile_id}, timeout=30)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Falha ao abrir perfil {profile_id}: {data}")
    ws = data["data"]["ws"]["puppeteer"]
    print(f"    Browser aberto (port={data['data']['debug_port']})")
    return ws


def close_profile(profile_id: str):
    requests.get(f"{ADSPOWER_URL}/api/v1/browser/stop",
                 params={"user_id": profile_id}, timeout=10)


# ── OAuth ─────────────────────────────────────────────────────────────────────

def build_auth_url() -> str:
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    return f"{AUTH_URL}?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    r = requests.post(TOKEN_URL, data={
        "code":          code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=15)
    r.raise_for_status()
    return r.json()


# ── Token storage ─────────────────────────────────────────────────────────────

def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


# ── Fluxo principal por perfil ────────────────────────────────────────────────

def auth_profile(profile: dict, account_id: str | None = None, force: bool = False) -> bool:
    global _auth_code, _auth_event

    profile_id = profile["user_id"]
    gmail      = profile.get("username", profile_id)
    label      = account_id or gmail

    tokens = load_tokens()
    key    = account_id or gmail

    if key in tokens and not force:
        print(f"  ⏭  {label} — já autenticado (use --force para re-autenticar)")
        return True

    print(f"\n  Autenticando: {label}  (perfil={profile_id})")

    # Reseta estado do callback
    _auth_code  = None
    _auth_event = threading.Event()

    server = start_callback_server()
    auth_url = build_auth_url()

    ws = open_profile(profile_id)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            context = browser.contexts[0]
            page    = context.pages[0] if context.pages else context.new_page()

            print(f"    Navegando para OAuth...")
            page.goto(auth_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)

            # Se pediu para selecionar conta — escolhe a primeira
            try:
                account_btns = page.locator("div[data-identifier]")
                if account_btns.count() > 0:
                    account_btns.first.click()
                    time.sleep(2)
            except Exception:
                pass

            # Clica em "Permitir" / "Allow"
            for text in ["Permitir", "Allow", "Continuar", "Continue"]:
                try:
                    btn = page.locator(f"button:has-text('{text}')")
                    if btn.count() > 0:
                        btn.first.click()
                        print(f"    Clicou '{text}'")
                        break
                except Exception:
                    pass

            # Aguarda o callback (máx 30s)
            _auth_event.wait(timeout=30)

            browser.close()
    except Exception as e:
        print(f"    ❌ Erro no browser: {e}")
        close_profile(profile_id)
        server.shutdown()
        return False

    close_profile(profile_id)
    server.shutdown()

    if not _auth_code:
        print(f"    ❌ Código OAuth não recebido.")
        return False

    try:
        token_data = exchange_code(_auth_code)
    except Exception as e:
        print(f"    ❌ Erro ao trocar código: {e}")
        return False

    if "refresh_token" not in token_data:
        print(f"    ❌ refresh_token ausente na resposta: {token_data}")
        return False

    tokens[key] = {
        "refresh_token": token_data["refresh_token"],
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "gmail":         gmail,
    }
    save_tokens(tokens)
    print(f"    ✅ Token salvo para {label}")
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile",  help="ID do perfil no AdsPower")
    parser.add_argument("--gmail",    help="Gmail da conta (busca o perfil automaticamente)")
    parser.add_argument("--account",  help="ID Google Ads para usar como chave (ex: 123-456-7890)")
    parser.add_argument("--all",      action="store_true", help="Autentica todos os perfis")
    parser.add_argument("--force",    action="store_true", help="Re-autentica mesmo se já existir")
    args = parser.parse_args()

    if args.all:
        profiles = list_profiles()
        print(f"{len(profiles)} perfis encontrados no AdsPower.")
        ok = err = skip = 0
        for p in profiles:
            result = auth_profile(p, force=args.force)
            if result:
                tokens = load_tokens()
                key = p.get("username", p["user_id"])
                if key in tokens:
                    ok += 1
                else:
                    skip += 1
            else:
                err += 1
            time.sleep(2)
        print(f"\nConcluído: {ok} autenticados, {skip} pulados, {err} erros.")
        return

    if args.profile:
        profiles = list_profiles()
        profile  = next((p for p in profiles if p["user_id"] == args.profile), None)
        if not profile:
            print(f"Perfil {args.profile} não encontrado no AdsPower.")
            return
        auth_profile(profile, account_id=args.account, force=args.force)
        return

    if args.gmail:
        profile = find_profile_by_gmail(args.gmail)
        if not profile:
            print(f"Perfil com Gmail '{args.gmail}' não encontrado no AdsPower.")
            return
        auth_profile(profile, account_id=args.account, force=args.force)
        return

    parser.print_help()


if __name__ == "__main__":
    main()

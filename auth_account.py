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

TOKENS_FILE       = os.path.join(os.path.dirname(__file__), "account_tokens.json")
TOTP_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "totp_secrets.json")
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


def get_active_browser_ws() -> tuple[str, str] | None:
    """Retorna (ws_url, user_id) do primeiro browser aberto no AdsPower."""
    endpoints = [
        "/api/v1/browser/local-active-url",
        "/api/v1/browser/local-active",
        "/api/v1/browser/active",
    ]
    for ep in endpoints:
        try:
            r = requests.get(f"{ADSPOWER_URL}{ep}", timeout=5)
            if not r.text.strip():
                continue
            data = r.json()
            if data.get("code") == 0 and data.get("data"):
                items = data["data"] if isinstance(data["data"], list) else [data["data"]]
                if items:
                    item = items[0]
                    ws  = (item.get("ws", {}) or {}).get("puppeteer") or item.get("puppeteer", "")
                    uid = item.get("user_id", "")
                    if ws:
                        return ws, uid
        except Exception:
            continue

    # Fallback: usa lsof para encontrar portas do SunBrowser/Chrome abertas
    print("  Escaneando processos do browser...")
    import subprocess
    try:
        out = subprocess.check_output(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"],
            stderr=subprocess.DEVNULL, text=True
        )
        ports = []
        for line in out.splitlines():
            if any(k in line for k in ["SunBrowse", "chrome", "Chromium", "chromium"]):
                parts = line.split()
                for p in parts:
                    if ":" in p:
                        port_str = p.split(":")[-1]
                        if port_str.isdigit():
                            ports.append(int(port_str))
        for port in sorted(set(ports)):
            try:
                r = requests.get(f"http://localhost:{port}/json/version", timeout=1)
                if r.ok:
                    ws = r.json().get("webSocketDebuggerUrl", "")
                    if ws:
                        print(f"  Browser encontrado na porta {port}")
                        return ws, str(port)
            except Exception:
                continue
    except Exception:
        pass

    print("Nenhum browser aberto encontrado.")
    print("Abra o perfil no AdsPower clicando em 'Abrir' e tente novamente.")
    return None


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


# ── TOTP via pyotp ────────────────────────────────────────────────────────────

def load_totp_secrets() -> dict:
    if os.path.exists(TOTP_SECRETS_FILE):
        with open(TOTP_SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_totp_secret(gmail: str, secret: str):
    secrets = load_totp_secrets()
    secrets[gmail.lower()] = secret.upper().replace(" ", "")
    with open(TOTP_SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(secrets, f, indent=2)
    print(f"  ✅ Segredo TOTP salvo para {gmail}")


ADSPOWER_AUTH_EXT_ID = "chcmmdbpbocmnmbhpbjchdgjjhbnfige"


def get_totp_from_adspower_popup(context, gmail: str) -> str | None:
    """Abre o popup do AdsPower Authenticator e lê o código TOTP para o gmail informado."""
    p = None
    for path in ["popup.html", "index.html", "popup/index.html", "dist/popup.html", "options.html"]:
        try:
            p = context.new_page()
            p.goto(f"chrome-extension://{ADSPOWER_AUTH_EXT_ID}/{path}", timeout=4000)
            p.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)

            # Lê todo o texto visível da página
            text = p.evaluate("document.body.innerText") or ""
            p.close()
            p = None

            if not text.strip():
                continue

            # Salva para debug na primeira vez
            debug_path = os.path.join(os.path.dirname(__file__), "totp_debug.json")
            if not os.path.exists(debug_path):
                with open(debug_path, "w") as df:
                    json.dump({"popup_path": path, "text": text}, df, indent=2)

            # Tenta associar o gmail ao código de 6 dígitos mais próximo
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            gmail_lower = gmail.lower()
            for i, line in enumerate(lines):
                if gmail_lower in line.lower() or line.lower() in gmail_lower:
                    # Procura um código de 6 dígitos nas próximas 5 linhas
                    for j in range(i + 1, min(i + 6, len(lines))):
                        m = re.search(r'\b(\d{6})\b', lines[j])
                        if m:
                            return m.group(1)

            # Fallback: retorna o primeiro código de 6 dígitos encontrado
            all_codes = re.findall(r'\b\d{6}\b', text)
            if all_codes:
                return all_codes[0]

        except Exception:
            if p:
                try:
                    p.close()
                except Exception:
                    pass
                p = None
            continue

    return None


def _parse_totp_secrets(raw: dict) -> dict:
    """Tenta extrair {gmail: base32_secret} de diferentes formatos de storage."""
    secrets = {}
    base32_re = re.compile(r"^[A-Z2-7]{16,}$")

    def _scan(obj):
        if isinstance(obj, dict):
            email = obj.get("email", obj.get("account", obj.get("issuer",
                    obj.get("username", obj.get("name", "")))))
            for key in ["secret", "totp", "key", "otp_secret", "totpSecret", "seed"]:
                candidate = obj.get(key, "")
                if candidate and base32_re.match(str(candidate).upper().replace(" ", "")):
                    if email and "@" in str(email):
                        secrets[str(email).lower()] = str(candidate).upper().replace(" ", "")
                    break
            for v in obj.values():
                _scan(v)
        elif isinstance(obj, list):
            for item in obj:
                _scan(item)

    # Varre local, sync e localStorage se vierem separados
    for section in [raw.get("local", raw), raw.get("sync", {}), raw.get("localStorage", {})]:
        _scan(section)

    return secrets


def generate_totp(gmail: str) -> str | None:
    try:
        import pyotp
        secret = load_totp_secrets().get(gmail.lower())
        if secret:
            return pyotp.TOTP(secret).now()
    except ImportError:
        print("  ⚠️  pyotp não instalado. Execute: pip install pyotp")
    return None


def import_totp_from_json(json_path: str):
    """Importa segredos TOTP de um arquivo JSON exportado pela extensão Authenticator."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    accounts = data if isinstance(data, list) else data.get("accounts", data)
    importados = 0
    for item in (accounts if isinstance(accounts, list) else accounts.values()):
        gmail  = item.get("account", item.get("email", item.get("name", "")))
        secret = item.get("secret", item.get("totp", ""))
        if gmail and secret:
            save_totp_secret(gmail, secret)
            importados += 1
    print(f"\n{importados} segredo(s) TOTP importado(s).")


# ── Fluxo OAuth no browser ────────────────────────────────────────────────────

def _do_oauth_in_browser(ws: str, gmail: str, account_id: str,
                         close_after: bool = True, profile_id: str = "",
                         force: bool = False) -> bool:
    global _auth_code, _auth_event

    tokens = load_tokens()
    key    = account_id or gmail

    if key in tokens and not force:
        print(f"  ⏭  {key} — já autenticado (use --force para re-autenticar)")
        return True

    _auth_code  = None
    _auth_event = threading.Event()

    server   = start_callback_server()
    auth_url = build_auth_url()

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            context = browser.contexts[0]
            page    = context.pages[0] if context.pages else context.new_page()

            print(f"    Navegando para OAuth...")
            page.goto(auth_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)

            # Seleciona conta se aparecer tela de escolha
            try:
                btns = page.locator("div[data-identifier]")
                if btns.count() > 0:
                    btns.first.click()
                    time.sleep(2)
            except Exception:
                pass

            # Loop principal: monitora cada etapa do fluxo OAuth (máx 3 min)
            # Ordem real: 2FA → app não verificado → Permitir → redirect
            print(f"    Aguardando conclusão do fluxo OAuth (máx 3 min)...")
            done = False
            _totp_preenchido = False
            deadline = time.time() + 180
            while time.time() < deadline and not done:
                try:
                    url = page.url

                    # Capturou o redirect — termina
                    if "localhost:8080" in url:
                        done = True
                        break

                    # Tela de 2FA TOTP → gera código via pyotp
                    if "signin/challenge/totp" in url and not _totp_preenchido:
                        # Tenta gerar via pyotp (segredo armazenado localmente)
                        code = generate_totp(gmail)

                        # Fallback: lê o código diretamente do popup da extensão
                        if not code:
                            code = get_totp_from_adspower_popup(context, gmail)
                        if code:
                            inp = page.locator(
                                "input[name='totpPin'], input[type='tel'], "
                                "#totpPin, input[autocomplete='one-time-code']"
                            )
                            if inp.count() > 0:
                                inp.first.fill(code)
                                time.sleep(0.5)
                                for txt in ["Avançar", "Next"]:
                                    btn = page.locator(f"button:has-text('{txt}')")
                                    if btn.count() > 0:
                                        btn.first.click()
                                        print(f"    ✅ 2FA preenchido automaticamente ({code})")
                                        _totp_preenchido = True
                                        time.sleep(2)
                                        break
                        else:
                            print(f"    ⏳ 2FA necessário — insira o código manualmente no browser")
                            print(f"    Dica: adicione o segredo com --add-totp --gmail={gmail}")
                            _totp_preenchido = True  # evita repetir a mensagem
                        time.sleep(2)
                        continue

                    # Tela de resumo de consentimento → clica Continuar
                    if "consentsummary" in url:
                        for text in ["Continuar", "Continue"]:
                            btn = page.locator(f"button:has-text('{text}')")
                            if btn.count() > 0:
                                btn.first.click()
                                print(f"    Clicou '{text}' (consent summary)")
                                time.sleep(2)
                                page.wait_for_load_state("domcontentloaded")
                                break
                        time.sleep(1)
                        continue

                    # Tela "app não verificado" → clica Avançado → não seguro
                    if "oauth/warning" in url or "oauth2/v2/auth/oauthchooseaccount" in url:
                        adv = page.locator("a:has-text('Avançado'), a:has-text('Advanced')")
                        if adv.count() > 0:
                            adv.first.click()
                            print("    Clicou 'Avançado'")
                            time.sleep(1)
                        unsafe = page.locator("#proceed-link, a:has-text('não seguro'), a:has-text('unsafe')")
                        if unsafe.count() > 0:
                            unsafe.first.click()
                            print("    Clicou 'Ir para app (não seguro)'")
                            time.sleep(2)
                            page.wait_for_load_state("domcontentloaded")
                        time.sleep(1)
                        continue

                    # Tela de consentimento → clica Permitir
                    for text in ["Permitir", "Allow"]:
                        btn = page.locator(f"button:has-text('{text}')")
                        if btn.count() > 0:
                            btn.first.click()
                            print(f"    Clicou '{text}'")
                            time.sleep(2)
                            done = True
                            break

                except Exception:
                    pass
                time.sleep(2)

            # Captura redirect com auth code via Playwright
            try:
                page.wait_for_url("*localhost:8080*", timeout=30000)
                parsed = urllib.parse.urlparse(page.url)
                code   = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
                if code:
                    _auth_code = code
                    _auth_event.set()
            except Exception:
                _auth_event.wait(timeout=15)

            if close_after:
                browser.close()
    except Exception as e:
        print(f"    ❌ Erro no browser: {e}")
        if close_after and profile_id:
            close_profile(profile_id)
        server.shutdown()
        return False

    if close_after and profile_id:
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
        print(f"    ❌ refresh_token ausente: {token_data}")
        return False

    tokens[key] = {
        "refresh_token": token_data["refresh_token"],
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "gmail":         gmail,
    }
    save_tokens(tokens)
    print(f"    ✅ Token salvo para {key}")
    return True


def auth_profile(profile: dict, account_id: str | None = None, force: bool = False) -> bool:
    profile_id = profile["user_id"]
    gmail      = profile.get("username", profile_id)
    label      = account_id or gmail

    print(f"\n  Autenticando: {label}  (perfil={profile_id})")
    try:
        ws = open_profile(profile_id)
    except Exception as e:
        print(f"    ❌ Não foi possível abrir o perfil: {e}")
        return False

    return _do_oauth_in_browser(ws, gmail, label,
                                close_after=True, profile_id=profile_id, force=force)


# ── Fila de autenticação ──────────────────────────────────────────────────────

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")


def _load_queue() -> list[tuple[str, str]]:
    """Retorna lista de (account_id, nome) das contas ✅ ainda sem token."""
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"Arquivo não encontrado: {ACCOUNTS_FILE}")
        return []

    tokens = load_tokens()
    queue  = []
    seen   = set()

    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("✅"):
                continue
            line = line[1:].strip()
            parts = line.split("|", 1)
            acc_id = parts[0].strip()
            nome   = parts[1].strip() if len(parts) > 1 else acc_id
            if acc_id in seen or acc_id in tokens:
                continue
            seen.add(acc_id)
            queue.append((acc_id, nome))

    return queue


def _run_queue(force: bool = False):
    queue  = _load_queue()
    total  = len(queue)

    if not total:
        tokens = load_tokens()
        print(f"Todas as contas ✅ já estão autenticadas ({len(tokens)} tokens).")
        return

    print(f"\n{'─'*50}")
    print(f"  Fila: {total} conta(s) pendente(s)")
    print(f"  Para cada conta:")
    print(f"    1. Abra o perfil no AdsPower")
    print(f"    2. Pressione Enter aqui")
    print(f"    3. O script faz o OAuth automaticamente")
    print(f"{'─'*50}\n")

    ok = err = skip = 0
    for idx, (acc_id, nome) in enumerate(queue, 1):
        print(f"[{idx}/{total}] {acc_id} | {nome}")
        resp = input("  Abriu o perfil no AdsPower? (Enter=continuar / s=pular / q=sair): ").strip().lower()

        if resp == "q":
            print("Interrompido pelo usuário.")
            break
        if resp == "s":
            skip += 1
            continue

        result = get_active_browser_ws()
        if not result:
            print("  ❌ Nenhum browser aberto. Abra o perfil e tente novamente.\n")
            err += 1
            continue

        ws, uid = result
        success = _do_oauth_in_browser(ws, uid, acc_id, close_after=False, force=force)
        if success:
            ok += 1
        else:
            err += 1
        print()

    print(f"\n{'─'*50}")
    print(f"  Concluído: {ok} autenticados | {skip} pulados | {err} erros")
    print(f"  Tokens salvos: {len(load_tokens())}")
    print(f"{'─'*50}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open",        action="store_true",
                        help="Usa o perfil já aberto no AdsPower (abra manualmente antes)")
    parser.add_argument("--profile",     help="ID interno do perfil no AdsPower (ex: k1d6wcp4)")
    parser.add_argument("--serial",      help="Número serial visível no AdsPower (ex: 1922)")
    parser.add_argument("--name",        help="Nome ou parte do nome do perfil (ex: LC141)")
    parser.add_argument("--gmail",       help="Gmail da conta")
    parser.add_argument("--account",     help="ID Google Ads para usar como chave (ex: 123-456-7890)")
    parser.add_argument("--all",         action="store_true", help="Autentica todos os perfis")
    parser.add_argument("--force",       action="store_true", help="Re-autentica mesmo se já existir")
    parser.add_argument("--add-totp",    action="store_true",
                        help="Salva segredo TOTP de uma conta (requer --gmail)")
    parser.add_argument("--import-totp", help="Importa segredos TOTP de arquivo JSON exportado pela extensão Authenticator")
    parser.add_argument("--queue",       action="store_true",
                        help="Processa fila de contas ✅ do accounts_to_invite.txt")
    args = parser.parse_args()

    # Gerenciamento de segredos TOTP (não abre browser)
    if args.add_totp:
        if not args.gmail:
            print("Use --gmail=conta@gmail.com junto com --add-totp")
            return
        print(f"Cole o segredo base32 do 2FA para {args.gmail}")
        print("(encontre em: extensão Authenticator → ⚙️ Configurações → Exportar)")
        secret = input("Segredo base32: ").strip()
        if secret:
            save_totp_secret(args.gmail, secret)
        return

    if args.import_totp:
        import_totp_from_json(args.import_totp)
        return

    if args.queue:
        _run_queue(force=args.force)
        return

    if args.open:
        result = get_active_browser_ws()
        if not result:
            print("Nenhum browser aberto encontrado no AdsPower.")
            print("Abra o perfil manualmente no AdsPower e tente novamente.")
            return
        ws, uid = result
        print(f"Browser ativo encontrado (user_id={uid})")
        account_id = args.account
        if not account_id:
            account_id = input("ID da conta Google Ads (ex: 123-456-7890): ").strip()
        _do_oauth_in_browser(ws, uid, account_id, close_after=False, force=args.force)
        return

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
        # Vai direto — não precisa estar na listagem da API (pode ser outro workspace)
        profile = {"user_id": args.profile, "username": args.gmail or args.profile}
        auth_profile(profile, account_id=args.account, force=args.force)
        return

    if args.serial or args.name:
        profiles = list_profiles()
        profile = None
        if args.serial:
            profile = next((p for p in profiles
                            if str(p.get("serial_number", "")) == str(args.serial)), None)
            if not profile:
                print(f"Serial {args.serial} não encontrado. Tentando abrir direto...")
                # AdsPower às vezes aceita o serial como user_id em versões mais antigas
                profile = {"user_id": args.serial, "username": args.serial}
        if args.name and not profile:
            needle = args.name.lower()
            profile = next((p for p in profiles
                            if needle in p.get("name", "").lower()), None)
        if not profile:
            print(f"Perfil não encontrado com os critérios fornecidos.")
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

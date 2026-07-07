"""
Agente autônomo para criação de Gmail + Google Ads
Usa Claude API como cérebro para lidar com telas inesperadas
"""
import os
import re
import sys
import time
import base64
import requests
import anthropic
import pyotp
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright, Page

load_dotenv()

SPREADSHEET_ID = "1qIlVRWUXTZcYd0QkfAawensrgsWTiBgj1xEfeWEbpLY"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
ADSPOWER_URL = "http://local.adspower.net:50325"
GROUP_NAME = "IA"
MAX_ACCOUNTS_PER_PROXY = 3

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
ONLINESIM_KEY = os.getenv("ONLINESIM_API_KEY")
SLACK_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL_ID")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

_BASE_DIR       = os.path.dirname(__file__)
_SCREENSHOT_DIR = os.path.join(_BASE_DIR, "screenshots")

# ── Logging em arquivo ────────────────────────────────────────────────────────

class _Tee:
    """Espelha stdout no terminal e em um arquivo de log."""
    def __init__(self, terminal, log_fh):
        self.terminal = terminal
        self.log_fh   = log_fh
        self._bol     = True   # beginning-of-line

    def write(self, msg):
        if not msg:
            return
        # Adiciona timestamp apenas no início de cada linha
        if self._bol and msg != "\n":
            ts = time.strftime("%H:%M:%S")
            out = f"[{ts}] {msg}"
        else:
            out = msg
        self._bol = msg.endswith("\n")
        self.terminal.write(out)
        self.terminal.flush()
        self.log_fh.write(out)
        self.log_fh.flush()

    def flush(self):
        self.terminal.flush()
        self.log_fh.flush()

    def isatty(self):
        return False


def _init_logging():
    """Redireciona stdout para terminal + arquivo diário de log."""
    os.makedirs(_BASE_DIR, exist_ok=True)
    ts = time.strftime("%Y-%m-%d")
    log_path = os.path.join(_BASE_DIR, f"gmail_agent_{ts}.log")
    log_fh = open(log_path, "a", encoding="utf-8")
    sep = "=" * 60
    log_fh.write(f"\n{sep}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sessão iniciada\n{sep}\n")
    log_fh.flush()
    sys.stdout = _Tee(sys.__stdout__, log_fh)
    return log_fh


# ── Screenshots ───────────────────────────────────────────────────────────────

def save_screenshot(page: Page, account_name: str, label: str = "erro") -> str | None:
    """Salva screenshot para diagnóstico. Retorna o caminho do arquivo."""
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        ts   = time.strftime("%Y%m%d_%H%M%S")
        name = re.sub(r"[^\w]", "_", account_name)[:25]
        path = os.path.join(_SCREENSHOT_DIR, f"{ts}_{name}_{label}.png")
        page.screenshot(path=path, full_page=True)
        print(f"  [screenshot] {path}")
        return path
    except Exception as e:
        print(f"  [screenshot] falhou: {e}")
        return None


# ── Slack ─────────────────────────────────────────────────────────────────────

def notify_slack(msg: str):
    """Envia mensagem no canal Slack configurado no .env."""
    if not SLACK_TOKEN or not SLACK_CHANNEL:
        return
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"  [slack] falhou: {e}")

# Column indexes (0-based)
COL_NAME      = 1
COL_GMAIL     = 2  # será preenchido pelo agente após criação
COL_PASS      = 3  # será preenchido pelo agente após criação
COL_CNPJ      = 4
COL_RAZAO     = 5
COL_CARD_NAME = 6
COL_CARD_NUM  = 7
COL_CARD_EXP  = 8
COL_CARD_CVV  = 9
COL_STATUS    = 10
COL_OBS       = 11
COL_RECOVERY  = 12  # email de recuperação para challenge/selection
COL_TOTP      = 13  # segredo TOTP do autenticador (coluna N)


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheets():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    return build("sheets", "v4", credentials=creds).spreadsheets()


def read_accounts(sheets) -> list:
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID, range="Contas!A2:N200"
    ).execute()
    accounts = []
    for i, row in enumerate(result.get("values", [])):
        row = row + [""] * (14 - len(row))
        cnpj = re.sub(r"\D", "", row[COL_CNPJ].strip())
        if not cnpj:
            continue
        if row[COL_STATUS].strip() in ("Criado", "Erro - ignorar"):
            continue
        accounts.append({
            "row_index": i + 2,
            "name": row[COL_NAME].strip(),
            "gmail": row[COL_GMAIL].strip(),
            "password": row[COL_PASS].strip(),
            "cnpj": cnpj,
            "card_name": row[COL_CARD_NAME].strip(),
            "card_number": re.sub(r"\s", "", row[COL_CARD_NUM].strip()),
            "card_exp": row[COL_CARD_EXP].strip(),
            "card_cvv": row[COL_CARD_CVV].strip().zfill(3),  # zero-pad ex: 75 → 075
            "recovery_email": row[COL_RECOVERY].strip(),
            "totp_secret": row[COL_TOTP].strip(),
        })
    return accounts


def update_status(sheets, row_index, status, obs=""):
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!K{row_index}:L{row_index}",
        valueInputOption="RAW",
        body={"values": [[status, obs]]}
    ).execute()


def update_totp_secret(sheets, row_index, secret):
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!N{row_index}",
        valueInputOption="RAW",
        body={"values": [[secret]]}
    ).execute()


def update_gmail_created(sheets, row_index, gmail, password, razao):
    # C=GMAIL, D=SENHA (não tocar E=CNPJ), F=RAZÃO SOCIAL
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!C{row_index}:D{row_index}",
        valueInputOption="RAW",
        body={"values": [[gmail, password]]}
    ).execute()
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!F{row_index}",
        valueInputOption="RAW",
        body={"values": [[razao]]}
    ).execute()


# ── OnlineSIM ─────────────────────────────────────────────────────────────────

def sms_get_number(country: int = 55, service: str = "google") -> dict:
    """Solicita número para verificação. Retorna {tzid, number}."""
    r = requests.get(
        "https://onlinesim.io/api/getNum.php",
        params={"apikey": ONLINESIM_KEY, "service": service, "country": country},
        timeout=15
    )
    data = r.json()
    if data.get("response") != 1:
        raise Exception(f"OnlineSIM getNum erro: {data}")

    # Buscar o número via getState
    tzid = data["tzid"]
    time.sleep(2)
    state = sms_get_state(tzid)
    number = state[0].get("number", "")
    print(f"  SMS: número obtido {number} (tzid={tzid})")
    return {"tzid": tzid, "number": number}


def sms_get_state(tzid: int) -> list:
    r = requests.get(
        "https://onlinesim.io/api/getState.php",
        params={"apikey": ONLINESIM_KEY, "tzid": tzid},
        timeout=15
    )
    return r.json()


def sms_wait_code(tzid: int, timeout: int = 120) -> str:
    """Aguarda o código SMS chegar. Retorna o código."""
    print(f"  SMS: aguardando código (tzid={tzid})...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = sms_get_state(tzid)
        if isinstance(state, list) and state:
            s = state[0]
            response = s.get("response", "")
            if response == "TZ_NUM_ANSWER":
                msg = s.get("msg", "")
                # Extrai código numérico da mensagem
                code = re.search(r"\b\d{6}\b", msg)
                if code:
                    print(f"  SMS: código recebido → {code.group()}")
                    return code.group()
        time.sleep(5)
    raise Exception("Timeout aguardando código SMS")


def sms_finish(tzid: int):
    requests.get(
        "https://onlinesim.io/api/setOperationOk.php",
        params={"apikey": ONLINESIM_KEY, "tzid": tzid},
        timeout=10
    )


def sms_cancel(tzid: int):
    requests.get(
        "https://onlinesim.io/api/setOperationRevise.php",
        params={"apikey": ONLINESIM_KEY, "tzid": tzid},
        timeout=10
    )


# ── Detector de telas inesperadas ────────────────────────────────────────────

def _detect_interruption(page: Page) -> str | None:
    """
    Detecta telas inesperadas do Google que podem travar o fluxo.
    Retorna uma string identificando o tipo ou None se tudo normal.
    """
    try:
        url  = page.url.lower()
        text = ""
        try:
            text = page.inner_text("body", timeout=3000).lower()
        except:
            pass

        # Ad blocker detectado
        if "ad blocker" in text or "bloqueador de anúncios" in text:
            return "ad_blocker"

        # Desafio de identidade / reautenticação
        if any(kw in text for kw in [
            "verificar que é você", "verify it's you",
            "confirmar sua identidade", "confirm your identity",
            "digitar sua senha", "enter your password",
        ]):
            return "identity_challenge"

        # Google pedindo número de recuperação no meio do fluxo
        if any(kw in text for kw in [
            "adicionar número de telefone de recuperação",
            "add a recovery phone", "número de recuperação",
        ]):
            return "recovery_phone_prompt"

        # Termos de serviço inesperados
        if any(kw in text for kw in [
            "termos de serviço", "terms of service",
        ]) and any(kw in text for kw in ["concordo", "i agree", "aceito"]):
            return "terms"

        # Alerta de bloqueio de conta
        if any(kw in text for kw in [
            "sua conta foi suspensa", "account suspended",
            "conta desativada", "account disabled",
        ]):
            return "account_suspended"

        # Prompt de 2FA não esperado
        if "two-step-verification" in url and "enroll" not in url:
            return "2fa_prompt"

        return None
    except:
        return None


def _handle_interruption(page: Page, interruption: str, account_name: str = "") -> bool:
    """
    Tenta resolver automaticamente uma interrupção detectada.
    Retorna True se resolveu, False se precisa de intervenção manual.
    """
    print(f"  [interruption] Detectado: {interruption}")

    if interruption == "ad_blocker":
        # Tentar fechar o aviso
        try:
            page.locator("button:has-text('Fechar'), button:has-text('Close'), button:has-text('OK')").first.click(timeout=3000)
        except:
            pass
        return True

    if interruption == "terms":
        # Aceitar termos automaticamente
        for label in ["Concordo", "I agree", "Aceitar", "Accept"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=2000)
                time.sleep(1)
                return True
            except:
                pass
        return False

    if interruption == "recovery_phone_prompt":
        # Pular pedido de telefone de recuperação
        for label in ["Agora não", "Not now", "Pular", "Skip", "Confirmar"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=2000)
                time.sleep(1)
                return True
            except:
                pass
        return False

    if interruption == "2fa_prompt":
        # Pular configuração de 2FA não solicitada
        for label in ["Agora não", "Not now", "Pular", "Skip"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=2000)
                time.sleep(1)
                return True
            except:
                pass
        return False

    # identity_challenge e account_suspended precisam de intervenção manual
    return False


# ── BrasilAPI ─────────────────────────────────────────────────────────────────

def lookup_cnpj(cnpj: str) -> dict:
    r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", timeout=15)
    r.raise_for_status()
    d = r.json()
    return {
        "razao_social": d.get("razao_social", ""),
        "cep": re.sub(r"\D", "", d.get("cep", "")),
        "logradouro": d.get("logradouro", ""),
        "numero": d.get("numero", ""),
        "municipio": d.get("municipio", ""),
        "uf": d.get("uf", ""),
    }


# ── AdsPower ──────────────────────────────────────────────────────────────────

def build_proxy_pool() -> list:
    proxy_map = {}
    proxy_usage = {}
    for page in range(1, 30):
        r = requests.get(f"{ADSPOWER_URL}/api/v1/user/list?page={page}&page_size=100")
        d = r.json()
        if d.get("code") != 0:
            break
        users = d["data"].get("list", [])
        if not users:
            break
        for u in users:
            pid = u.get("fbcc_proxy_acc_id", "0")
            if pid and pid != "0":
                cfg = u.get("user_proxy_config", {})
                if cfg.get("proxy_host"):
                    if pid not in proxy_map:
                        proxy_map[pid] = {
                            "proxy_soft": cfg.get("proxy_soft", "other"),
                            "proxy_type": cfg.get("proxy_type", "socks5"),
                            "proxy_host": cfg["proxy_host"],
                            "proxy_port": cfg["proxy_port"],
                            "proxy_user": cfg.get("proxy_user", ""),
                            "proxy_password": cfg.get("proxy_password", ""),
                        }
                    proxy_usage[pid] = proxy_usage.get(pid, 0) + 1
    available = [
        (pid, proxy_map[pid], proxy_usage.get(pid, 0))
        for pid in proxy_map
        if proxy_usage.get(pid, 0) < MAX_ACCOUNTS_PER_PROXY
    ]
    available.sort(key=lambda x: (x[2], -int(x[0])))
    print(f"  Proxies: {len(proxy_map)} encontrados, {len(available)} disponíveis")
    return available


def get_or_create_group() -> str:
    r = requests.get(f"{ADSPOWER_URL}/api/v1/group/list", params={"page": 1, "page_size": 100})
    for g in r.json()["data"].get("list", []):
        if g["group_name"] == GROUP_NAME:
            return str(g["group_id"])
    r2 = requests.post(f"{ADSPOWER_URL}/api/v1/group/create", json={"group_name": GROUP_NAME})
    return str(r2.json()["data"]["group_id"])


def find_existing_profile_by_name(name: str) -> dict | None:
    """Retorna o perfil completo se já existe um com esse nome no grupo IA."""
    for page in range(1, 10):
        r = requests.get(f"{ADSPOWER_URL}/api/v1/user/list?page={page}&page_size=100")
        d = r.json()
        if d.get("code") != 0:
            break
        for u in d["data"].get("list", []):
            if u.get("name", "").strip() == name.strip() and u.get("group_name") == GROUP_NAME:
                return u
    return None


def _scan_all_profiles() -> list:
    all_users = []
    for page in range(1, 20):
        for attempt in range(3):
            try:
                r = requests.get(f"{ADSPOWER_URL}/api/v1/user/list?page={page}&page_size=100", timeout=10)
                d = r.json()
                break
            except Exception as e:
                if attempt == 2:
                    return all_users
                time.sleep(1)
        if d.get("code") != 0:
            break
        users = d["data"].get("list", [])
        if not users:
            break
        all_users.extend(users)
    return all_users


def find_existing_profile(gmail: str, name: str = None) -> str | None:
    """Retorna o profile_id mais antigo com esse nome no grupo IA (ou Gmail como fallback)."""
    all_users = _scan_all_profiles()
    print(f"    [scan: {len(all_users)} perfis]", end=" ")
    found = []
    # Primário: busca por nome no grupo IA
    if name:
        for u in all_users:
            if u.get("name", "").strip() == name.strip() and u.get("group_name") == GROUP_NAME:
                found.append(u)
    # Fallback: busca por username/Gmail
    if not found:
        for u in all_users:
            if u.get("username", "").lower() == gmail.lower():
                found.append(u)
    if not found:
        return None
    found.sort(key=lambda u: int(u.get("serial_number", 9999)))
    return found[0]["user_id"]


def create_profile(name: str, gmail: str, password: str, group_id: str, proxy_config: dict) -> str:
    existing = find_existing_profile(gmail, name)
    if existing:
        print(f"  Perfil já existe: {existing}")
        return existing
    payload = {
        "name": name,
        "group_id": group_id,
        "domain_name": "accounts.google.com",
        "username": gmail,
        "password": password,
        "user_proxy_config": proxy_config,
        "fingerprint_config": {"os": "Windows", "language": ["pt-BR", "pt", "en-US", "en"]},
    }
    r = requests.post(f"{ADSPOWER_URL}/api/v1/user/create", json=payload)
    d = r.json()
    if d.get("code") == 0:
        print(f"  Perfil criado: {d['data']['id']}")
        return d["data"]["id"]
    raise Exception(f"Falha ao criar perfil: {d}")


def open_profile(profile_id: str) -> str:
    r = requests.get(f"{ADSPOWER_URL}/api/v1/browser/start", params={"user_id": profile_id})
    d = r.json()
    if d.get("code") == 0:
        ws = d["data"]["ws"]["puppeteer"]
        print(f"  Browser aberto (port={d['data']['debug_port']})")
        return ws
    raise Exception(f"Falha ao abrir browser: {d}")


def close_profile(profile_id: str):
    requests.get(f"{ADSPOWER_URL}/api/v1/browser/stop", params={"user_id": profile_id})


# ── Claude Vision Agent ───────────────────────────────────────────────────────

def screenshot_b64(page: Page) -> str:
    img = page.screenshot()
    return base64.standard_b64encode(img).decode()


def ask_claude_coords(page: Page, element_description: str) -> tuple | None:
    """Pergunta ao Claude as coordenadas de um elemento visível na tela."""
    import json as _json
    img_b64 = screenshot_b64(page)
    prompt = (
        f"Analise a screenshot e retorne as coordenadas do centro do elemento: {element_description}\n"
        f"Responda APENAS em JSON: {{\"x\": NUMBER, \"y\": NUMBER}}\n"
        f"Se não encontrar, retorne: {{\"x\": null, \"y\": null}}"
    )
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    raw = response.content[0].text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            data = _json.loads(match.group())
            x, y = data.get("x"), data.get("y")
            if x is not None and y is not None:
                return (int(x), int(y))
        except:
            pass
    return None


def ask_claude(page: Page, instruction: str, context: str = "") -> str:
    """Tira screenshot e pergunta ao Claude o que fazer."""
    img_b64 = screenshot_b64(page)
    prompt = f"""Você é um agente de automação controlando um browser.
URL atual: {page.url}
{f'Contexto: {context}' if context else ''}

Tarefa: {instruction}

Analise a tela e responda em JSON com uma das ações:
- {{"action": "click", "selector": "CSS_SELECTOR_OU_TEXTO"}}
- {{"action": "fill", "selector": "CSS_SELECTOR", "value": "VALOR"}}
- {{"action": "wait", "seconds": N}}
- {{"action": "done", "message": "DESCRICAO"}}
- {{"action": "error", "message": "DESCRICAO_DO_PROBLEMA"}}

Responda APENAS o JSON, sem explicações."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    return response.content[0].text.strip()


def agent_execute(page: Page, instruction: str, context: str = "", max_steps: int = 10) -> bool:
    """Executa uma instrução usando o agente Claude com visão."""
    print(f"  Agente: {instruction[:60]}...")
    for step in range(max_steps):
        try:
            raw = ask_claude(page, instruction, context)
            # Extrai JSON da resposta
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                print(f"  Agente resposta inválida: {raw[:100]}")
                time.sleep(2)
                continue

            action = __import__('json').loads(match.group())
            act = action.get("action")
            print(f"  [step {step}] {act}: {str(action.get('selector', action.get('message', '')))[:60]}")

            if act == "done":
                print(f"  ✓ {action.get('message', 'Concluído')}")
                return True

            elif act == "error":
                print(f"  ✗ Agente: {action.get('message')}")
                return False

            elif act == "click":
                sel = action["selector"]
                clicked = False
                try:
                    page.click(sel, timeout=3000)
                    clicked = True
                except:
                    pass
                if not clicked:
                    try:
                        page.click(f"text={sel}", timeout=3000)
                        clicked = True
                    except:
                        pass
                if not clicked:
                    # Tentar em iframes
                    for _fr in page.frames:
                        if not _fr.url:
                            continue
                        try:
                            _fr.click(sel, timeout=2000)
                            clicked = True
                            break
                        except:
                            pass
                        try:
                            _fr.get_by_text(sel, exact=False).first.click(timeout=2000)
                            clicked = True
                            break
                        except:
                            pass
                time.sleep(1.5)

            elif act == "fill":
                filled = False
                try:
                    page.fill(action["selector"], action["value"], timeout=3000)
                    filled = True
                except:
                    pass
                if not filled:
                    # Tentar em iframes
                    for _fr in page.frames:
                        if not _fr.url:
                            continue
                        try:
                            _fr.fill(action["selector"], action["value"], timeout=2000)
                            filled = True
                            break
                        except:
                            pass
                        # Tentar por placeholder
                        try:
                            _fr.get_by_placeholder(action["selector"], exact=False).first.fill(action["value"], timeout=2000)
                            filled = True
                            break
                        except:
                            pass
                time.sleep(1)

            elif act == "wait":
                time.sleep(action.get("seconds", 2))

        except Exception as e:
            print(f"  Agente step {step} erro: {e}")
            time.sleep(2)

    return False


# ── Gmail Creation ────────────────────────────────────────────────────────────

GMAIL_DEFAULT_PASSWORD = "Fegdigital@$"

def generate_gmail_credentials(account_name: str) -> tuple[str, str]:
    """Gera username para o novo Gmail com senha padrão da empresa."""
    base = re.sub(r"[^a-z0-9]", "", account_name.lower().replace(" ", ""))
    import random, string
    suffix = ''.join(random.choices(string.digits, k=6))
    username = f"{base}{suffix}"
    return f"{username}@gmail.com", GMAIL_DEFAULT_PASSWORD


def detect_gmail_step(page: Page) -> str:
    """Detecta em qual etapa do fluxo de criação do Gmail estamos."""
    import json
    img_b64 = screenshot_b64(page)
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": (
                    "Analise esta tela de criação de conta Google. Responda APENAS com uma dessas palavras:\n"
                    "- 'name' se mostrar campos de Nome e Sobrenome\n"
                    "- 'birthdate' se mostrar campos de data de nascimento e/ou gênero\n"
                    "- 'username' se mostrar campo de endereço de email/username do Gmail\n"
                    "- 'password' se mostrar campos de senha\n"
                    "- 'phone' se mostrar campo de número de telefone\n"
                    "- 'code' se mostrar campo para inserir código de verificação\n"
                    "- 'skip' se mostrar tela para adicionar número de recuperação ou informações opcionais\n"
                    "- 'done' se a conta foi criada (está no Gmail ou Google Account)\n"
                    "- 'other' para qualquer outra tela\n"
                    "Responda APENAS a palavra."
                )}
            ]
        }]
    )
    step = response.content[0].text.strip().lower()
    print(f"  Etapa detectada: {step}")
    return step


def is_gmail_logged_in(page: Page) -> bool:
    """Verifica se já está logado no Gmail navegando para gmail.com."""
    try:
        page.goto("https://mail.google.com/mail/u/0/", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=10000)
    except:
        pass
    url = page.url
    return "accounts.google.com" not in url and "mail.google.com" in url


def login_gmail(page: Page, gmail: str, password: str, recovery_email: str = "") -> bool:
    """Faz login em uma conta Gmail existente. Retorna True se logado com sucesso."""
    print(f"  Fazendo login: {gmail}")
    try:
        page.goto("https://accounts.google.com/signin/v2/identifier?hl=pt-BR", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=10000)
    except:
        pass
    time.sleep(2)

    email_done = False
    password_done = False
    last_url = ""

    for iteration in range(25):
        url = page.url
        print(f"  [login-iter={iteration}] url={url}")

        # Se URL não mudou e email já foi preenchido mas ainda está no identifier, resetar
        if url == last_url and email_done and "identifier" in url:
            email_done = False
            time.sleep(2)
        last_url = url

        # Já logado
        if "mail.google.com" in url or (
            "myaccount.google.com" in url and "interstitials" not in url
        ):
            print(f"  ✓ Login concluído")
            return True

        # interstitials/birthday: Google pedindo data de nascimento após login — pular
        if "interstitials/birthday" in url:
            print(f"  [login] interstitials/birthday — tentando pular...")
            skipped = page.evaluate("""() => {
                const kw = ["pular", "skip", "não agora", "not now", "later", "lembrar", "dispensar", "concluído", "done", "continuar", "continue", "confirmar"];
                const btns = Array.from(document.querySelectorAll('button,[role="button"],a'));
                for (const k of kw) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                return null;
            }""")
            if skipped:
                print(f"  [login] Clicou '{skipped}' ✓")
                time.sleep(3)
            else:
                # Navegar diretamente para o destino após login
                page.goto("https://mail.google.com/mail/u/0/", timeout=15000)
                time.sleep(3)
            continue

        # gds.google.com: página de configuração de recuperação — pular
        if "gds.google.com" in url:
            print(f"  [login] gds.google.com — tentando pular configuração...")
            # Tentar clicar em "Pular", "Skip", "Não agora", "Lembrar mais tarde"
            skipped = page.evaluate("""() => {
                const kw = ["pular", "skip", "não agora", "not now", "later", "lembrar", "dispensar", "done", "concluir"];
                const btns = Array.from(document.querySelectorAll('button,[role="button"],a'));
                for (const k of kw) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                return null;
            }""")
            if skipped:
                print(f"  [login] Clicou '{skipped}' ✓")
                time.sleep(3)
            else:
                # Verificar se já estamos logados navegando para mail
                print(f"  [login] Verificando login via mail.google.com...")
                page.goto("https://mail.google.com/mail/u/0/", timeout=15000)
                time.sleep(3)
            continue

        # challenge/selection: página de escolha de método de verificação
        if "challenge/selection" in url:
            if recovery_email:
                print(f"  [login] challenge/selection — selecionando email de recuperação...")
                # Primeiro: verificar se já há campo de email visível (sub-página de digitação)
                email_field_found = False
                for sel in ["input[type='email']", "input[name='knowledgePreregisteredEmailResponse']"]:
                    try:
                        field = page.locator(sel).first
                        if field.is_visible(timeout=1500):
                            field.click()
                            time.sleep(0.2)
                            field.fill(recovery_email, timeout=3000)
                            page.evaluate("""() => {
                                const inputs = document.querySelectorAll('input[type="email"],input[name="knowledgePreregisteredEmailResponse"]');
                                inputs.forEach(el => {
                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                });
                            }""")
                            time.sleep(0.5)
                            clicked = page.evaluate("""() => {
                                const kw = ["avançar", "next", "continuar", "continue", "confirmar", "confirm"];
                                const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                                for (const k of kw) {
                                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                                    if (b) { b.click(); return b.textContent.trim(); }
                                }
                                return null;
                            }""")
                            if clicked:
                                print(f"  [login] Clicou '{clicked.strip()}' ✓")
                            else:
                                page.keyboard.press("Enter")
                            time.sleep(3)
                            email_field_found = True
                            break
                    except:
                        pass

                if not email_field_found:
                    # Clicar na opção de email de recuperação via coordenadas visuais
                    coords = ask_claude_coords(page,
                        "opção para usar email de recuperação, email alternativo, ou 'Confirmar email de recuperação' — clique nessa opção")
                    if coords:
                        page.mouse.click(coords[0], coords[1])
                        print(f"  [login] Clicou opção email de recuperação ✓")
                        time.sleep(3)
                    else:
                        # Fallback: tentar clicar em qualquer opção de email via JS
                        page.evaluate("""() => {
                            const kw = ["recupera", "recovery", "email alternativo", "alternate"];
                            const all = Array.from(document.querySelectorAll('[role="link"],[role="button"],a,li,div[tabindex]'));
                            for (const k of kw) {
                                const el = all.find(e => (e.textContent||'').toLowerCase().includes(k));
                                if (el) { el.click(); return; }
                            }
                        }""")
                        time.sleep(3)
                continue
            else:
                print(f"  ⚠ Google requer email de recuperação — adicione na coluna M da planilha")
                return "manual"

        # Desafio de 2FA/verificação que não conseguimos resolver automaticamente
        _2fa_patterns = ["challenge/totp", "challenge/az", "challenge/ipp", "challenge/sk",
                         "challenge/recaptcha", "2sv", "twosv",
                         "reauth/type/TOTP", "reauth/type/PHONE"]
        _matched = [p for p in _2fa_patterns if p in url]
        if _matched:
            print(f"  ⚠ Desafio de verificação (2FA) — padrão: {_matched}")
            return "manual"

        # /challenge/pwd = página de senha (pode acontecer múltiplas vezes com cids diferentes)
        if "/challenge/pwd" in url:
            # cid=1: senha do Gmail | cid=7: confirmação do email de recuperação (digitar o endereço)
            import re as _re2
            cid_match = _re2.search(r'[?&]cid=(\d+)', url)
            cid = int(cid_match.group(1)) if cid_match else 1
            # cid=7 pede para digitar o endereço do email de recuperação
            pwd_to_use = password if cid == 1 else (recovery_email if recovery_email else password)
            # Tentar campo de email (cid=7) ou senha (cid=1)
            sels_to_try = (
                ["input[type='email']", "input[name='knowledgePreregisteredEmailResponse']",
                 "input[type='text']", "input[type='password']"]
                if cid != 1 else
                ["input[type='password']", "input[name='password']", "input[name='Passwd']"]
            )
            for sel in sels_to_try:
                try:
                    field = page.locator(sel).first
                    if field.is_visible(timeout=2000):
                        field.click(click_count=3)
                        time.sleep(0.2)
                        field.fill(pwd_to_use, timeout=3000)
                        time.sleep(0.5)
                        page.keyboard.press("Enter")
                        time.sleep(4)
                        password_done = True
                        print(f"  [login] Senha (challenge/pwd) preenchida ✓")
                        break
                except Exception as e:
                    print(f"  [login] erro senha challenge/pwd: {e}")
            continue

        def _fill_and_advance(selector, value):
            """Preenche campo via setter nativo React + dispara eventos JS + clica Avançar."""
            field = page.locator(selector).first
            if not field.is_visible(timeout=2000):
                return False
            field.click()
            time.sleep(0.2)
            # Usar setter nativo do React/Angular — mais confiável que field.fill()
            # e ignora autocomplete do browser
            filled = page.evaluate("""([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.setAttribute('autocomplete', 'off');
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                return el.value === val;
            }""", [selector, value])
            if not filled:
                # Fallback: Playwright fill padrão
                field.fill(value, timeout=3000)
            time.sleep(0.5)
            # Clicar botão Avançar via JS
            clicked = page.evaluate("""() => {
                const keywords = ["avançar", "next", "continuar", "continue", "próxima"];
                const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                for (const k of keywords) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                return null;
            }""")
            if clicked:
                print(f"  [login] Clicou '{clicked.strip()}' ✓")
            else:
                page.keyboard.press("Enter")
                print(f"  [login] Enter ✓")
            time.sleep(3)
            return True

        # Etapa 1: campo de email (identifier)
        if not email_done:
            for sel in ["input[type='email']", "input[name='identifier']"]:
                try:
                    if _fill_and_advance(sel, gmail):
                        email_done = True
                        print(f"  [login] Email preenchido ✓")
                        break
                except Exception as e:
                    print(f"  [login] erro email sel={sel}: {e}")
            if email_done:
                continue

        # Etapa 2: campo de senha
        if email_done and not password_done:
            pwd_filled = False
            for sel in ["input[type='password']", "input[name='password']", "input[name='Passwd']"]:
                try:
                    if _fill_and_advance(sel, password):
                        password_done = True
                        pwd_filled = True
                        print(f"  [login] Senha preenchida ✓")
                        break
                except Exception as e:
                    print(f"  [login] erro senha sel={sel}: {e}")
            if not pwd_filled:
                time.sleep(2)
            continue

        # Etapa 3: pós-senha — aceitar prompts do Google (salvar senha, etc.)
        if password_done:
            for btn_name in ["Continuar", "Continue", "Aceitar", "Accept", "Não agora", "Not now", "Avançar", "Next"]:
                try:
                    b = page.get_by_role("button", name=btn_name, exact=False).first
                    if b.is_visible(timeout=800):
                        b.click()
                        print(f"  [login] Clicou '{btn_name}' ✓")
                        time.sleep(3)
                        break
                except:
                    pass

        time.sleep(2)

    print(f"  ✗ Não foi possível fazer login no Gmail")
    return False


def create_gmail(page: Page, account_name: str) -> tuple[str, str, int]:
    """
    Cria uma conta Gmail via browser com detecção de etapa.
    Retorna (gmail, password, tzid_sms).
    """
    gmail, password = generate_gmail_credentials(account_name)
    first_name = account_name.split()[0] if account_name else "Usuario"
    last_name = account_name.split()[-1] if len(account_name.split()) > 1 else "IA"
    username = gmail.replace("@gmail.com", "")

    print(f"  Criando Gmail: {gmail}")
    page.goto("https://accounts.google.com/signup/v2/createaccount?flowName=GlifWebSignIn&flowEntry=SignUp", timeout=30000)
    time.sleep(3)

    sms = None
    completed_steps = set()
    max_iterations = 30

    for iteration in range(max_iterations):
        # Detectar telas inesperadas antes de processar o passo
        interruption = _detect_interruption(page)
        if interruption:
            resolved = _handle_interruption(page, interruption)
            if not resolved:
                raise Exception(f"Interrupção não resolvida automaticamente: {interruption}")
            time.sleep(1)

        step = detect_gmail_step(page)
        time.sleep(1)

        if step == "done":
            print(f"  ✓ Gmail criado: {gmail}")
            tzid = sms["tzid"] if sms else 0
            if sms:
                sms_finish(sms["tzid"])
            return gmail, password, tzid

        elif step == "name" and "name" not in completed_steps:
            try:
                page.fill("input[name='firstName']", first_name, timeout=3000)
                page.fill("input[name='lastName']", last_name, timeout=3000)
            except:
                agent_execute(page, f"Preencha Nome com '{first_name}' e Sobrenome com '{last_name}'", max_steps=3)
            try:
                page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
            except:
                page.keyboard.press("Enter")
            completed_steps.add("name")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=6000)
            except:
                time.sleep(2)

        elif step == "birthdate" and "birthdate" not in completed_steps:
            # Dia
            try:
                page.fill("input[name='day']", "15", timeout=3000)
            except:
                pass
            # Ano
            try:
                page.fill("input[name='year']", "1990", timeout=3000)
            except:
                pass
            # Mês — abrir combo, selecionar Junho, fechar
            try:
                combos = page.query_selector_all("[role='combobox']")
                combos[0].click(force=True)
                time.sleep(1)
                page.locator("[role='option']").filter(has_text="Junho").click()
                time.sleep(1)
            except:
                pass
            # Gênero — abrir segundo combo, selecionar Masculino
            try:
                combos = page.query_selector_all("[role='combobox']")
                combos[1].click(force=True)
                time.sleep(1)
                page.locator("[role='option']").filter(has_text="Masculino").first.click()
                time.sleep(1)
            except:
                pass
            time.sleep(1)
            try:
                page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
            except:
                page.keyboard.press("Enter")
            completed_steps.add("birthdate")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=6000)
            except:
                time.sleep(2)

        elif step == "username" and "username" not in completed_steps:
            # Tentar opção "criar próprio endereço"
            try:
                page.click("text=Criar seu próprio endereço", timeout=2000)
                time.sleep(1)
            except:
                try:
                    page.click("text=Create your own Gmail address", timeout=2000)
                    time.sleep(1)
                except:
                    pass
            try:
                page.fill("input[name='Username'], input[id='username']", username, timeout=3000)
            except:
                agent_execute(page, f"Preencha o campo de username/email com '{username}'", max_steps=3)
            try:
                page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
            except:
                page.keyboard.press("Enter")
            completed_steps.add("username")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=6000)
            except:
                time.sleep(2)

        elif step == "password" and "password" not in completed_steps:
            try:
                page.fill("input[name='Passwd'], input[type='password']", password, timeout=3000)
                page.fill("input[name='ConfirmPasswd'], input[name='PasswdAgain']", password, timeout=3000)
            except:
                agent_execute(page, f"Preencha senha e confirmação com '{password}'", max_steps=3)
            try:
                page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
            except:
                page.keyboard.press("Enter")
            completed_steps.add("password")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except:
                time.sleep(3)

        elif step == "phone":
            # Retry com até 3 números diferentes caso o número seja rejeitado
            if not sms:
                for _sms_try in range(3):
                    try:
                        sms = sms_get_number(country=55, service="google")
                        break
                    except Exception as _e:
                        print(f"  SMS tentativa {_sms_try+1} falhou: {_e}")
                        if _sms_try == 2:
                            raise
                        time.sleep(5)
            number_br = sms["number"].replace("+55", "").strip()
            # Tentar vários seletores
            filled = False
            for sel in ["input[id='phoneNumberId']", "input[name='phoneNumber']", "input[type='tel']", "input[autocomplete='tel']"]:
                try:
                    page.fill(sel, number_br, timeout=2000)
                    filled = True
                    break
                except:
                    pass
            if not filled:
                try:
                    page.locator("input:visible").first.fill(number_br)
                    filled = True
                except:
                    pass
            if filled:
                time.sleep(1)
                try:
                    page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
                except:
                    page.keyboard.press("Enter")
                completed_steps.add("phone")
            time.sleep(3)

        elif step == "code" and "code" not in completed_steps:
            if not sms:
                sms = sms_get_number(country=55, service="google")

            # Tentar receber SMS — se timeout, trocar número e voltar para phone
            code = None
            for _code_try in range(3):
                try:
                    code = sms_wait_code(sms["tzid"], timeout=90)
                    if code:
                        break
                except Exception:
                    pass
                print(f"  SMS {sms['number']} não recebeu código (tentativa {_code_try+1}) — trocando número")
                sms_cancel(sms["tzid"])
                sms = sms_get_number(country=55, service="google")
                # Re-submeter novo número na tela de telefone (voltar um passo se possível)
                try:
                    page.go_back(timeout=5000)
                    time.sleep(2)
                except:
                    pass
                number_br = sms["number"].replace("+55", "").strip()
                for sel in ["input[id='phoneNumberId']", "input[name='phoneNumber']", "input[type='tel']"]:
                    try:
                        page.fill(sel, number_br, timeout=2000)
                        break
                    except:
                        pass
                try:
                    page.locator("button:has-text('Avançar'), button:has-text('Próxima'), button:has-text('Next')").first.click(force=True, timeout=5000)
                except:
                    page.keyboard.press("Enter")
                time.sleep(3)

            if not code:
                raise Exception("SMS: código não recebido após 3 tentativas com números diferentes")

            try:
                page.fill("input[name='code'], input[id='code'], input[type='tel']", code, timeout=3000)
            except:
                agent_execute(page, f"Preencha o código com '{code}'", max_steps=3)
            try:
                page.click("button:has-text('Verificar'), button:has-text('Avançar'), button:has-text('Next')", timeout=3000)
            except:
                page.keyboard.press("Enter")
            completed_steps.add("code")
            time.sleep(3)

        elif step == "skip":
            try:
                page.click("button:has-text('Pular'), button:has-text('Skip'), button:has-text('Não')", timeout=3000)
            except:
                try:
                    page.click("text=Pular, text=Skip, text=Não, obrigado", timeout=2000)
                except:
                    agent_execute(page, "Clique em Pular, Skip ou Não obrigado", max_steps=2)
            time.sleep(2)

        elif step == "other":
            # Tentar avançar
            try:
                page.click("button:has-text('Avançar'), button:has-text('Concordo'), button:has-text('I agree')", timeout=3000)
            except:
                pass
            time.sleep(2)

        else:
            time.sleep(2)

    raise Exception("Timeout: fluxo de criação do Gmail não concluiu em tempo")


# ── Google Ads Setup ──────────────────────────────────────────────────────────

def angular_fill(page, selector: str, value: str) -> bool:
    """Preenche campo Angular/React disparando eventos necessários."""
    try:
        page.click(selector, timeout=3000)
        time.sleep(0.2)
        page.evaluate(f"""(sel, val) => {{
            const el = document.querySelector(sel);
            if (!el) return;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}""", [selector, value])
        time.sleep(0.3)
        return True
    except:
        return False


def _fill_in_frame_or_page(page, selectors: list, value: str, label: str) -> bool:
    """Tenta preencher um campo na página principal ou em qualquer iframe."""
    # Página principal
    for sel in selectors:
        try:
            page.click(sel, timeout=1500)
            time.sleep(0.2)
            page.fill(sel, value, timeout=2000)
            print(f"  {label} ✓")
            return True
        except:
            pass
    # Iframes (especialmente payments.google.com)
    for frame in page.frames:
        if frame.url and frame != page.main_frame:
            for sel in selectors:
                try:
                    frame.click(sel, timeout=1500)
                    time.sleep(0.2)
                    frame.fill(sel, value, timeout=2000)
                    print(f"  {label} (iframe) ✓")
                    return True
                except:
                    pass
    return False


def _check_in_frames(page, texts: list, timeout_ms=2000) -> bool:
    """Verifica se algum dos textos está visível em qualquer frame."""
    for frame in page.frames:
        if not frame.url:
            continue
        for txt in texts:
            try:
                if frame.get_by_text(txt, exact=False).first.is_visible(timeout=timeout_ms):
                    return True
            except:
                pass
    return False


def _click_in_frames(page, texts: list, timeout_ms=3000) -> bool:
    """Clica no primeiro texto encontrado em qualquer frame."""
    for frame in page.frames:
        if not frame.url:
            continue
        for txt in texts:
            try:
                el = frame.get_by_text(txt, exact=False).first
                if el.is_visible(timeout=timeout_ms):
                    el.click(timeout=timeout_ms)
                    return True
            except:
                pass
    return False


def _click_button_in_frames(page, names: list, timeout_ms=3000) -> bool:
    """Clica num botão por nome em qualquer frame (exact e partial match)."""
    for frame in page.frames:
        if not frame.url:
            continue
        for name in names:
            # Exact match via role
            try:
                btn = frame.get_by_role("button", name=name)
                if btn.first.is_visible(timeout=timeout_ms):
                    btn.first.click(timeout=timeout_ms)
                    return True
            except:
                pass
            # Partial text match via has-text
            try:
                btn = frame.locator(f"button:has-text('{name}')")
                if btn.first.is_visible(timeout=500):
                    btn.first.click(force=True, timeout=timeout_ms)
                    return True
            except:
                pass
    # Fallback: JS click via textContent partial match in any frame
    name_lower_list = [n.lower() for n in names]
    for frame in page.frames:
        try:
            clicked = frame.evaluate("""(kws) => {
                const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                for (const k of kws) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                return null;
            }""", name_lower_list)
            if clicked:
                return True
        except:
            pass
    return False


def _fill_in_frames(page, selectors: list, value: str, timeout_ms=3000) -> bool:
    """Preenche um campo (por seletor) em qualquer frame."""
    for frame in page.frames:
        if not frame.url:
            continue
        for sel in selectors:
            try:
                el = frame.locator(sel).first
                if el.is_visible(timeout=timeout_ms):
                    el.click(timeout=timeout_ms)
                    time.sleep(0.3)
                    el.fill(value, timeout=timeout_ms)
                    return True
            except:
                pass
    return False


def fill_payment_page(page, account: dict, cnpj_data: dict, _stuck_counter: list = None):
    """Preenche o formulário de pagamento do Google Ads.

    Retorna:
      "done"   — conta criada com sucesso
      "next"   — ação executada, aguardar próxima iteração
      False    — não conseguiu avançar
    """
    if _stuck_counter is None:
        _stuck_counter = [0]
    cnpj = account.get("cnpj", "")  # já só dígitos
    time.sleep(4)

    # ── Detectar sub-estado via conteúdo visível nos frames ─────────────────

    # 1. Modal de CNPJ aberto (modaliframe tem input tel visível)
    modal_cnpj_open = False
    modal_frame = None
    for frame in page.frames:
        if "modaliframe" in (frame.url or ""):
            try:
                if frame.locator('input[type="tel"]').first.is_visible(timeout=1500):
                    modal_cnpj_open = True
                    modal_frame = frame
                    break
            except:
                pass

    # 2. Botão "Criar novo perfil" visível
    criar_perfil = _check_in_frames(page, ["Criar novo perfil para pagamentos", "Create new payments profile"])

    # 3. Botão "Adicionar forma de pagamento" visível (apenas botão clicável, não texto)
    adicionar_pagamento = False
    for _frame in page.frames:
        if not _frame.url:
            continue
        for _name in ["Adicionar forma de pagamento", "Add payment method"]:
            try:
                _btn = _frame.get_by_role("button", name=_name, exact=False).first
                if _btn.is_visible(timeout=800):
                    adicionar_pagamento = True
                    break
            except:
                pass
        if adicionar_pagamento:
            break

    # 4. Formulário do cartão aberto (botão "Salvar cartão" visível)
    card_form_open = False
    for _frame in page.frames:
        if not _frame.url:
            continue
        for _name in ["Salvar cartão", "Save card", "Salvar"]:
            try:
                _btn = _frame.get_by_role("button", name=_name, exact=False).first
                if _btn.is_visible(timeout=800):
                    card_form_open = True
                    break
            except:
                pass
        if card_form_open:
            break
    if not card_form_open:
        card_form_open = _check_in_frames(page, ["Salvar cartão", "Save card"], timeout_ms=800)

    print(f"  [estado] criar={criar_perfil}, modal_cnpj={modal_cnpj_open}, add_pag={adicionar_pagamento}, card_form={card_form_open}")

    # ── Sub-estado: modal CNPJ aberto → preencher e confirmar (PRIORIDADE 1) ──
    if modal_cnpj_open and modal_frame:
        print("  Preenchendo CNPJ no modal...")
        try:
            inp = modal_frame.locator('input[type="tel"]').first
            inp.click(timeout=2000)
            time.sleep(0.2)
            inp.fill(cnpj, timeout=2000)
            print(f"  CNPJ '{cnpj}' preenchido ✓")
        except Exception as e:
            print(f"  ⚠ Erro ao preencher CNPJ: {e}")

        if _click_button_in_frames(page, ["Confirmar", "Confirm"]):
            print("  Clicou 'Confirmar' ✓")
            time.sleep(7)  # aguardar modal seguinte carregar
            return "next"
        else:
            print("  ⚠ Botão Confirmar não encontrado")
            return False

    # ── Sub-estado: modal "Criar um perfil para pagamentos" (campo Nome + botão Criar)
    # Aparece após confirmar CNPJ — preencher Nome com razao_social e clicar Criar
    modal_criar_perfil_frame = None
    for frame in page.frames:
        if not frame.url:
            continue
        try:
            # Detectar pelo placeholder "Nome" OU pelo botão "Criar" (não "Criar novo perfil")
            nome_input = frame.locator('input[placeholder="Nome"], input[placeholder="Name"]').first
            criar_btn = frame.get_by_role("button", name="Criar").first
            if nome_input.is_visible(timeout=1000) or (
                criar_btn.is_visible(timeout=500) and
                not frame.get_by_text("Criar novo perfil para pagamentos", exact=False).first.is_visible(timeout=300)
            ):
                modal_criar_perfil_frame = frame
                break
        except:
            pass

    if modal_criar_perfil_frame:
        razao = cnpj_data["razao_social"]
        print(f"  Preenchendo Nome no modal com '{razao}'...")
        nome_filled = False
        # Buscar input "Nome" em TODOS os frames (pode estar em sub-frame do modal)
        for frame in page.frames:
            if not frame.url:
                continue
            for sel in ['input[placeholder="Nome"]', 'input[placeholder="Name"]',
                        'input[placeholder="nome"]', 'input[type="text"]:not([readonly])']:
                try:
                    inp = frame.locator(sel).first
                    if inp.is_visible(timeout=800):
                        inp.click(timeout=1500)
                        time.sleep(0.2)
                        inp.fill(razao, timeout=2000)
                        val = frame.evaluate(f"document.querySelector('{sel}')?.value")
                        if val and len(str(val)) > 0:
                            print(f"  Nome preenchido ✓ (frame: {frame.url[:40]})")
                            nome_filled = True
                            break
                except:
                    pass
            if nome_filled:
                break
        if not nome_filled:
            print(f"  ⚠ Campo Nome não encontrado — tentando clicar Criar mesmo assim")

        if _click_button_in_frames(page, ["Criar", "Create"]):
            print("  Clicou 'Criar' ✓")
            time.sleep(5)
            return "next"
        else:
            print("  ⚠ Botão 'Criar' não encontrado")
            return False

    # ── Sub-estado: formulário do cartão visível (PRIORIDADE 2) ─────────────
    # Detectado pelo botão "Salvar cartão" OU pelo placeholder "Número do cartão"
    _card_detected = card_form_open
    if not _card_detected:
        for _frame in page.frames:
            try:
                if _frame.get_by_placeholder("Número do cartão").first.is_visible(timeout=500):
                    _card_detected = True
                    break
            except:
                pass
    if not _card_detected:
        for _frame in page.frames:
            try:
                if _frame.get_by_placeholder("MM/AA").first.is_visible(timeout=500):
                    _card_detected = True
                    break
            except:
                pass

    if _card_detected:
        print("  Formulário do cartão aberto → preenchendo via mouse+teclado...")
        card_num = account.get("card_number", "")
        card_exp = account.get("card_exp", "")
        card_cvv = account.get("card_cvv", "")

        card_name = account.get("card_name", "")

        def mouse_fill(description, value):
            coords = ask_claude_coords(page, description)
            if coords:
                print(f"  {description} → '{value[:6]}...'")
                # Triple-click seleciona todo o conteúdo (funciona em iframes sandboxed)
                page.mouse.click(coords[0], coords[1], click_count=3)
                time.sleep(0.3)
                page.keyboard.press("Delete")   # apaga seleção
                time.sleep(0.1)
                page.keyboard.press("Backspace")  # garante campo vazio
                time.sleep(0.1)
                page.keyboard.type(value, delay=50)
                print(f"    ✓")
                return True
            else:
                print(f"  ⚠ Coordenadas não encontradas: {description}")
                return False

        mouse_fill("o campo de entrada 'Número do cartão'", card_num)
        time.sleep(0.5)
        # Remover barra da validade — o campo MM/AA auto-insere a barra ao digitar
        card_exp_digits = re.sub(r"[^0-9]", "", card_exp)
        mouse_fill("o campo de entrada 'MM/AA' de validade do cartão", card_exp_digits)
        time.sleep(0.5)
        mouse_fill("o campo de entrada 'Código de segurança' ou CVV", card_cvv)
        time.sleep(0.5)
        # Nome do titular: limpar o preenchimento automático e colocar o da planilha
        if card_name:
            mouse_fill("o campo 'Nome do titular do cartão'", card_name)
        time.sleep(0.5)

        # Clicar em Salvar cartão — tentar por frame primeiro, depois coordenadas
        saved = _click_button_in_frames(page, ["Salvar cartão", "Save card"])
        if saved:
            print("  Clicou 'Salvar cartão' (frame) ✓")
        else:
            coords = ask_claude_coords(page, "o botão 'Salvar cartão'")
            if coords:
                print(f"  Clicando 'Salvar cartão' @ {coords}")
                page.mouse.click(coords[0], coords[1])
                saved = True
        time.sleep(6)

        # Debug: verificar se formulário fechou ou se há erro
        still_open = _check_in_frames(page, ["Salvar cartão", "Save card"], timeout_ms=1000)
        if still_open:
            card_error = None
            for _fr in page.frames:
                if not _fr.url or "payments.google.com" not in _fr.url:
                    continue
                try:
                    errs = _fr.evaluate("""() =>
                        Array.from(document.querySelectorAll('[role=alert], .error, [aria-live], .Xb9hP'))
                        .map(e => e.textContent.trim().replace(/\\s+/g,' ').substring(0, 80))
                        .filter(t => t.length > 2)
                    """)
                    if errs:
                        card_error = errs[0]
                        print(f"  [erro-cartão] {errs[:2]}")
                except:
                    pass
            # Se erro de cartão inválido/recusado → parar tentativas (não adianta re-tentar)
            if card_error and any(x in card_error for x in ["recusada", "inválida", "OR_CCREU", "declined", "invalid"]):
                print(f"  ✗ Cartão recusado permanentemente — verifique dados na planilha")
                return ("card_error", card_error)
        return "next"

    # ── Sub-estado: "Adicionar cartão de crédito ou débito" visível (PRIORIDADE 3)
    adicionar_cartao = _check_in_frames(page, ["Adicionar cartão de crédito ou débito", "Add credit or debit card"])
    if adicionar_cartao:
        _stuck_counter[0] = 0  # progresso detectado
        print("  Clicando 'Adicionar cartão de crédito ou débito'...")
        if _click_in_frames(page, ["Adicionar cartão de crédito ou débito", "Add credit or debit card"]):
            print("  Clicou 'Adicionar cartão de crédito ou débito' ✓")
            time.sleep(6)
            return "next"
        return False

    # ── Sub-estado: "Adicionar forma de pagamento" clicável (PRIORIDADE 4) ────
    if adicionar_pagamento and not criar_perfil:
        _stuck_counter[0] += 1
        # Se preso neste passo por muitas iterações → recarregar página para resetar modal
        if _stuck_counter[0] >= 3:
            print(f"  [stuck={_stuck_counter[0]}] Recarregando página para resetar modal...")
            try:
                page.reload(wait_until="networkidle", timeout=30000)
            except:
                page.reload(timeout=30000)
            _stuck_counter[0] = 0
            time.sleep(4)
            return "next"
        print("  Clicando 'Adicionar forma de pagamento'...")
        clicked = _click_button_in_frames(page, ["Adicionar forma de pagamento", "Add payment method"])
        if not clicked:
            clicked = _click_in_frames(page, ["Adicionar forma de pagamento", "Add payment method"])
        if not clicked:
            # Fallback: JavaScript click em qualquer botão com esse texto
            clicked = page.evaluate("""() => {
                for (const frame of [document, ...Array.from(document.querySelectorAll('iframe')).map(f => { try { return f.contentDocument; } catch(e) { return null; } }).filter(Boolean)]) {
                    const btns = Array.from(frame.querySelectorAll('button, [role="button"]'));
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim().toLowerCase();
                        if (txt.includes('adicionar forma de pagamento') || txt.includes('add payment method')) {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")
        if clicked:
            print("  Clicou 'Adicionar forma de pagamento' ✓")
            time.sleep(6)
            return "next"
        return False

    # ── Sub-estado: botão "Criar novo perfil" visível → clicar (PRIORIDADE 5) ─
    if criar_perfil:
        print("  Criando perfil para pagamentos...")
        if _click_in_frames(page, ["Criar novo perfil para pagamentos", "Create new payments profile"]):
            print("  Clicou 'Criar novo perfil para pagamentos' ✓")
            time.sleep(4)
            return "next"
        elif adicionar_pagamento:
            if _click_in_frames(page, ["Adicionar forma de pagamento", "Add payment method"]):
                print("  Clicou 'Adicionar forma de pagamento' ✓")
                time.sleep(4)
                return "next"
        return False

    # ── Sub-estado: cartão salvo, aguardando botão "Enviar" / "Salvar" na página principal ─
    # Quando o cartão foi salvo, add_pag desaparece e o frame mostra o cartão cadastrado
    # A página principal (ads.google.com) tem um botão "Enviar" para finalizar o cadastro
    for frame in page.frames:
        url = frame.url or ""
        if "ads.google.com" not in url and "payments.google.com" not in url:
            continue
        for btn_name in ["Enviar", "Submit", "Continuar", "Continue", "Salvar", "Save"]:
            try:
                btn = frame.get_by_role("button", name=btn_name, exact=False).first
                if btn.is_visible(timeout=1000):
                    # Responder "Não" na pergunta de orientações (se visível)
                    for nao_label in ["Não", "No", "Nao"]:
                        try:
                            nao = frame.get_by_label(nao_label, exact=False).first
                            if nao.is_visible(timeout=500):
                                nao.click()
                                time.sleep(0.5)
                                break
                        except:
                            pass
                    print(f"  Clicando '{btn_name}' para finalizar cadastro...")
                    btn.click(timeout=5000)
                    print(f"  Clicou '{btn_name}' ✓")
                    time.sleep(8)
                    return "done"
            except:
                pass

    # Estado desconhecido — debug + tentar avançar
    all_btns = []
    for frame in page.frames:
        frame_url = frame.url or ""
        if "payments.google.com" in frame_url or "ads.google.com" in frame_url:
            try:
                btns = frame.evaluate("""() =>
                    Array.from(document.querySelectorAll('button, [role=button], a'))
                    .filter(b => b.offsetParent).map(b => b.textContent.trim().substring(0, 60))
                """)
                inputs = frame.evaluate("""() =>
                    Array.from(document.querySelectorAll('input:not([type=hidden])'))
                    .map(i => i.type + ':' + (i.placeholder || i.name || i.id))
                """)
                if btns or inputs:
                    print(f"  [debug] Frame {frame_url[:60]}: btns={btns[:6]}, inputs={inputs[:4]}")
                    all_btns.extend(btns)
            except:
                pass

    # Tentar clicar em qualquer botão que sugira progressão
    advance_kw = ["enviar", "submit", "continuar", "continue", "avançar", "next",
                  "salvar", "save", "criar", "create", "confirmar", "confirm",
                  "adicionar", "add"]
    for frame in page.frames:
        frame_url = frame.url or ""
        if "payments.google.com" not in frame_url and "ads.google.com" not in frame_url:
            continue
        try:
            clicked = frame.evaluate("""(kws) => {
                const btns = Array.from(document.querySelectorAll('button,[role="button"]'))
                    .filter(b => b.offsetParent);
                for (const k of kws) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                return null;
            }""", advance_kw)
            if clicked:
                print(f"  [desconhecido] Clicou '{clicked}' ✓")
                time.sleep(4)
                _stuck_counter[0] = 0
                return "next"
        except:
            pass

    # Incrementar stuck counter — retornar "next" até 5x antes de desistir
    _stuck_counter[0] += 1
    if _stuck_counter[0] <= 5:
        print(f"  [desconhecido] Nenhum estado detectado (tentativa {_stuck_counter[0]}/5) — aguardando...")
        time.sleep(3)
        return "next"
    return False



def setup_2fa_authenticator(page: Page, account: dict, sheets=None) -> str | None:
    """
    Configura verificação em duas etapas com aplicativo autenticador na conta Google.
    Retorna o segredo TOTP (base32) se bem-sucedido, None caso contrário.
    Salva o segredo na planilha se sheets for fornecido.
    """
    # Se já tem segredo salvo na planilha, não reconfigurar
    if account.get("totp_secret"):
        print(f"  [2fa] Segredo já existe na planilha — pulando configuração")
        return account["totp_secret"]

    print("  [2fa] Configurando autenticador 2FA...")

    def _click_next_btn():
        """Clica em botões de avanço (Próxima, Next, Continuar, Avançar, etc.)."""
        for label in ["Próxima", "Próximo", "Next", "Continuar", "Avançar", "Avançar ", "Ok"]:
            try:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    time.sleep(1.5)
                    return True
            except:
                pass
        return False

    def _extract_totp_secret() -> str | None:
        """Extrai o segredo TOTP base32 da página atual."""
        try:
            # Tentar pegar via aria-label ou data-secret
            secret = page.evaluate("""() => {
                // Procurar em elementos que contêm segredo TOTP (base32 puro)
                const all = Array.from(document.querySelectorAll('*'));
                for (const el of all) {
                    const txt = (el.textContent || '').trim().replace(/\\s/g, '').toUpperCase();
                    // Segredo base32: 16-32 chars, apenas A-Z e 2-7
                    if (/^[A-Z2-7]{16,32}$/.test(txt)) {
                        return txt;
                    }
                }
                // Fallback: procurar texto com espaços (ex: "ABCD EFGH IJKL MNOP")
                for (const el of all) {
                    const txt = (el.textContent || '').trim().toUpperCase();
                    const m = txt.match(/\\b([A-Z2-7]{4}(?: [A-Z2-7]{4}){3,7})\\b/);
                    if (m) {
                        return m[1].replace(/\\s/g, '');
                    }
                }
                return null;
            }""")
            return secret
        except:
            return None

    try:
        # ── Passo 1: Navegar para a página de 2FA ─────────────────────────────
        page.goto(
            "https://myaccount.google.com/signinoptions/two-step-verification/enroll-welcome",
            wait_until="domcontentloaded", timeout=25000
        )
        time.sleep(3)
        print(f"  [2fa] URL: {page.url}")

        # Se a conta já tem 2FA ativo, a URL muda para a página de gerenciamento
        if "signinoptions/two-step-verification" in page.url and "enroll" not in page.url:
            print("  [2fa] 2FA já configurado nesta conta — pulando")
            return None

        # ── Passo 2: Clicar "Começar" ─────────────────────────────────────────
        for btn_text in ["Começar", "Get started", "Ativar"]:
            try:
                btn = page.locator(f"button:has-text('{btn_text}')")
                if btn.count() > 0:
                    btn.first.click(timeout=4000)
                    time.sleep(2)
                    print(f"  [2fa] Clicou '{btn_text}'")
                    break
            except:
                pass

        # ── Passo 3: Verificação de identidade (senha) se solicitada ──────────
        for _ in range(3):
            pwd_inputs = page.locator("input[type='password']")
            if pwd_inputs.count() > 0:
                print("  [2fa] Confirmando senha...")
                pwd_inputs.first.fill(account.get("password", ""))
                time.sleep(0.5)
                _click_next_btn()
                time.sleep(2)
            else:
                break

        # ── Passo 4: Pular opção de telefone — ir direto ao autenticador ──────
        # Google pode mostrar "telefone" como padrão; precisamos clicar em
        # "Mostrar mais opções" ou "Aplicativo autenticador"
        for _ in range(5):
            cur_url = page.url
            print(f"  [2fa] URL atual: {cur_url}")

            # Verificar se já estamos na tela de QR code
            if _extract_totp_secret():
                print("  [2fa] Segredo detectado na tela atual")
                break

            # Procurar link/botão para aplicativo autenticador
            auth_found = False
            for label in [
                "Aplicativo autenticador", "Authenticator app",
                "Usar aplicativo de autenticação", "Use an authenticator app",
                "Mais opções", "Show more options", "Mostrar mais opções",
            ]:
                try:
                    el = page.locator(f"text='{label}'").first
                    if el.is_visible(timeout=1500):
                        el.click()
                        time.sleep(2)
                        auth_found = True
                        print(f"  [2fa] Clicou '{label}'")
                        break
                except:
                    pass

            if auth_found:
                # Avançar para a tela de QR code
                _click_next_btn()
                time.sleep(2)
                break

            # Tentar avançar se botão de próximo existir
            if _click_next_btn():
                time.sleep(2)
            else:
                time.sleep(1)

        # ── Passo 5: Tela do QR code — clicar "Não consigo ler" ──────────────
        for attempt in range(6):
            secret = _extract_totp_secret()
            if secret:
                print(f"  [2fa] Segredo encontrado: {secret[:8]}...")
                break

            # Procurar link de "não consigo ler o QR"
            for label in [
                "Não consigo ler o código QR",
                "Não consigo digitalizar o código QR",
                "Can't scan it",
                "Can't scan",
                "Não é possível digitalizar",
                "Inserir chave de configuração",
                "Enter setup key",
            ]:
                try:
                    el = page.locator(f"text='{label}'").first
                    if el.is_visible(timeout=1500):
                        el.click()
                        time.sleep(2)
                        print(f"  [2fa] Clicou '{label}'")
                        break
                except:
                    pass

            # Também tentar partial match via JS
            if not secret:
                try:
                    clicked = page.evaluate("""() => {
                        const keywords = ['consigo ler', 'can\\'t scan', 'setup key', 'inserir chave', 'não consigo'];
                        const els = Array.from(document.querySelectorAll('a, button, span[role="button"], div[role="button"]'));
                        for (const el of els) {
                            const txt = (el.textContent || '').toLowerCase();
                            if (keywords.some(kw => txt.includes(kw))) {
                                el.click();
                                return el.textContent.trim();
                            }
                        }
                        return null;
                    }""")
                    if clicked:
                        print(f"  [2fa] Clicou '{clicked}' via JS")
                        time.sleep(2)
                except:
                    pass

            time.sleep(1)
        else:
            secret = _extract_totp_secret()

        if not secret:
            print("  [2fa] ✗ Não foi possível extrair o segredo TOTP")
            return None

        print(f"  [2fa] Segredo TOTP: {secret}")

        # ── Passo 6: Salvar segredo na planilha ───────────────────────────────
        if sheets and account.get("row_index"):
            update_totp_secret(sheets, account["row_index"], secret)
            print("  [2fa] Segredo salvo na planilha ✓")

        # ── Passo 7: Avançar para campo de verificação ────────────────────────
        _click_next_btn()
        time.sleep(2)

        # ── Passo 8: Gerar e inserir código TOTP ──────────────────────────────
        totp = pyotp.TOTP(secret)
        code = totp.now()
        print(f"  [2fa] Código gerado: {code}")

        # Aguardar campo de entrada de código
        code_input = None
        for _ in range(5):
            inputs = page.locator("input[type='number'], input[type='text'][autocomplete='one-time-code'], input[aria-label*='código'], input[aria-label*='code']")
            # Fallback: qualquer input visível
            if inputs.count() == 0:
                inputs = page.locator("input[type='text'], input[type='number'], input:not([type])")
            if inputs.count() > 0:
                code_input = inputs.first
                break
            time.sleep(1)

        if not code_input:
            print("  [2fa] ✗ Campo de código não encontrado")
            return None

        code_input.fill(code)
        time.sleep(0.5)

        # ── Passo 9: Clicar "Verificar" ───────────────────────────────────────
        verified = False
        for label in ["Verificar", "Verify", "Confirmar", "Confirm"]:
            try:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    time.sleep(2)
                    verified = True
                    print(f"  [2fa] Clicou '{label}'")
                    break
            except:
                pass

        if not verified:
            _click_next_btn()
            time.sleep(2)

        # ── Passo 10: Confirmar ativação ──────────────────────────────────────
        for label in ["Ativar", "Enable", "Ligar", "Turn on", "Confirmar"]:
            try:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count() > 0:
                    btn.first.click(timeout=3000)
                    time.sleep(2)
                    print(f"  [2fa] Clicou '{label}' — 2FA ativado!")
                    break
            except:
                pass

        # Verificar se 2FA foi ativado (URL muda para página de gerenciamento)
        time.sleep(2)
        final_url = page.url
        if "two-step-verification" in final_url and "enroll" not in final_url:
            print("  [2fa] ✓ Verificação em duas etapas ativada com sucesso!")
        else:
            print(f"  [2fa] ⚠ URL final: {final_url} — verifique manualmente se foi ativado")

        return secret

    except Exception as e:
        print(f"  [2fa] ✗ Erro: {e}")
        return None


def verify_account(page: Page, account: dict) -> bool:
    """Realiza todas as verificações de política/conta do Google Ads automaticamente.

    Tarefas executadas:
      - Anúncios políticos na UE → Não
      - Perguntas sobre a organização → própria empresa + Não gerencia outras
      - Quem paga pelos anúncios → própria empresa
      - Dun & Bradstreet → submete formulário com dados pré-preenchidos
    """
    import re as _re

    # Obter URL params da conta atual para montar URL de política
    def _extract_params(url_str):
        return dict(_re.findall(r'([^?&=\s]+)=([^&\s]+)', url_str.split('?')[1] if '?' in url_str else ''))

    url = page.url
    params = _extract_params(url)
    ocid  = params.get('ocid', '')
    euid  = params.get('euid', '')
    u     = params.get('__u', '')
    uscid = params.get('uscid', '')
    c     = params.get('__c', '')

    # Se faltam params essenciais, navegar para overview para obtê-los
    if not ocid or not euid:
        print("  [verify] Params incompletos — navegando para overview...")
        try:
            page.goto("https://ads.google.com/aw/overview", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass
            time.sleep(4)
            url = page.url
            params = _extract_params(url)
            ocid  = params.get('ocid', '')
            euid  = params.get('euid', '')
            u     = params.get('__u', '')
            uscid = params.get('uscid', '')
            c     = params.get('__c', '')
            print(f"  [verify] Params após overview: ocid={ocid}, euid={euid[:8] if euid else ''}")
        except Exception as e:
            print(f"  [verify] Erro ao navegar overview: {e}")

    if not ocid:
        print("  [verify] Não foi possível extrair params da URL — pulando verificações")
        return False

    policy_url = (
        f"https://ads.google.com/aw/policy/account"
        f"?ocid={ocid}&euid={euid}&__u={u}&uscid={uscid}&__c={c}&authuser=0&hl=pt-BR"
    )

    def _get_task_buttons():
        """Retorna lista de botões 'Iniciar tarefa' {disabled, y, x, context}."""
        return page.evaluate("""() => {
            const spans = Array.from(document.querySelectorAll('span'))
                .filter(s => s.textContent.trim() === 'Iniciar tarefa');
            return spans.map(s => {
                const mb = s.closest('material-button') || s.closest('button');
                const rect = s.getBoundingClientRect();
                // Tentar vários níveis de parent para achar o container da tarefa
                let parent = null;
                let el = s;
                for (let i = 0; i < 12; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    const txt = (el.textContent || '').replace(/\\s+/g,' ').trim();
                    // Encontrou container útil: tem texto além de "Iniciar tarefa"
                    const withoutBtn = txt.replace(/Iniciar tarefa/g, '').replace(/\\s+/g,' ').trim();
                    if (withoutBtn.length > 15) {
                        parent = el;
                        break;
                    }
                }
                const raw = (parent?.textContent || '').replace(/\\s+/g,' ').trim();
                // Remover "Iniciar tarefa" do contexto para deixar só o título
                const ctx = raw.replace(/Iniciar tarefa/g, '').replace(/\\s+/g,' ').trim().slice(0,120);
                return {
                    disabled: !!(mb?.classList?.contains('is-disabled')),
                    y: rect.y, x: rect.x,
                    context: ctx
                };
            });
        }""")

    def _get_completed_tasks():
        """Retorna textos das tarefas concluídas."""
        return page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('[class*="completed"], [class*="done"]'));
            const texts = items.map(el => el.textContent.replace(/\\s+/g,' ').trim().slice(0,80));
            // Also look for checkmark icons next to task text
            const checks = Array.from(document.querySelectorAll('*')).filter(el => {
                const txt = el.textContent || '';
                return txt.includes('✓') || txt.includes('check_circle') || txt.includes('Respondido');
            }).map(el => el.textContent.replace(/\\s+/g,' ').trim().slice(0,80));
            return [...new Set([...texts, ...checks])].filter(t => t.length > 10);
        }""")

    def _click_radio_by_text(texts: list, fallback_index: int = 0) -> bool:
        """Clica no radio que contém algum dos textos; fallback por índice."""
        radios = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[role="radio"]')).map(r => ({
                text: r.textContent.trim(),
                checked: r.getAttribute('aria-checked'),
                x: r.getBoundingClientRect().x,
                y: r.getBoundingClientRect().y,
                w: r.getBoundingClientRect().width,
                h: r.getBoundingClientRect().height,
            }));
        }""")
        if not radios:
            return False
        target = None
        for txt in texts:
            for r in radios:
                if txt.lower() in r['text'].lower():
                    target = r
                    break
            if target:
                break
        if not target and fallback_index < len(radios):
            target = radios[fallback_index]
        if target:
            page.mouse.click(target['x'] + target['w'] / 2, target['y'] + target['h'] / 2)
            return True
        return False

    def _js_click_button(keywords: list) -> str | None:
        """Clica no primeiro elemento visível que contém alguma keyword. Retorna texto ou None."""
        return page.evaluate("""(kws) => {
            // Busca ampla: button, material-button, role=button, e qualquer elemento visível
            const candidates = Array.from(document.querySelectorAll(
                'button, material-button, [role="button"], a, div, span'
            )).filter(el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;  // sem offsetParent — panels fixed não têm
            });
            for (const kw of kws) {
                const el = candidates.find(el => {
                    const children = Array.from(el.childNodes);
                    // Preferir elementos folha (sem filhos com texto) para evitar containers
                    const ownText = children
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join('').toLowerCase();
                    const fullText = (el.textContent || '').trim().toLowerCase();
                    return (ownText.includes(kw) || fullText === kw);
                });
                if (el) { el.click(); return el.textContent.trim(); }
                // Fallback: qualquer elemento com texto exato
                const el2 = candidates.find(el =>
                    (el.textContent||'').trim().toLowerCase() === kw
                );
                if (el2) { el2.click(); return el2.textContent.trim(); }
            }
            return null;
        }""", keywords)

    def _handle_modal(task_name: str) -> bool:
        """Processa um modal aberto com radios + botão Enviar/Enviar resposta/Enviar inscrição."""
        time.sleep(2)
        try:
            radios = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('[role="radio"]')).map(r => ({
                    text: r.textContent.trim(),
                    checked: r.getAttribute('aria-checked'),
                    x: r.getBoundingClientRect().x,
                    y: r.getBoundingClientRect().y,
                    w: r.getBoundingClientRect().width,
                    h: r.getBoundingClientRect().height,
                }));
            }""")
        except Exception as e:
            print(f"  [verify] Erro ao buscar radios ({task_name}): {e}")
            return False
        # Aguardar até 10s pelo modal aparecer
        if not radios:
            for _ in range(5):
                time.sleep(2)
                radios = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('[role="radio"]')).map(r => ({
                        text: r.textContent.trim(),
                        checked: r.getAttribute('aria-checked'),
                        x: r.getBoundingClientRect().x,
                        y: r.getBoundingClientRect().y,
                        w: r.getBoundingClientRect().width,
                        h: r.getBoundingClientRect().height,
                    }));
                }""")
                if radios:
                    break
        if not radios:
            return False

        if task_name == "eu_ads":
            # Selecionar "Não pretendo usar esta conta..."
            _click_radio_by_text(["Não pretendo", "não pretendo", "não", "No, I"], fallback_index=1)

        elif task_name == "org":
            # Q1: selecionar própria empresa (primeiro radio que NÃO é "Razão social de outra")
            _click_radio_by_text(["LTDA", "própria", account.get("name","")], fallback_index=0)
            time.sleep(1)
            # Q2 pode aparecer após Q1 — re-buscar radios
            radios2 = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('[role="radio"]')).map(r => ({
                    text: r.textContent.trim(),
                    checked: r.getAttribute('aria-checked'),
                    x: r.getBoundingClientRect().x,
                    y: r.getBoundingClientRect().y,
                    w: r.getBoundingClientRect().width,
                    h: r.getBoundingClientRect().height,
                }));
            }""")
            # Procurar radio "Não" para a pergunta de gerenciar outras orgs
            for r in radios2:
                if r['checked'] == 'false':
                    txt = r['text'].lower()
                    if 'não' in txt and ('gerencia' in txt or 'não gerencia' in txt or 'não,' in txt):
                        page.mouse.click(r['x'] + r['w']/2, r['y'] + r['h']/2)
                        break
            else:
                # Fallback: clicar no último radio (geralmente é o "Não")
                last_unchecked = [r for r in radios2 if r['checked'] == 'false']
                if last_unchecked:
                    r = last_unchecked[-1]
                    page.mouse.click(r['x'] + r['w']/2, r['y'] + r['h']/2)

        elif task_name == "quem_paga":
            # Selecionar própria empresa paga (primeiro radio — empresa paga por si mesma)
            razao_lower = account.get("name", "").split("-")[0].strip().lower()
            search_texts = ["LTDA", "S.A.", "S.A", "própria", razao_lower] if razao_lower else ["LTDA", "S.A.", "própria"]
            clicked_radio = _click_radio_by_text(search_texts, fallback_index=0)
            time.sleep(0.5)
            # Verificar se radio ficou selecionado; se não, clicar pelo index 0
            radio_checked = page.evaluate("""() => {
                const r = document.querySelector('[role="radio"][aria-checked="true"]');
                return r ? (r.textContent||'').trim().slice(0, 50) : null;
            }""")
            if not radio_checked:
                # Forçar clique no primeiro radio visível
                page.evaluate("""() => {
                    const r = Array.from(document.querySelectorAll('[role="radio"]'))
                        .find(r => r.getBoundingClientRect().width > 0);
                    if (r) r.click();
                }""")
                time.sleep(0.3)
            else:
                print(f"  [verify] Radio selecionado: '{radio_checked[:40]}'")

        time.sleep(0.5)
        # Contar radios antes de enviar (para detectar se o modal fechou)
        radios_before = len(radios)

        clicked = _js_click_button(["enviar inscrição", "enviar resposta", "enviar"])
        if not clicked:
            # Fallback: procurar botão de envio por texto exato
            clicked = page.evaluate("""() => {
                const kws = ['enviar inscrição', 'enviar resposta', 'enviar'];
                const all = Array.from(document.querySelectorAll('*'))
                    .filter(el => el.offsetParent);
                for (const kw of kws) {
                    const el = all.find(el => (el.textContent||'').trim().toLowerCase() === kw);
                    if (el) { el.click(); return el.textContent.trim(); }
                }
                return null;
            }""")
        if clicked:
            print(f"  [verify] '{task_name}' → clicou '{clicked}' — aguardando confirmação...")
            # Verificar se o modal fechou (radios desapareceram) — prova de que foi aceito
            confirmed = False
            for _ in range(8):
                time.sleep(1.5)
                try:
                    radios_after = page.evaluate("""() =>
                        Array.from(document.querySelectorAll('[role="radio"]')).length
                    """)
                    if radios_after == 0:
                        confirmed = True
                        break
                except:
                    confirmed = True
                    break
            if confirmed:
                print(f"  [verify] '{task_name}' → modal fechou ✓ (tarefa concluída)")
                return True
            else:
                print(f"  [verify] '{task_name}' → modal ainda aberto ✗ (formulário não aceito)")
                return False
        return False

    def _handle_db() -> bool:
        """Abre o painel D&B e clica em 'Iniciar a verificação'."""
        btns = _get_task_buttons()
        db_btn = None
        for b in btns:
            ctx = b.get('context','').lower()
            if not b['disabled'] and ('dun' in ctx or 'bradstreet' in ctx or 'forneça' in ctx):
                db_btn = b
                break
        if not db_btn:
            return False

        # Clicar via Playwright locator (mais robusto que mouse.click)
        # Encontrar o índice deste botão entre todos os "Iniciar tarefa"
        all_task_btns = page.locator("material-button:has-text('Iniciar tarefa'), button:has-text('Iniciar tarefa')")
        btn_count = all_task_btns.count()
        clicked_open = False
        for idx in range(btn_count):
            try:
                bbox = all_task_btns.nth(idx).bounding_box()
                if bbox and abs(bbox['y'] - db_btn['y']) < 20:
                    all_task_btns.nth(idx).scroll_into_view_if_needed()
                    time.sleep(0.3)
                    all_task_btns.nth(idx).click(force=True, timeout=3000)
                    clicked_open = True
                    break
            except:
                pass
        if not clicked_open:
            # Fallback: clicar pelo índice baseado na posição (D&B geralmente é o 2º botão)
            btns_all = _get_task_buttons()
            db_index = next((i for i, b in enumerate(btns_all) if 'bradstreet' in b.get('context','').lower() or 'forneça' in b.get('context','').lower()), 1)
            try:
                all_task_btns.nth(db_index).scroll_into_view_if_needed()
                time.sleep(0.3)
                all_task_btns.nth(db_index).click(force=True, timeout=3000)
                clicked_open = True
            except:
                pass
        time.sleep(4)

        # Aguardar iframe identityverification carregar completamente
        clicked = False
        for _wait in range(15):
            time.sleep(2)
            identity_frames = [f for f in page.frames if f.url and ('identityverif' in f.url or 'identityverification' in f.url)]
            if not identity_frames:
                if _wait == 3:
                    print(f"  [verify] D&B: aguardando iframe... frames={[f.url[:40] for f in page.frames if f.url][:3]}")
                continue
            for frame in identity_frames:
                try:
                    # Aguardar carregamento do frame
                    try:
                        frame.wait_for_load_state("domcontentloaded", timeout=3000)
                    except:
                        pass
                    # Verificar conteúdo do frame
                    content_info = frame.evaluate("""() => ({
                        bodyText: (document.body?.innerText || '').slice(0, 200),
                        btnCount: document.querySelectorAll('button,[role="button"]').length,
                        allText: Array.from(document.querySelectorAll('button,[role="button"],a'))
                            .map(el => (el.textContent||'').trim().slice(0, 30))
                            .filter(t => t.length > 0)
                    })""")
                    if _wait <= 1:
                        print(f"  [verify] D&B frame: btns={content_info['btnCount']}, texts={content_info['allText'][:3]}")
                    # Clicar no botão
                    result = frame.evaluate("""() => {
                        const all = Array.from(document.querySelectorAll('*'));
                        const kws = ['iniciar a verifica', 'iniciar verifica', 'start verif'];
                        for (const kw of kws) {
                            const el = all.find(el => (el.textContent||'').trim().toLowerCase().includes(kw) && (el.textContent||'').trim().length < 50);
                            if (el && typeof el.click === 'function') { el.click(); return (el.textContent||'').trim(); }
                        }
                        // Fallback: primeiro botão visível no frame
                        const btns = Array.from(document.querySelectorAll('button,[role="button"]'))
                            .filter(b => b.getBoundingClientRect().width > 0);
                        if (btns.length) { btns[0].click(); return btns[0].textContent.trim(); }
                        return null;
                    }""")
                    if result:
                        clicked = result
                        break
                except Exception as e:
                    if _wait <= 1:
                        print(f"  [verify] D&B frame erro: {e}")
            if clicked:
                break

        if clicked:
            print(f"  [verify] D&B: clicou '{clicked}' ✓")
            time.sleep(5)
            # Submeter o formulário se carregar
            for frame in page.frames:
                frame_url = frame.url or ''
                if 'identityverif' in frame_url or 'identityverification' in frame_url:
                    try:
                        submit_result = frame.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            const s = btns.find(b => (b.textContent||'').trim().toLowerCase().includes('enviar'));
                            if (s) { s.click(); return s.textContent.trim(); }
                            return null;
                        }""")
                        if submit_result:
                            print(f"  [verify] D&B formulário: clicou '{submit_result}' ✓")
                            time.sleep(5)
                    except:
                        pass
            print("  [verify] D&B verificação iniciada ✓")
            # Fechar painel D&B (Escape ou botão Fechar) para liberar a tela
            time.sleep(2)
            try:
                page.keyboard.press("Escape")
                time.sleep(1)
            except:
                pass
            try:
                page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                    const close = btns.find(b => {
                        const t = (b.textContent||'').trim().toLowerCase();
                        return t === 'fechar' || t === 'close' || t === '×' || t === 'x';
                    });
                    if (close) close.click();
                }""")
            except:
                pass
            time.sleep(2)
            return True

        # Debug: listar frames disponíveis
        frames_dbg = [(f.url or '')[:60] for f in page.frames if f.url]
        print(f"  [verify] D&B: frames={frames_dbg}")
        return False

    def _task_already_done(keyword: str) -> bool:
        """Verifica se uma tarefa já foi concluída."""
        return page.evaluate("""(kw) => {
            const allText = document.body.innerText.toLowerCase();
            // Procurar seção "Tarefas concluídas" com o keyword
            const sections = Array.from(document.querySelectorAll('*'));
            for (const el of sections) {
                const txt = (el.textContent || '').toLowerCase();
                if (txt.includes('respondido') && txt.includes(kw)) return true;
                if (txt.includes('confirmado') && txt.includes(kw)) return true;
                if (txt.includes('forneceu') && txt.includes(kw)) return true;
            }
            return false;
        }""", keyword)

    # ── Navegar para página de política ──────────────────────────────────────
    print("  [verify] Acessando página de verificações...")
    # Verificar se já há uma aba com policy/account aberta (com params válidos)
    policy_page = None
    try:
        for pg in page.context.pages:
            pg_url = pg.url or ''
            if 'policy/account' in pg_url and ('ocid=' in pg_url or 'euid=' in pg_url):
                policy_page = pg
                # Preferir aba com euid completo
                if euid and euid in pg_url:
                    break
    except:
        pass

    if policy_page:
        if policy_page != page:
            print(f"  [verify] Usando aba existente: {policy_page.url[:70]}")
        page = policy_page
        # Recarregar se necessário para garantir estado fresco
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except:
            pass
        time.sleep(2)
    else:
        try:
            page.goto(policy_url, timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            time.sleep(5)
        except Exception as e:
            print(f"  [verify] Erro ao navegar: {e}")
    time.sleep(2)
    print(f"  [verify] URL política: {page.url[:80]}")

    completed = set()
    max_iterations = 15

    for iteration in range(max_iterations):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except:
            pass
        time.sleep(2)

        # Verificar se a página ainda está aberta (pode ter sido fechada por navegação)
        try:
            _ = page.url
        except Exception:
            # Tentar recuperar a aba com a política
            try:
                for pg in page.context.pages:
                    if 'policy/account' in (pg.url or '') and ocid in (pg.url or ''):
                        page = pg
                        print(f"  [verify] Página recuperada: {page.url[:60]}")
                        break
            except:
                print("  [verify] Página fechada — encerrando")
                break

        # Ler tarefas disponíveis
        try:
            btns = _get_task_buttons()
        except Exception as e:
            print(f"  [verify] Erro ao ler tarefas: {e}")
            break
        enabled = [b for b in btns if not b['disabled']]

        if not enabled:
            print("  [verify] Nenhuma tarefa pendente — verificações concluídas ✓")
            break

        # Mapear índices dos botões habilitados para locator
        all_task_loc = page.locator("material-button:has-text('Iniciar tarefa'), button:has-text('Iniciar tarefa')")

        def _click_task_btn(target_y: float):
            """Clica no botão 'Iniciar tarefa' mais próximo de target_y usando Playwright locator."""
            total = all_task_loc.count()
            best_idx = 0
            best_dist = float('inf')
            for i in range(total):
                try:
                    bb = all_task_loc.nth(i).bounding_box()
                    if bb and abs(bb['y'] - target_y) < best_dist:
                        best_dist = abs(bb['y'] - target_y)
                        best_idx = i
                except:
                    pass
            try:
                all_task_loc.nth(best_idx).scroll_into_view_if_needed()
                time.sleep(0.3)
                all_task_loc.nth(best_idx).click(force=True, timeout=3000)
                return True
            except Exception as e:
                return False

        acted = False
        for b in enabled:
            ctx = b.get('context', '').lower()

            # ── EU Political Ads ──
            if ('union' in ctx or 'europeia' in ctx or ' ue' in ctx or 'política' in ctx
                    or 'politic' in ctx or 'políticos' in ctx or 'planeja veicular' in ctx) \
                    and 'eu_ads' not in completed:
                print("  [verify] Respondendo EU political ads...")
                _click_task_btn(b['y'])
                time.sleep(2)
                if _handle_modal("eu_ads"):
                    completed.add('eu_ads')
                    acted = True
                    break

            # ── Organização ──
            elif ('organização' in ctx or 'organization' in ctx or 'perguntas' in ctx
                    or 'afiliação' in ctx) \
                    and 'org' not in completed:
                print("  [verify] Respondendo perguntas da organização...")
                _click_task_btn(b['y'])
                time.sleep(2)
                if _handle_modal("org"):
                    completed.add('org')
                    acted = True
                    break

            # ── Quem paga ──
            elif ('paga' in ctx or 'paga pelos' in ctx or 'pays' in ctx) \
                    and 'quem_paga' not in completed:
                print("  [verify] Respondendo quem paga pelos anúncios...")
                _click_task_btn(b['y'])
                time.sleep(2)
                if _handle_modal("quem_paga"):
                    completed.add('quem_paga')
                    acted = True
                    break

            # ── D&B ──
            elif ('dun' in ctx or 'bradstreet' in ctx or 'forneça' in ctx) \
                    and 'db' not in completed:
                print("  [verify] Enviando informações D&B...")
                if _handle_db():
                    completed.add('db')
                    acted = True
                    break

        if acted:
            # Recarregar página após cada tarefa para estado limpo
            time.sleep(3)
            try:
                page.reload(wait_until="networkidle", timeout=15000)
            except:
                try:
                    page.reload(timeout=15000)
                except:
                    pass
            time.sleep(3)
            continue

        if not acted:
            # Verificar se os botões restantes são todos tarefas já concluídas
            # (ex: D&B fica como "Iniciar tarefa" por dias enquanto processa)
            known_done = {'db': ['dun', 'bradstreet', 'forneça'],
                          'eu_ads': ['union', 'europeia', 'politic', 'políticos', 'planeja veicular'],
                          'org': ['organização', 'organization', 'afiliação', 'perguntas'],
                          'quem_paga': ['paga', 'pays']}
            all_already_done = True
            for b in enabled:
                ctx_b = b.get('context', '').lower()
                task_key = None
                for k, kws in known_done.items():
                    if any(kw in ctx_b for kw in kws):
                        task_key = k
                        break
                if task_key is None or task_key not in completed:
                    all_already_done = False
                    break
            if all_already_done and completed:
                print("  [verify] Todas as tarefas reconhecidas foram concluídas ✓")
                break

            # Nenhum dos botões foi reconhecido — clicar no primeiro e tentar responder
            if enabled:
                b = enabled[0]
                ctx = b.get('context','').lower()
                print(f"  [verify] Tarefa não reconhecida: '{ctx[:80]}' — tentando clicar...")
                page.mouse.click(b['x'] + 40, b['y'] + 9)
                time.sleep(2)
                # Verificar se abriu modal com radios
                radios_now = page.evaluate("""() =>
                    Array.from(document.querySelectorAll('[role="radio"]')).map(r => r.textContent.trim())
                """)
                modal_text = page.evaluate("""() => {
                    const dlg = document.querySelector('[role="dialog"], [role="alertdialog"], .modal, [class*="modal"]');
                    return (dlg?.textContent || '').replace(/\\s+/g,' ').trim().slice(0,200);
                }""") or ""
                print(f"  [verify] Modal: radios={radios_now[:3]}, texto='{modal_text[:80]}'")
                if radios_now:
                    # Tentar identificar pelo modal
                    modal_lower = modal_text.lower()
                    if 'union' in modal_lower or 'europeia' in modal_lower or 'eu' in modal_lower or 'política' in modal_lower or 'political' in modal_lower:
                        if _handle_modal("eu_ads"):
                            completed.add('eu_ads')
                            acted = True
                    elif 'organização' in modal_lower or 'organization' in modal_lower or 'empresa' in modal_lower or 'perguntas' in modal_lower:
                        if _handle_modal("org"):
                            completed.add('org')
                            acted = True
                    elif 'paga' in modal_lower or 'pays' in modal_lower:
                        if _handle_modal("quem_paga"):
                            completed.add('quem_paga')
                            acted = True
                    else:
                        # Fallback: selecionar primeiro radio e enviar
                        if radios_now:
                            r_list = page.evaluate("""() =>
                                Array.from(document.querySelectorAll('[role="radio"]')).map(r => ({
                                    x: r.getBoundingClientRect().x,
                                    y: r.getBoundingClientRect().y,
                                    w: r.getBoundingClientRect().width,
                                    h: r.getBoundingClientRect().height,
                                    checked: r.getAttribute('aria-checked')
                                }))
                            """)
                            # Clicar no primeiro unchecked ou no primeiro
                            to_click = next((r for r in r_list if r['checked'] == 'false'), r_list[0] if r_list else None)
                            if to_click:
                                page.mouse.click(to_click['x'] + to_click['w']/2, to_click['y'] + to_click['h']/2)
                                time.sleep(0.5)
                        clicked_send = _js_click_button(["enviar inscrição", "enviar resposta", "enviar"])
                        if clicked_send:
                            print(f"  [verify] Fallback: clicou '{clicked_send}' ✓")
                            completed.add(f'unknown_{iteration}')
                            acted = True
                            time.sleep(3)
                else:
                    # Sem radios — fechar modal se houver e continuar
                    _js_click_button(["fechar", "close", "cancelar", "cancel", "ok"])
                    time.sleep(1)
            if not acted:
                break

    print(f"  [verify] Tarefas concluídas: {completed}")
    return len(completed) > 0


def configure_content_suitability(page: Page) -> bool:
    """Configura a Adequação de Conteúdo na conta Google Ads recém criada.

    Seções configuradas:
    1. Inventário → Padrão
    2. Conteúdo sensível excluído → todas as opções
    3. Rótulos e tipos excluídos → 6 opções específicas
    4. Temas de conteúdo excluídos → Jogos (luta) + Jogos (somente para adultos)
    5. Canais excluídos → Categorias de aplicativos (todos os 140 apps)
    """
    print("  [content] Configurando Adequação do Conteúdo...")

    import re as _re
    current_url = page.url
    ocid = _re.search(r'ocid=(\d+)', current_url)
    u    = _re.search(r'__u=(\d+)', current_url)
    c    = _re.search(r'__c=(\d+)', current_url)
    auth = _re.search(r'authuser=(\d+)', current_url)

    if not (ocid and u and c):
        print("  [content] Não foi possível extrair parâmetros da URL — pulando")
        return False

    cs_url = (f"https://ads.google.com/aw/contentsuitability"
              f"?ocid={ocid.group(1)}&__u={u.group(1)}&__c={c.group(1)}"
              f"&authuser={auth.group(1) if auth else '0'}&hl=pt-BR")

    page.goto(cs_url, timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except:
        pass
    time.sleep(3)

    # ── helpers locais ──────────────────────────────────────────────────────

    def _dismiss_modal():
        """Descarta modal 'Alterações não salvas' clicando em 'Sim, continuar'."""
        btn = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('material-button, button, [role="button"]'));
            for (const b of btns) {
                const t = b.textContent?.trim() || '';
                if (t.includes('Sim, continuar') || t.includes('Discard')) {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0) return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
                }
            }
            return null;
        }""")
        if btn:
            page.mouse.click(btn['x'], btn['y'])
            time.sleep(1.5)
            return True
        return False

    def _open_panel(keyword: str) -> bool:
        """Abre painel material-expansionpanel cujo HEADER contenha keyword, tratando modais."""
        kw = repr(keyword.lower())
        for _ in range(5):
            r = page.evaluate(f"""() => {{
                const panels = Array.from(document.querySelectorAll('material-expansionpanel'));
                // Usar apenas o texto do header (não o textContent completo do painel)
                // para evitar falso match por texto do body de painéis vizinhos
                const panel = panels.find(p => {{
                    const h = p.querySelector('div.header');
                    return (h?.textContent||'').toLowerCase().includes({kw});
                }});
                if (!panel) return 'not_found';
                if (panel.querySelector('div.panel')?.classList.contains('open')) return 'already_open';
                const header = panel.querySelector('div.header');
                panel.scrollIntoView({{block: 'center'}});
                const rect = header?.getBoundingClientRect();
                if (!rect || rect.y < 30) return {{error: rect?.y}};
                return {{x: Math.round(rect.x + rect.width/2), y: Math.round(rect.y + rect.height/2)}};
            }}""")
            if r == 'already_open':
                return True
            if isinstance(r, dict) and 'x' in r:
                page.mouse.click(r['x'], r['y'])
                time.sleep(1.5)
                # tratar modal se aparecer
                modal = page.evaluate("""() => !!document.querySelector('material-dialog, [role="dialog"]')""")
                if modal:
                    _dismiss_modal()
                    time.sleep(0.5)
                ok = page.evaluate(f"""() => {{
                    const p = Array.from(document.querySelectorAll('material-expansionpanel'))
                        .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes({kw}));
                    return p?.querySelector('div.panel')?.classList.contains('open') || false;
                }}""")
                if ok:
                    return True
            else:
                time.sleep(0.5)
        return False

    def _save_open_panel():
        """Faz scrollIntoView no botão Salvar do painel aberto e clica."""
        for _ in range(5):
            # Usar scrollIntoView no próprio botão para garantir visibilidade
            btn = page.evaluate("""() => {
                const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                    .find(p => p.querySelector('div.panel.open'));
                if (!panel) return null;
                for (const b of Array.from(panel.querySelectorAll('material-button, button'))) {
                    if (b.textContent?.trim() === 'Salvar') {
                        b.scrollIntoView({block: 'center'});
                        const r = b.getBoundingClientRect();
                        if (r.width > 0 && r.y > 0)
                            return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
                    }
                }
                return null;
            }""")
            if btn:
                page.mouse.click(btn['x'], btn['y'])
                time.sleep(2)
                return True
            time.sleep(0.5)
        return False

    def _get_panel_cbs(keyword: str) -> list:
        """Retorna checkboxes do painel aberto cujo HEADER contenha keyword."""
        kw = repr(keyword.lower())
        return page.evaluate(f"""() => {{
            const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes({kw})
                          && p.querySelector('div.panel.open'));
            if (!panel) return [];
            return Array.from(panel.querySelectorAll('material-checkbox, mat-checkbox')).map(cb => {{
                const r = cb.getBoundingClientRect();
                return {{
                    text: (cb.textContent||'').trim().substring(0, 80),
                    checked: cb.getAttribute('aria-checked'),
                    x: Math.round(r.x + r.width/2),
                    y: Math.round(r.y + r.height/2)
                }};
            }});
        }}""")

    def _click_panel_cb(keyword: str, cb_text_fragment: str, to_check: bool) -> bool:
        """Clica num checkbox específico do painel via JS direto (sem coordenadas).
        Faz scrollIntoView antes do click para garantir visibilidade."""
        kw = repr(keyword.lower())
        frag = repr(cb_text_fragment.lower())
        action = 'true' if to_check else 'false'
        return page.evaluate(f"""() => {{
            const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes({kw})
                          && p.querySelector('div.panel.open'));
            if (!panel) return false;
            const cbs = Array.from(panel.querySelectorAll('material-checkbox, mat-checkbox'));
            const cb = cbs.find(c => (c.textContent||'').toLowerCase().includes({frag}));
            if (!cb) return false;
            const want = {action};
            const has = cb.getAttribute('aria-checked') === 'true';
            if (has === want) return true;  // já no estado correto
            cb.scrollIntoView({{block: 'nearest'}});
            // Tentar clicar via dispatchEvent para garantir que Angular detecta
            cb.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
            return true;
        }}""")

    # ── 1. Conteúdo sensível excluído (geralmente já aberto ao carregar) ──────
    print("  [content] 1/5 Conteúdo sensível excluído...")
    _open_panel("sensível")
    time.sleep(1)

    # Retry se checkboxes ainda não renderizaram (painel pode estar open mas lazy)
    cbs_sensivel = []
    for _ in range(5):
        cbs_sensivel = _get_panel_cbs("sensível")
        if cbs_sensivel:
            break
        time.sleep(0.8)
    print(f"  [content]   sensível: {len(cbs_sensivel)} checkboxes")

    for cb in cbs_sensivel:
        if cb['checked'] == 'true':
            continue  # já marcado
        frag = repr((cb['text'] or '').lower()[:40])
        clicked = page.evaluate(f"""() => {{
            const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes('sensível')
                          && p.querySelector('div.panel.open'));
            if (!panel) return false;
            const cb = Array.from(panel.querySelectorAll('material-checkbox, mat-checkbox'))
                .find(c => (c.textContent||'').toLowerCase().includes({frag}));
            if (!cb) return false;
            cb.scrollIntoView({{block: 'nearest'}});
            const r = cb.getBoundingClientRect();
            if (r.width === 0) return false;
            return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
        }}""")
        if clicked and isinstance(clicked, dict):
            page.mouse.click(clicked['x'], clicked['y'])
            time.sleep(0.2)
    _save_open_panel()
    print("  [content] ✓ Conteúdo sensível salvo")

    # ── 2. Inventário → Padrão ───────────────────────────────────────────────
    print("  [content] 2/5 Inventário padrão...")
    _open_panel("inventário")
    time.sleep(0.5)

    # Usar radio-box com div.wrapper[role="button"] — padrão é o segundo card
    standard_coords = page.evaluate("""() => {
        const rbs = Array.from(document.querySelectorAll('radio-box'));
        const rb = rbs.find(r => (r.textContent||'').includes('Inventário padrão'));
        if (!rb) return null;
        const wrapper = rb.querySelector('div.wrapper[role="button"]') || rb;
        wrapper.scrollIntoView({block: 'center'});
        const r = wrapper.getBoundingClientRect();
        if (r.width === 0) return null;
        return {x: Math.round(r.x + 20), y: Math.round(r.y + 20)};
    }""")
    if standard_coords:
        page.mouse.click(standard_coords['x'], standard_coords['y'])
        time.sleep(0.5)

    _save_open_panel()
    print("  [content] ✓ Inventário salvo")

    # ── 3. Rótulos e tipos excluídos ─────────────────────────────────────────
    print("  [content] 3/5 Rótulos e tipos excluídos...")
    _open_panel("rótulos")
    time.sleep(0.5)

    cbs_rotulos = _get_panel_cbs("rótulos")
    # Marcar apenas os 6 corretos: Transmissão, Incorporados, Abaixo da dobra,
    # Domínios reservados, DL-PG (qualquer hífen), Conteúdo não classificado
    rotulos_targets = ["transmissão", "incorporados", "abaixo da dobra",
                       "domínios reservados", "dl", "não classificado"]
    # Garantir que NÃO marcamos DL-G, DL-T, DL-MA (apenas DL-PG/supervisão)
    rotulos_avoid = ["gerais", "adolescentes", "adultos"]
    for cb in cbs_rotulos:
        t = (cb['text'] or '').lower()
        should_be = any(kw in t for kw in rotulos_targets) and not any(av in t for av in rotulos_avoid)
        is_checked = cb['checked'] == 'true'
        if is_checked == should_be:
            continue  # já no estado correto
        # Usar JS click com scrollIntoView para garantir visibilidade
        frag = repr(t[:40])
        clicked = page.evaluate(f"""() => {{
            const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes('rótulos')
                          && p.querySelector('div.panel.open'));
            if (!panel) return false;
            const cb = Array.from(panel.querySelectorAll('material-checkbox, mat-checkbox'))
                .find(c => (c.textContent||'').toLowerCase().includes({frag}));
            if (!cb) return false;
            cb.scrollIntoView({{block: 'nearest'}});
            const r = cb.getBoundingClientRect();
            if (r.width === 0) return false;
            return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
        }}""")
        if clicked and isinstance(clicked, dict):
            page.mouse.click(clicked['x'], clicked['y'])
            time.sleep(0.3)

    _save_open_panel()
    print("  [content] ✓ Rótulos e tipos salvo")

    # ── 4. Temas de conteúdo excluídos ───────────────────────────────────────
    print("  [content] 4/5 Temas de conteúdo excluídos...")
    _open_panel("temas")
    time.sleep(0.5)

    cbs_temas = _get_panel_cbs("temas")
    # Marcar APENAS Jogos (luta) e Jogos (somente para adultos)
    temas_keep = ["luta", "adultos"]
    for cb in cbs_temas:
        t = (cb['text'] or '').lower()
        should_be = any(kw in t for kw in temas_keep)
        is_checked = cb['checked'] == 'true'
        if is_checked == should_be:
            continue
        frag = repr(t[:40])
        clicked = page.evaluate(f"""() => {{
            const panel = Array.from(document.querySelectorAll('material-expansionpanel'))
                .find(p => (p.querySelector('div.header')?.textContent||'').toLowerCase().includes('temas')
                          && p.querySelector('div.panel.open'));
            if (!panel) return false;
            const cb = Array.from(panel.querySelectorAll('material-checkbox, mat-checkbox'))
                .find(c => (c.textContent||'').toLowerCase().includes({frag}));
            if (!cb) return false;
            cb.scrollIntoView({{block: 'nearest'}});
            const r = cb.getBoundingClientRect();
            if (r.width === 0) return false;
            return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
        }}""")
        if clicked and isinstance(clicked, dict):
            page.mouse.click(clicked['x'], clicked['y'])
            time.sleep(0.3)

    _save_open_panel()
    print("  [content] ✓ Temas de conteúdo salvo")

    # ── 5. Canais excluídos → todas as Categorias de aplicativos ─────────────
    print("  [content] 5/5 Canais excluídos — categorias de aplicativos...")
    _open_panel("canais excluídos")
    time.sleep(1)

    # Navegar pelo picker: pode estar na raiz ou já dentro de "Aplicativos"
    # Clicar em "Categorias de aplicativos" até chegar no nível correto
    for _ in range(3):
        cat_btn = page.evaluate("""() => {
            const container = document.querySelector('div.section-body-content');
            if (!container) return null;
            const all = Array.from(container.querySelectorAll('*'));
            for (const el of all) {
                const t = (el.textContent||'').trim();
                if (t.startsWith('Categorias de aplicativos') && el.offsetHeight > 0 && el.offsetHeight < 60) {
                    const r = el.getBoundingClientRect();
                    if (r.y > 200 && r.width > 0)
                        return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), text: t.substring(0,50)};
                }
            }
            return null;
        }""")
        if not cat_btn:
            break
        page.mouse.click(cat_btn['x'], cat_btn['y'])
        time.sleep(2)
        # Verificar se agora mostra Apple App Store / Google Play
        in_cats = page.evaluate("""() => {
            const c = document.querySelector('div.section-body-content');
            return (c?.innerText || '').includes('Apple App Store') || (c?.innerText || '').includes('Google Play');
        }""")
        if in_cats:
            break

    def _expand_picker_group(name_contains: str) -> bool:
        """Expande um grupo colapsado dentro do picker scrollável."""
        container_sel = 'div.section-body-content'
        for _ in range(5):
            scroll_target = page.evaluate(f"""() => {{
                const container = document.querySelector({repr(container_sel)});
                if (!container) return null;
                const contRect = container.getBoundingClientRect();
                const center_y = contRect.y + contRect.height / 2;
                const rows = Array.from(container.querySelectorAll('.row'));
                const grp = rows.find(r =>
                    r.textContent?.includes('expand_more') &&
                    (r.textContent||'').toLowerCase().includes({repr(name_contains.lower())})
                );
                if (!grp) return null;
                const r = grp.getBoundingClientRect();
                const adjust = (r.y + r.height/2) - center_y;
                return {{newScrollTop: Math.max(0, Math.round(container.scrollTop + adjust))}};
            }}""")
            if not scroll_target:
                return True  # já expandido ou não existe
            page.evaluate(f"() => {{ document.querySelector({repr(container_sel)}).scrollTop = {scroll_target['newScrollTop']}; }}")
            time.sleep(0.4)
            coords = page.evaluate(f"""() => {{
                const container = document.querySelector({repr(container_sel)});
                if (!container) return null;
                const contRect = container.getBoundingClientRect();
                const rows = Array.from(container.querySelectorAll('.row'));
                const grp = rows.find(r =>
                    r.textContent?.includes('expand_more') &&
                    (r.textContent||'').toLowerCase().includes({repr(name_contains.lower())})
                );
                if (!grp) return null;
                const r = grp.getBoundingClientRect();
                if (r.y < contRect.y || r.y + r.height > contRect.bottom) return null;
                return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
            }}""")
            if coords:
                page.mouse.click(coords['x'], coords['y'])
                time.sleep(1.5)
                still = page.evaluate(f"""() => {{
                    const c = document.querySelector({repr(container_sel)});
                    return Array.from(c?.querySelectorAll('.row') || []).some(r =>
                        r.textContent?.includes('expand_more') &&
                        (r.textContent||'').toLowerCase().includes({repr(name_contains.lower())})
                    );
                }}""")
                if not still:
                    return True
        return False

    def _check_all_in_picker() -> int:
        """Scrolla pelo picker e marca todos os checkboxes não marcados."""
        container_sel = 'div.section-body-content'
        page.evaluate(f"() => {{ const c=document.querySelector({repr(container_sel)}); if(c) c.scrollTop=0; }}")
        time.sleep(0.3)
        total = 0
        for _ in range(150):
            unchecked = page.evaluate(f"""() => {{
                const container = document.querySelector({repr(container_sel)});
                if (!container) return [];
                const contRect = container.getBoundingClientRect();
                return Array.from(document.querySelectorAll('material-checkbox.check'))
                    .filter(cb => {{
                        const r = cb.getBoundingClientRect();
                        return r.y >= contRect.y && r.y + r.height <= contRect.bottom &&
                               cb.getAttribute('aria-checked') !== 'true';
                    }}).map(cb => {{
                        const r = cb.getBoundingClientRect();
                        return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
                    }});
            }}""")
            for cb in unchecked:
                page.mouse.click(cb['x'], cb['y'])
                time.sleep(0.1)
                total += 1
            res = page.evaluate(f"""() => {{
                const c = document.querySelector({repr(container_sel)});
                if (!c) return {{done: true}};
                c.scrollTop += 100;
                return {{done: c.scrollTop >= c.scrollHeight - c.clientHeight}};
            }}""")
            time.sleep(0.15)
            if res.get('done'):
                unchecked = page.evaluate(f"""() => {{
                    const container = document.querySelector({repr(container_sel)});
                    if (!container) return [];
                    const contRect = container.getBoundingClientRect();
                    return Array.from(document.querySelectorAll('material-checkbox.check'))
                        .filter(cb => {{
                            const r = cb.getBoundingClientRect();
                            return r.y >= contRect.y && r.y + r.height <= contRect.bottom &&
                                   cb.getAttribute('aria-checked') !== 'true';
                        }}).map(cb => {{
                            const r = cb.getBoundingClientRect();
                            return {{x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}};
                        }});
                }}""")
                for cb in unchecked:
                    page.mouse.click(cb['x'], cb['y'])
                    time.sleep(0.1)
                    total += 1
                break
        return total

    # Expandir Apple App Store e Google Play
    _expand_picker_group("apple app store")
    time.sleep(0.5)
    _check_all_in_picker()

    _expand_picker_group("google play")
    time.sleep(0.5)
    _check_all_in_picker()

    # Expandir sub-grupos ainda colapsados (Games, Magazines, Stickers, etc.)
    for _ in range(20):
        collapsed = page.evaluate("""() => {
            const container = document.querySelector('div.section-body-content');
            if (!container) return [];
            return Array.from(container.querySelectorAll('.row'))
                .filter(r => r.textContent?.includes('expand_more'))
                .map(r => (r.textContent||'').replace('expand_more','').trim().split('(')[0].trim().toLowerCase());
        }""")
        if not collapsed:
            break
        grp_key = collapsed[0]
        print(f"  [content]   expandindo sub-grupo: {grp_key}")
        _expand_picker_group(grp_key)
        time.sleep(0.5)
        _check_all_in_picker()

    counter = page.evaluate("""() => document.querySelector('.group-header')?.textContent?.trim()""")
    print(f"  [content]   Picker: {counter}")

    # Salvar (botão está no painel de Canais, não no picker)
    _save_open_panel()
    print("  [content] ✓ Canais excluídos salvo")
    print("  [content] ✓ Adequação de Conteúdo configurada!")
    return True


def detect_ads_step(page: Page) -> str:
    """Detecta a etapa atual do fluxo de criação de conta Google Ads via URL + visão."""
    url = page.url

    # Sinal primário: URL (mais confiável que visão)
    if "selectaccount" in url:
        print(f"  [selectaccount]", end=" ")
        return "selectaccount"
    if "/aw/signup/payment" in url:
        print(f"  [payment]", end=" ")
        return "payment"
    if "/aw/signup/congrats" in url:
        print(f"  [congrats]", end=" ")
        return "congrats"
    if "/aw/identity" in url:
        print(f"  [identity]", end=" ")
        return "identity"
    if ("/signup/mobile/business" in url or "/signup/v2/business" in url
            or "aboutyourbusiness" in url or "currentStep=business" in url):
        print(f"  [business]", end=" ")
        return "business"
    if "accounts.google.com" in url:
        print(f"  [login]", end=" ")
        return "login"
    # Painel do Google Ads (conta criada com sucesso)
    if "/aw/overview" in url or "/aw/campaigns" in url or "/aw/home" in url:
        print(f"  [done]", end=" ")
        return "done"

    # Fallback: visão para distinguir campaign/confirm/business
    img_b64 = screenshot_b64(page)
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": (
                    "Analise esta tela do Google Ads. Responda com UMA palavra:\n"
                    "- 'campaign' se mostrar 'Criar sua primeira campanha' OU pedir URL do site para anúncio, objetivo, orçamento, palavras-chave, segmentação, criação de anúncio\n"
                    "- 'confirm' se mostrar 'Confirmar as configurações da sua conta' com título exato 'Confirmar as configurações' E campos de Fuso horário e Unidade monetária\n"
                    "- 'done' se mostrar painel principal do Google Ads com campanhas\n"
                    "- 'login' se pedir login/senha\n"
                    "Responda APENAS a palavra, sem explicação."
                )}
            ]
        }]
    )
    step = response.content[0].text.strip().lower().split()[0]
    print(f"  [{step}]", end=" ")
    return step


def setup_google_ads(page: Page, account: dict, cnpj_data: dict, sheets=None):
    """Cria a conta Google Ads com billing usando detecção visual de etapas."""
    print("  Configurando Google Ads...")
    page.goto("https://ads.google.com/aw/signup?hl=pt-BR", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except:
        pass
    time.sleep(3)

    if "accounts.google.com" in page.url:
        print("  Não está logado.")
        return False

    razao = cnpj_data["razao_social"]
    address = f"{cnpj_data['logradouro']} {cnpj_data['numero']}".strip()
    municipio = cnpj_data.get("municipio", "")
    cep = cnpj_data.get("cep", "")

    for step_num in range(50):
        # Tentar "Configure apenas a conta" primeiro — pula TODA criação de campanha
        try:
            skip_link = page.get_by_text("Configure apenas a conta", exact=False)
            if skip_link.first.is_visible(timeout=1000):
                skip_link.first.click(timeout=3000)
                print(f"\n  ✓ Clicou 'Configure apenas a conta' — pulando campanha!")
                time.sleep(4)
                continue
        except:
            pass

        step = detect_ads_step(page)

        if step == "done" or step == "congrats":
            print(f"\n  ✓ Conta criada!")
            return True

        if step == "login":
            print(f"\n  Não está logado.")
            return False

        if step == "selectaccount":
            print(f"Selecionando conta Google Ads...")
            # Clicar em "Nova conta Google Ads" / "New Google Ads account"
            clicked = page.evaluate("""() => {
                const kw = ["nova conta", "new google ads", "criar conta", "create account",
                            "새 google ads", "new account"];
                const btns = Array.from(document.querySelectorAll('button,[role="button"],a'));
                for (const k of kw) {
                    const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                    if (b) { b.click(); return b.textContent.trim(); }
                }
                // Fallback: clicar no primeiro botão visível que não seja "trocar conta"
                const skip = ["trocar", "switch", "전환"];
                const first = btns.find(b => {
                    const t = (b.textContent||'').trim().toLowerCase();
                    return t.length > 0 && !skip.some(s => t.includes(s));
                });
                if (first) { first.click(); return first.textContent.trim(); }
                return null;
            }""")
            if clicked:
                print(f"    Clicou '{clicked.strip()}' ✓")
            time.sleep(4)
            continue

        if step == "campaign":
            print(f"Avançando campanha... (URL: {page.url.split('?')[0].split('/')[-1]})")
            # Limpar campo "nome da empresa" (opcional) se preenchido com URL
            try:
                page.evaluate("""() => {
                    const inputs = Array.from(document.querySelectorAll('input'));
                    for (const inp of inputs) {
                        const ph = (inp.placeholder || '').toLowerCase();
                        const isCompanyName = ph.includes('nome da sua empresa') || ph.includes('company name') || ph.includes('nome da empresa');
                        if (isCompanyName && inp.value) {
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            setter.call(inp, '');
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }
                }""")
            except:
                pass
            # Preencher URL — múltiplas estratégias
            url_filled = False

            # Estratégia 1: get_by_placeholder (Playwright nativo)
            for ph in ["Insira um URL de página da Web", "URL de página da Web", "Enter a web page URL"]:
                try:
                    page.get_by_placeholder(ph).first.click(timeout=1500)
                    time.sleep(0.3)
                    page.get_by_placeholder(ph).first.fill("https://www.google.com", timeout=2000)
                    url_filled = True
                    break
                except:
                    pass

            # Estratégia 2: CSS selectors
            if not url_filled:
                for sel in [
                    "input[placeholder*='URL de página']",
                    "input[placeholder*='URL da Web']",
                    "input[placeholder*='página da Web']",
                    "input[aria-label*='URL']",
                    "input[type='url']",
                    "input[name*='url']",
                    "input[name*='Url']",
                ]:
                    try:
                        page.click(sel, timeout=1500)
                        time.sleep(0.3)
                        page.fill(sel, "https://www.google.com", timeout=2000)
                        url_filled = True
                        break
                    except:
                        pass

            # Estratégia 3: JavaScript React/Angular hack — apenas inputs de URL
            if not url_filled:
                try:
                    filled = page.evaluate("""() => {
                        const inputs = Array.from(document.querySelectorAll('input'));
                        const urlInput = inputs.find(i =>
                            (i.placeholder || '').toLowerCase().includes('url') ||
                            (i.type === 'url') ||
                            (i.name || '').toLowerCase().includes('url')
                        );
                        if (!urlInput) return false;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(urlInput, 'https://www.google.com');
                        urlInput.dispatchEvent(new Event('input', {bubbles: true}));
                        urlInput.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""")
                    url_filled = bool(filled)
                except:
                    pass

            if url_filled:
                print(f"    URL preenchida ✓")
                try:
                    page.keyboard.press("Tab")  # trigger form validation
                except:
                    pass
                time.sleep(0.5)

            # Avançar para próxima etapa
            advanced = False

            # Scroll para garantir que botões estejam visíveis
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
            except:
                pass

            # Estratégia de botão depende de ter preenchido URL ou não
            # - URL preenchida: usar Avançar (envia o formulário com URL)
            # - Sem URL: usar Pular (skip etapas opcionais como "escolher meta")
            if url_filled:
                btn_order = ['avançar', 'próxima', 'próximo', 'next', 'continuar', 'continue', 'salvar']
            else:
                btn_order = ['pular', 'skip', 'avançar', 'próxima', 'próximo', 'next', 'continuar', 'continue']

            try:
                keywords_js = str(btn_order).replace("'", '"')
                # Busca por keyword em ordem de prioridade (match exato ou texto curto)
                clicked = page.evaluate(f"""() => {{
                    const keywords = {keywords_js};
                    const getText = e => (e.textContent || e.innerText || '').trim().toLowerCase();
                    const elems = Array.from(document.querySelectorAll('button, [role="button"], a'));
                    for (const k of keywords) {{
                        // Match exato OU texto curto que contém a keyword (evita links de acessibilidade longos)
                        const el = elems.find(e => {{
                            const t = getText(e);
                            return t === k || (t.length <= k.length + 5 && t.includes(k));
                        }});
                        if (el) {{ el.click(); return el.textContent.trim(); }}
                    }}
                    return null;
                }}""")
                if clicked:
                    print(f"    Botão JS: '{clicked.strip()}' ✓")
                    advanced = True
                    time.sleep(3)
            except:
                pass

            if not advanced:
                btn_texts = ['Pular', 'Skip', 'Avançar', 'Próxima', 'Próximo', 'Next', 'Continuar', 'Continue', 'Salvar e continuar'] \
                    if not url_filled else \
                    ['Avançar', 'Próxima', 'Próximo', 'Next', 'Continuar', 'Continue', 'Pular', 'Skip']
                for btn_text in btn_texts:
                    try:
                        page.locator(f"button:has-text('{btn_text}'), [role='button']:has-text('{btn_text}')").first.click(force=True, timeout=1500)
                        advanced = True
                        time.sleep(3)
                        break
                    except:
                        pass

            # Tab + Enter após preencher URL
            if not advanced and url_filled:
                try:
                    page.keyboard.press("Tab")
                    time.sleep(0.3)
                    page.keyboard.press("Enter")
                    advanced = True
                    time.sleep(3)
                except:
                    pass

            if not advanced:
                # Log botões visíveis para diagnóstico
                try:
                    btns = page.evaluate("""() =>
                        Array.from(document.querySelectorAll('button, [role="button"]'))
                        .filter(b => b.offsetParent !== null)
                        .map(b => b.textContent.trim().substring(0, 30))
                    """)
                    print(f"    Botões visíveis: {btns[:5]}")
                except:
                    pass
                agent_execute(page,
                    "Clique no botão azul para avançar para a próxima etapa.",
                    max_steps=3
                )
                time.sleep(3)

        elif step == "confirm":
            print(f"Confirmando configurações da conta...")
            clicked = page.evaluate("""() => {
                const keywords = ["continuar", "continue", "next", "avançar"];
                const getText = e => (e.textContent || e.innerText || '').trim().toLowerCase();
                const elems = Array.from(document.querySelectorAll('button, [role="button"]'));
                for (const k of keywords) {
                    const el = elems.find(e => {
                        const t = getText(e);
                        return t === k || (t.length <= k.length + 5 && t.includes(k));
                    });
                    if (el) { el.click(); return el.textContent.trim(); }
                }
                return null;
            }""")
            if clicked:
                print(f"    Botão '{clicked.strip()}' ✓")
            time.sleep(4)

        elif step == "identity":
            print(f"Confirmando identidade...")
            # Selecionar primeiro radio disponível (nome da empresa) e clicar Enviar
            try:
                radios = page.locator("input[type='radio']")
                count = radios.count()
                if count > 0:
                    radios.first.click(timeout=3000)
                    print(f"    Radio selecionado ✓")
            except:
                pass
            time.sleep(1)
            for btn_name in ["Enviar", "Submit", "Continuar", "Continue", "Avançar", "Next"]:
                try:
                    btn = page.get_by_role("button", name=btn_name, exact=False).first
                    if btn.is_visible(timeout=1500):
                        btn.click(timeout=5000)
                        print(f"    Clicou '{btn_name}' ✓")
                        time.sleep(4)
                        break
                except:
                    pass

        elif step == "business":
            current_url = page.url
            # Detectar sub-passo via currentStep param
            cs_match = re.search(r'currentStep=([^&\s#]+)', current_url)
            current_step_val = cs_match.group(1).lower() if cs_match else "business"
            print(f"Preenchendo empresa... (currentStep={current_step_val})")
            time.sleep(1)

            def _fill_nth_input(n: int, value: str) -> bool:
                """Preenche o n-ésimo input da página (0-indexed) via JS nativo."""
                return page.evaluate("""([n, val]) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="text"],input[type="url"],input:not([type])'));
                    const inp = inputs[n];
                    if (!inp) return false;
                    inp.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(inp, val);
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    inp.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                    return true;
                }""", [n, value])

            def _click_btn_playwright(keywords: list) -> str:
                """Tenta clicar um botão com Playwright (coordenadas reais) antes de JS."""
                kw_lower = [k.lower() for k in keywords]
                try:
                    result = page.evaluate("""(kws) => {
                        const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                        for (const k of kws) {
                            const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                            if (!b) continue;
                            const r = b.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), text: b.textContent.trim()};
                        }
                        return null;
                    }""", kw_lower)
                    if result:
                        page.mouse.click(result['x'], result['y'])
                        return result['text']
                except:
                    pass
                # fallback JS click
                clicked = page.evaluate("""(kws) => {
                    const btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                    for (const k of kws) {
                        const b = btns.find(b => (b.textContent||'').trim().toLowerCase().includes(k));
                        if (b) { b.click(); return b.textContent.trim(); }
                    }
                    return null;
                }""", kw_lower)
                return clicked or ""

            if current_step_val == "linking":
                # Passo de vincular conta existente — pular
                print(f"  [business] Passo 'linking' — pulando...")
                clicked = _click_btn_playwright(["pular", "skip", "não tenho", "continuar sem", "next", "continue"])
                if not clicked:
                    # Tentar Playwright locator
                    for btn in ["Pular", "Skip", "Continuar", "Continue", "Avançar", "Next"]:
                        try:
                            page.locator(f"button:has-text('{btn}')").first.click(force=True, timeout=2000)
                            clicked = btn
                            break
                        except:
                            pass
                if clicked:
                    print(f"  [business] Clicou '{clicked}' ✓")
                time.sleep(3)

            elif current_step_val in ("expert", "keyword", "keywords"):
                # Passo de palavra-chave — pular
                print(f"  [business] Passo '{current_step_val}' — pulando...")
                clicked = _click_btn_playwright(["pular", "skip", "next", "continuar", "continue", "avançar"])
                if clicked:
                    print(f"  [business] Clicou '{clicked}' ✓")
                time.sleep(3)

            else:
                # currentStep=business (ou desconhecido) — preencher formulário
                # Campo 0: nome da empresa
                filled = _fill_nth_input(0, razao)
                if filled:
                    print(f"  [business] Nome da empresa preenchido ✓")
                time.sleep(0.3)

                # Campo 1: URL do site
                site_url = "https://www.google.com"
                filled_url = _fill_nth_input(1, site_url)
                if filled_url:
                    print(f"  [business] URL preenchida ✓")
                time.sleep(0.3)

                # Campo 2: palavra-chave (se existir — opcional mas pode ser obrigatório)
                n_inputs = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('input[type="text"],input[type="url"],input:not([type])')).length;
                }""")
                if n_inputs and n_inputs >= 3:
                    _fill_nth_input(2, "produto")
                    print(f"  [business] Palavra-chave preenchida ✓")
                    time.sleep(0.3)

                time.sleep(0.5)
                # Clicar Avançar com mouse real
                clicked = _click_btn_playwright(["avançar", "próximo", "próxima", "next", "continuar", "continue", "salvar"])
                if clicked:
                    print(f"  [business] Clicou '{clicked}' ✓")
                time.sleep(3)

        elif step == "payment":
            if "_payment_stuck" not in locals():
                _payment_stuck = [0]
            result = fill_payment_page(page, account, cnpj_data, _payment_stuck)
            time.sleep(4)
            if result == "done":
                # Não retornar ainda — continuar para tratar congrats/identity
                continue
            if isinstance(result, tuple) and result[0] == "card_error":
                _, card_err_msg = result
                print(f"  ✗ Cartão recusado — registrando na planilha")
                if sheets:
                    update_status(sheets, account["row_index"], "Verificar", f"Cartão recusado: {card_err_msg}")
                break
            if result is False:
                print(f"  ✗ Pagamento falhou — encerrando")
                break
            # Continua o loop — pode haver mais passos na página de pagamento

        else:
            # Etapa desconhecida — tentar avançar
            print(f"Tentando avançar...")
            for btn in ['Próxima', 'Avançar', 'Next', 'Continuar', 'Continue']:
                try:
                    page.locator(f"button:has-text('{btn}')").first.click(force=True, timeout=2000)
                    time.sleep(3)
                    break
                except:
                    pass

    print(f"\n  Timeout navegando Google Ads. URL: {page.url}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run(test_mode: bool = True):
    _init_logging()
    sheets = get_sheets()
    accounts = read_accounts(sheets)

    if not accounts:
        print("Nenhuma conta pendente.")
        return

    if test_mode:
        accounts = accounts[:1]
        print("[MODO TESTE] Processando 1 conta.")

    print(f"\nContas: {len(accounts)}")
    group_id = get_or_create_group()

    print("Mapeando proxies...")
    proxy_pool = build_proxy_pool()
    if not proxy_pool:
        raise Exception("Nenhum proxy disponível.")
    time.sleep(2)  # cooldown após scan de proxies

    for idx, account in enumerate(accounts):
        print(f"\n[{idx+1}/{len(accounts)}] {account['name']}")
        profile_id = None
        tzid = None

        try:
            # CNPJ
            print(f"  CNPJ: {account['cnpj']}")
            cnpj_data = lookup_cnpj(account["cnpj"])
            print(f"  Razão Social: {cnpj_data['razao_social']}")

            # Proxy
            proxy_index = idx // MAX_ACCOUNTS_PER_PROXY
            if proxy_index >= len(proxy_pool):
                raise Exception("Proxies esgotados.")
            pid, proxy_config, usage = proxy_pool[proxy_index]
            print(f"  Proxy {pid}: {proxy_config['proxy_host']}:{proxy_config['proxy_port']}")

            gmail_from_sheet = account.get("gmail", "").strip()
            print(f"  Gmail planilha: '{gmail_from_sheet}'")

            # Se há Gmail na planilha, buscar perfil por ele (com fallback por nome)
            if gmail_from_sheet:
                existing_id = find_existing_profile(gmail_from_sheet, account["name"])
                print(f"  find_existing_profile → {existing_id}")
                if existing_id:
                    profile_id = existing_id
                    gmail_final = gmail_from_sheet
                    password_final = account.get("password", GMAIL_DEFAULT_PASSWORD)
                    print(f"  Perfil existente: {profile_id} ({gmail_final})")
                    skip_gmail = True
                else:
                    # Gmail na planilha mas sem perfil — criar perfil com esse Gmail
                    update_status(sheets, account["row_index"], "Criando perfil...")
                    profile_id = create_profile(account["name"], gmail_from_sheet, GMAIL_DEFAULT_PASSWORD, group_id, proxy_config)
                    gmail_final = gmail_from_sheet
                    password_final = account.get("password", GMAIL_DEFAULT_PASSWORD) or GMAIL_DEFAULT_PASSWORD
                    skip_gmail = True  # Gmail já existe, não precisa criar
            else:
                # Sem Gmail na planilha — criar Gmail do zero
                gmail, password = generate_gmail_credentials(account["name"])
                update_status(sheets, account["row_index"], "Criando perfil...")
                profile_id = create_profile(account["name"], gmail, password, group_id, proxy_config)
                gmail_final = gmail
                password_final = password
                skip_gmail = False

            # Abrir browser
            update_status(sheets, account["row_index"], "Abrindo browser...")
            ws = open_profile(profile_id)
            time.sleep(3)

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(ws)
                context = browser.contexts[0]
                # Filtrar páginas reais (ignorar devtools://)
                real_pages = [pg for pg in context.pages if not pg.url.startswith("devtools://")]
                if real_pages:
                    page = real_pages[0]
                    for old_page in context.pages:
                        if old_page != page:
                            try: old_page.close()
                            except: pass
                elif context.pages:
                    # Todas são devtools — abrir nova aba
                    page = context.new_page()
                    for old_page in context.pages:
                        if old_page != page:
                            try: old_page.close()
                            except: pass
                else:
                    page = context.new_page()

                # Criar Gmail apenas se não tiver perfil existente com Gmail
                if not skip_gmail:
                    update_status(sheets, account["row_index"], "Criando Gmail...")
                    gmail_final, password_final, tzid = create_gmail(page, account["name"])
                    update_status(sheets, account["row_index"], "Gmail criado")
                elif not is_gmail_logged_in(page):
                    # Perfil existe mas sessão expirou — fazer login
                    update_status(sheets, account["row_index"], "Fazendo login Gmail...")
                    login_result = login_gmail(page, gmail_final, password_final, account.get("recovery_email", ""))
                    if login_result == "manual":
                        msg = f"⚠️ *{account['name']}* — Login Gmail requer verificação manual\n`{gmail_final}`"
                        update_status(sheets, account["row_index"], "Verificar", "Login Gmail requer verificação manual")
                        notify_slack(msg)
                        raise Exception("Login Gmail requer verificação manual")
                    if not login_result:
                        update_status(sheets, account["row_index"], "Erro", "Login Gmail falhou")
                        notify_slack(f"❌ *{account['name']}* — Login Gmail falhou (`{gmail_final}`)")
                        raise Exception("Login Gmail falhou")
                    update_status(sheets, account["row_index"], "Gmail OK")

                # Salvar Gmail na planilha
                update_gmail_created(sheets, account["row_index"], gmail_final, password_final, cnpj_data["razao_social"])

                # Atualizar nome do perfil no AdsPower
                requests.post(f"{ADSPOWER_URL}/api/v1/user/update", json={
                    "user_id": profile_id,
                    "name": account["name"],
                    "username": gmail_final,
                    "password": password_final,
                })

                # Configurar 2FA com autenticador
                update_status(sheets, account["row_index"], "Configurando 2FA...")
                account["password"] = password_final  # garantir senha atualizada
                try:
                    totp_secret = setup_2fa_authenticator(page, account, sheets)
                    if totp_secret:
                        print(f"  ✓ 2FA configurado!")
                        update_status(sheets, account["row_index"], "2FA OK")
                    else:
                        print(f"  ⚠ 2FA não configurado — continuando sem ele")
                        update_status(sheets, account["row_index"], "2FA pulado")
                except Exception as tfa_e:
                    print(f"  ⚠ Erro no 2FA: {tfa_e} — continuando")

                # Criar Google Ads
                update_status(sheets, account["row_index"], "Criando Google Ads...")
                success = setup_google_ads(page, account, cnpj_data, sheets)

                if success:
                    print(f"  ✓ Google Ads criado! Iniciando verificações...")
                    update_status(sheets, account["row_index"], "Verificando políticas...")
                    try:
                        verify_account(page, account)
                        update_status(sheets, account["row_index"], "Verificações OK")
                    except Exception as ve:
                        print(f"  ⚠ Erro nas verificações: {ve}")
                        save_screenshot(page, account["name"], "verify_erro")
                    update_status(sheets, account["row_index"], "Configurando conteúdo...")
                    try:
                        configure_content_suitability(page)
                    except Exception as ce:
                        print(f"  ⚠ Erro na adequação de conteúdo: {ce}")
                        save_screenshot(page, account["name"], "content_erro")
                    update_status(sheets, account["row_index"], "Criado", "Verificações e adequação de conteúdo concluídas")
                    notify_slack(f"✅ *{account['name']}* — conta criada com sucesso! `{gmail_final}`")
                    print(f"  ✓ Concluído!")
                else:
                    update_status(sheets, account["row_index"], "Verificar", page.url)
                    save_screenshot(page, account["name"], "ads_falhou")
                    notify_slack(f"⚠️ *{account['name']}* — Google Ads não concluiu. Verificação manual necessária.\n`{page.url}`")

        except Exception as e:
            print(f"  ✗ Erro: {e}")
            try:
                save_screenshot(page, account["name"], "excecao")
            except:
                pass
            update_status(sheets, account["row_index"], "Erro", str(e))
            notify_slack(f"❌ *{account['name']}* — erro inesperado:\n```{str(e)[:300]}```")
            if tzid:
                try:
                    sms_cancel(tzid)
                except:
                    pass
        finally:
            try:
                browser.close()
            except:
                pass
            if profile_id:
                try:
                    close_profile(profile_id)
                except:
                    pass

        if idx < len(accounts) - 1:
            time.sleep(5)

    print("\nConcluído!")


if __name__ == "__main__":
    import sys
    test_mode = "--all" not in sys.argv
    if test_mode:
        print("Modo teste (1 conta). Use --all para todas.")
    run(test_mode=test_mode)

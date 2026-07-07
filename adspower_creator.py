import os
import re
import time
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

load_dotenv()

SPREADSHEET_ID = "1qIlVRWUXTZcYd0QkfAawensrgsWTiBgj1xEfeWEbpLY"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
ADSPOWER_URL = "http://local.adspower.net:50325"
GROUP_NAME = "IA"
MAX_ACCOUNTS_PER_PROXY = 3

# Column indexes (0-based)
COL_NUM       = 0
COL_NAME      = 1
COL_GMAIL     = 2
COL_PASS      = 3
COL_CNPJ      = 4
COL_RAZAO     = 5
COL_CARD_NAME = 6
COL_CARD_NUM  = 7
COL_CARD_EXP  = 8
COL_CARD_CVV  = 9
COL_STATUS    = 10
COL_OBS       = 11


def get_sheets():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()


def read_accounts(sheets) -> list:
    result = sheets.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Contas!A2:L200"
    ).execute()
    rows = result.get("values", [])
    accounts = []
    for i, row in enumerate(rows):
        row = row + [""] * (12 - len(row))
        gmail = row[COL_GMAIL].strip()
        if not gmail:
            continue
        status = row[COL_STATUS].strip()
        if status in ("Criado", "Erro - ignorar"):
            continue
        accounts.append({
            "row_index": i + 2,
            "name": row[COL_NAME].strip(),
            "gmail": gmail,
            "password": row[COL_PASS].strip(),
            "cnpj": re.sub(r"\D", "", row[COL_CNPJ].strip()),
            "card_name": row[COL_CARD_NAME].strip(),
            "card_number": re.sub(r"\s", "", row[COL_CARD_NUM].strip()),
            "card_exp": row[COL_CARD_EXP].strip(),
            "card_cvv": row[COL_CARD_CVV].strip(),
        })
    return accounts


def update_status(sheets, row_index: int, status: str, obs: str = ""):
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!K{row_index}:L{row_index}",
        valueInputOption="RAW",
        body={"values": [[status, obs]]}
    ).execute()


def update_razao(sheets, row_index: int, razao: str):
    sheets.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Contas!F{row_index}",
        valueInputOption="RAW",
        body={"values": [[razao]]}
    ).execute()


def lookup_cnpj(cnpj: str) -> dict:
    r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", timeout=15)
    r.raise_for_status()
    data = r.json()
    return {
        "razao_social": data.get("razao_social", ""),
        "cep": re.sub(r"\D", "", data.get("cep", "")),
        "logradouro": data.get("logradouro", ""),
        "numero": data.get("numero", ""),
        "complemento": data.get("complemento", ""),
        "bairro": data.get("bairro", ""),
        "municipio": data.get("municipio", ""),
        "uf": data.get("uf", ""),
    }


def build_proxy_pool() -> list:
    """Escaneia todos os perfis do AdsPower e retorna proxies disponíveis (< 3 contas)."""
    proxy_map = {}   # proxy_id -> config
    proxy_usage = {} # proxy_id -> count

    page = 1
    while True:
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
        page += 1
        if page > 30:
            break

    # Retorna proxies com menos de 3 contas, ordenados por menos usados primeiro
    available = [
        (pid, proxy_map[pid], proxy_usage.get(pid, 0))
        for pid in proxy_map
        if proxy_usage.get(pid, 0) < MAX_ACCOUNTS_PER_PROXY
    ]
    available.sort(key=lambda x: (x[2], -int(x[0])))
    print(f"  Pool: {len(proxy_map)} proxies encontrados, {len(available)} disponíveis")
    return available


def get_or_create_group() -> str:
    r = requests.get(f"{ADSPOWER_URL}/api/v1/group/list", params={"page": 1, "page_size": 100})
    data = r.json()
    if data.get("code") == 0:
        for g in data["data"].get("list", []):
            if g["group_name"] == GROUP_NAME:
                print(f"  Grupo '{GROUP_NAME}' encontrado (id={g['group_id']})")
                return str(g["group_id"])

    r2 = requests.post(f"{ADSPOWER_URL}/api/v1/group/create", json={"group_name": GROUP_NAME})
    data2 = r2.json()
    if data2.get("code") == 0:
        group_id = str(data2["data"]["group_id"])
        print(f"  Grupo '{GROUP_NAME}' criado (id={group_id})")
        return group_id
    raise Exception(f"Falha ao criar grupo: {data2}")


def find_existing_profile(gmail: str) -> str | None:
    """Retorna o profile_id se já existe um perfil com esse Gmail."""
    for page in range(1, 10):
        r = requests.get(f"{ADSPOWER_URL}/api/v1/user/list?page={page}&page_size=100")
        d = r.json()
        if d.get("code") != 0:
            break
        users = d["data"].get("list", [])
        if not users:
            break
        for u in users:
            if u.get("username", "").lower() == gmail.lower():
                return u["user_id"]
    return None


def create_profile(account: dict, group_id: str, proxy_config: dict) -> str:
    # Verifica se já existe perfil com esse Gmail
    existing = find_existing_profile(account["gmail"])
    if existing:
        print(f"  Perfil já existe: {existing} — reutilizando.")
        return existing

    payload = {
        "name": account["name"] or account["gmail"],
        "group_id": group_id,
        "domain_name": "accounts.google.com",
        "username": account["gmail"],
        "password": account["password"],
        "user_proxy_config": proxy_config,
        "fingerprint_config": {
            "os": "Windows",
        },
    }
    r = requests.post(f"{ADSPOWER_URL}/api/v1/user/create", json=payload)
    data = r.json()
    if data.get("code") == 0:
        profile_id = data["data"]["id"]
        print(f"  Perfil criado: {profile_id}")
        return profile_id
    raise Exception(f"Falha ao criar perfil: {data}")


def open_profile(profile_id: str) -> dict:
    r = requests.get(f"{ADSPOWER_URL}/api/v1/browser/start", params={"user_id": profile_id})
    data = r.json()
    if data.get("code") == 0:
        ws = data["data"]["ws"]["puppeteer"]
        port = data["data"]["debug_port"]
        print(f"  Browser aberto (port={port})")
        return {"ws": ws, "port": port}
    raise Exception(f"Falha ao abrir perfil: {data}")


def close_profile(profile_id: str):
    requests.get(f"{ADSPOWER_URL}/api/v1/browser/stop", params={"user_id": profile_id})
    print(f"  Browser fechado.")


def create_google_ads_account(ws_endpoint: str, account: dict, cnpj_data: dict):
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_endpoint)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        print("  Acessando Google Ads...")
        page.goto("https://ads.google.com/intl/pt-BR_br/home/", timeout=30000)
        time.sleep(3)

        # Clicar em "Começar agora" ou similar
        for btn in ["Começar agora", "Criar conta", "Get started"]:
            try:
                page.click(f"text={btn}", timeout=3000)
                time.sleep(2)
                break
            except:
                pass

        # Login se necessário
        if "accounts.google.com" in page.url or "signin" in page.url.lower():
            print("  Fazendo login...")
            page.fill("input[type='email']", account["gmail"])
            page.click("#identifierNext")
            time.sleep(2)
            page.fill("input[type='password']", account["password"])
            page.click("#passwordNext")
            time.sleep(4)

        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        # Pular objetivo inicial
        for skip in ["Pular configuração", "Criar uma conta sem uma campanha", "Pular"]:
            try:
                page.click(f"text={skip}", timeout=3000)
                time.sleep(2)
                break
            except:
                pass

        # Nome da empresa
        print("  Preenchendo dados da empresa...")
        for selector in [
            "input[name='businessName']",
            "input[placeholder*='empresa']",
            "input[placeholder*='negócio']",
            "input[aria-label*='empresa']",
        ]:
            try:
                page.fill(selector, cnpj_data["razao_social"], timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        for btn in ["Continuar", "Próxima", "Avançar", "Next"]:
            try:
                page.click(f"button:has-text('{btn}')", timeout=3000)
                time.sleep(2)
                break
            except:
                pass

        page.wait_for_load_state("networkidle", timeout=10000)
        time.sleep(2)

        print("  Preenchendo billing...")

        # País
        for sel in ["select[name*='country']", "select[aria-label*='país']", "select[aria-label*='country']"]:
            try:
                page.select_option(sel, "BR", timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # Fuso horário
        try:
            page.select_option("select[name*='timezone']", "America/Sao_Paulo", timeout=3000)
            time.sleep(1)
        except:
            pass

        # Tipo conta - Empresa
        for sel in ["input[value='BUSINESS']", "label:has-text('Empresa')", "label:has-text('Business')"]:
            try:
                page.click(sel, timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # Razão social
        for sel in ["input[name*='businessName']", "input[name*='company']", "input[name*='legalName']"]:
            try:
                page.fill(sel, cnpj_data["razao_social"], timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # Endereço
        address = f"{cnpj_data['logradouro']} {cnpj_data['numero']}".strip()
        for sel in ["input[name*='address']", "input[placeholder*='endereço']", "input[name*='streetAddress']"]:
            try:
                page.fill(sel, address, timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # Cidade
        for sel in ["input[name*='city']", "input[placeholder*='cidade']", "input[name*='locality']"]:
            try:
                page.fill(sel, cnpj_data["municipio"], timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # CEP
        for sel in ["input[name*='zip']", "input[name*='postal']", "input[placeholder*='CEP']"]:
            try:
                page.fill(sel, cnpj_data["cep"], timeout=3000)
                time.sleep(1)
                break
            except:
                pass

        # Cartão
        print("  Preenchendo cartão...")
        try:
            card_frame = None
            for frame in page.frames:
                if "card" in frame.url.lower() or "pay" in frame.url.lower():
                    card_frame = frame
                    break

            target = card_frame or page
            for sel in ["input[name*='cardnumber']", "input[name*='card_number']", "input[name*='cardNumber']"]:
                try:
                    target.fill(sel, account["card_number"], timeout=3000)
                    break
                except:
                    pass
            for sel in ["input[name*='exp']", "input[name*='expiry']", "input[name*='expDate']"]:
                try:
                    target.fill(sel, account["card_exp"], timeout=3000)
                    break
                except:
                    pass
            for sel in ["input[name*='cvc']", "input[name*='cvv']", "input[name*='security']"]:
                try:
                    target.fill(sel, account["card_cvv"], timeout=3000)
                    break
                except:
                    pass
            for sel in ["input[name*='name']", "input[name*='cardName']", "input[name*='cardHolder']"]:
                try:
                    target.fill(sel, account["card_name"], timeout=3000)
                    break
                except:
                    pass
        except Exception as e:
            print(f"  Aviso cartão: {e}")

        # Submeter
        for btn in ["Enviar", "Concluir", "Salvar", "Submit", "Próxima"]:
            try:
                page.click(f"button:has-text('{btn}')", timeout=3000)
                time.sleep(3)
                break
            except:
                pass

        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        final_url = page.url
        print(f"  URL final: {final_url}")
        browser.close()

        if "google.com/aw" in final_url or "ads.google.com" in final_url:
            return True, final_url
        return False, final_url


def run(test_mode: bool = True):
    sheets = get_sheets()
    accounts = read_accounts(sheets)

    if not accounts:
        print("Nenhuma conta pendente na planilha.")
        return

    if test_mode:
        accounts = accounts[:1]
        print(f"[MODO TESTE] Processando apenas 1 conta.")

    print(f"\nContas a processar: {len(accounts)}")

    group_id = get_or_create_group()

    print("\nMapeando proxies do AdsPower...")
    proxy_pool = build_proxy_pool()
    if not proxy_pool:
        raise Exception("Nenhum proxy disponível no AdsPower (todos com 3+ contas).")

    proxy_index = 0

    for idx, account in enumerate(accounts):
        print(f"\n[{idx+1}/{len(accounts)}] {account['name']} ({account['gmail']})")
        profile_id = None

        try:
            # Consultar CNPJ
            print(f"  Consultando CNPJ {account['cnpj']}...")
            cnpj_data = lookup_cnpj(account["cnpj"])
            print(f"  Razão Social: {cnpj_data['razao_social']}")
            update_razao(sheets, account["row_index"], cnpj_data["razao_social"])

            # Selecionar proxy do pool (avança a cada 3 contas)
            proxy_index = idx // MAX_ACCOUNTS_PER_PROXY
            if proxy_index >= len(proxy_pool):
                raise Exception("Proxies esgotados no pool.")
            pid, proxy_config, usage = proxy_pool[proxy_index]
            print(f"  Proxy ID {pid}: {proxy_config['proxy_host']}:{proxy_config['proxy_port']} ({usage}/3)")

            # Criar perfil
            update_status(sheets, account["row_index"], "Criando perfil...")
            profile_id = create_profile(account, group_id, proxy_config)

            # Abrir browser
            update_status(sheets, account["row_index"], "Abrindo browser...")
            browser_info = open_profile(profile_id)
            time.sleep(3)

            # Criar conta Google Ads
            update_status(sheets, account["row_index"], "Criando conta Ads...")
            success, final_url = create_google_ads_account(browser_info["ws"], account, cnpj_data)

            if success:
                print(f"  ✓ Conta criada com sucesso!")
                update_status(sheets, account["row_index"], "Criado", final_url)
            else:
                print(f"  ⚠ Verificar manualmente: {final_url}")
                update_status(sheets, account["row_index"], "Verificar", final_url)

        except Exception as e:
            print(f"  ✗ Erro: {e}")
            update_status(sheets, account["row_index"], "Erro", str(e))
        finally:
            if profile_id:
                try:
                    close_profile(profile_id)
                except:
                    pass

        if idx < len(accounts) - 1:
            print("  Aguardando 5s...")
            time.sleep(5)

    print("\nConcluído!")


if __name__ == "__main__":
    import sys
    test_mode = "--all" not in sys.argv
    if test_mode:
        print("Modo teste (1 conta). Use --all para processar todas.")
    run(test_mode=test_mode)

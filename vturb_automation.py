"""
Vturb automation — duplica vídeos template para uma nova conta BrainMary.

Uso:
    python vturb_automation.py --list
    python vturb_automation.py --setup-lc LC160
    python vturb_automation.py --duplicate <video_id> [--name "Novo Nome"]

Requer playwright: pip install playwright && playwright install chromium
"""

import argparse
import json
import sys
import requests as _requests

EMAIL = "fgdigital7d@gmail.com"
PASSWORD = "Vturbforte@091"
API_BASE = "https://api.vturb.com"

_HTTP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://app.vturb.com",
    "Referer": "https://app.vturb.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _login_http():
    """Tenta login direto via HTTP (sem browser). Retorna (session, jwt_token) ou lança exceção."""
    session = _requests.Session()
    resp = session.post(
        f"{API_BASE}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        headers=_HTTP_HEADERS,
        timeout=30,
    )
    if resp.status_code in (403, 503):
        raise RuntimeError(f"Cloudflare bloqueou login HTTP ({resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token") or data.get("jwt")
    if not token:
        raise RuntimeError(f"Login HTTP OK mas token não encontrado: {list(data.keys())}")
    return session, token


def _api_http(session, method, path, jwt_token, body=None, timeout=120):
    """Chamada à API Vturb via requests (sem browser)."""
    headers = {**_HTTP_HEADERS, "Authorization": f"Bearer {jwt_token}"}
    url = f"{API_BASE}{path}"
    m = method.upper()
    if m == "GET":
        resp = session.get(url, headers=headers, timeout=timeout)
    elif m == "POST":
        resp = session.post(url, headers=headers, json=body or {}, timeout=timeout)
    elif m == "PUT":
        resp = session.put(url, headers=headers, json=body or {}, timeout=timeout)
    elif m == "PATCH":
        resp = session.patch(url, headers=headers, json=body or {}, timeout=timeout)
    else:
        raise ValueError(f"Método não suportado: {method}")

    class _Compat:
        """Adapta requests.Response para a interface usada pelo restante do código."""
        def __init__(self, r):
            self._r = r
            self.ok = r.ok
            self.status = r.status_code
        def text(self):
            return self._r.text
        def json(self):
            return self._r.json()

    return _Compat(resp)

import os as _os
_OFFER_CONFIGS_FILE = _os.path.join(_os.path.dirname(__file__), "offer_configs.json")


def _load_offer_config(oferta: str) -> dict:
    with open(_OFFER_CONFIGS_FILE, encoding="utf-8") as f:
        configs = json.load(f)
    if oferta not in configs:
        raise ValueError(f"Oferta '{oferta}' não encontrada em offer_configs.json.")
    return configs[oferta]


def _launch_and_login():
    """Lança browser headed, faz login e retorna (playwright, browser, context, page, jwt)."""
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = context.new_page()

    jwt_token = None

    def capture_login(response):
        nonlocal jwt_token
        if jwt_token:
            return
        if ("auth/login" in response.url or "auth/session" in response.url or "api/auth" in response.url) and response.status == 200:
            try:
                data = response.json()
                jwt_token = data.get("token") or data.get("access_token") or data.get("jwt")
            except Exception:
                pass

    page.on("response", capture_login)

    print("[vturb] Abrindo Vturb...", flush=True)
    page.goto("https://app.vturb.com/login", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    page.wait_for_timeout(1500)

    page.fill("input[name=email]", EMAIL)
    page.wait_for_timeout(300)
    page.fill("input[name=password]", PASSWORD)
    page.wait_for_timeout(500)
    page.click("button[type=submit]")

    print("[vturb] Aguardando login...", flush=True)
    deadline = 20000
    step = 500
    elapsed = 0
    while not jwt_token and elapsed < deadline:
        page.wait_for_timeout(step)
        elapsed += step
        if "/login" not in page.url:
            page.wait_for_timeout(1500)
            break

    # Fallback: buscar token no localStorage se não capturado via intercept
    if not jwt_token and "/login" not in page.url:
        ls_keys = ["token", "access_token", "jwt", "auth_token", "vturb_token", "userToken"]
        for key in ls_keys:
            try:
                val = page.evaluate(f"localStorage.getItem('{key}')")
                if val and len(val) > 20:
                    jwt_token = val
                    print(f"[vturb] Token obtido via localStorage['{key}'].", flush=True)
                    break
            except Exception:
                pass

    if not jwt_token:
        raise RuntimeError(
            f"Login falhou — token JWT não capturado. URL atual: {page.url}"
        )

    print(f"[vturb] Login OK.", flush=True)
    return playwright, browser, context, page, jwt_token


def _api(context, method, path, jwt_token, body=None, timeout=120_000):
    """Chamada à API usando contexto do browser (cookies do Cloudflare incluídos)."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
        "Origin": "https://app.vturb.com",
        "Referer": "https://app.vturb.com/",
    }
    url = f"{API_BASE}{path}"
    m = method.upper()
    data = json.dumps(body or {}) if body is not None else None
    if m == "GET":
        return context.request.get(url, headers=headers, timeout=timeout)
    elif m == "POST":
        return context.request.post(url, headers=headers, data=json.dumps(body or {}), timeout=timeout)
    elif m == "PUT":
        return context.request.put(url, headers=headers, data=data, timeout=timeout)
    elif m == "PATCH":
        return context.request.patch(url, headers=headers, data=data, timeout=timeout)
    raise ValueError(f"Método não suportado: {method}")


def list_players(context, jwt_token):
    resp = _api(context, "GET", "/vturb/v3/players", jwt_token)
    if not resp.ok:
        raise RuntimeError(f"Erro ao listar players: {resp.status} {resp.text()}")
    return resp.json().get("players", [])


def duplicate_player(context_or_session, jwt_token, player_id, new_name, folder_id=None):
    body = {"name": new_name}
    if folder_id:
        body["folder_id"] = folder_id
    if isinstance(context_or_session, _requests.Session):
        resp = _api_http(context_or_session, "POST", f"/vturb/v2/players/{player_id}/duplicate", jwt_token, body=body)
    else:
        resp = _api(context_or_session, "POST", f"/vturb/v2/players/{player_id}/duplicate", jwt_token, body=body)
    if not resp.ok:
        raise RuntimeError(
            f"Erro ao duplicar {player_id}: {resp.status} {resp.text()[:200]}"
        )
    data = resp.json()
    return data.get("player", data)


def get_player(context, jwt_token, player_id: str) -> dict:
    """Busca dados completos de um player para inspecionar estrutura."""
    for path in [f"/vturb/v3/players/{player_id}", f"/vturb/v2/players/{player_id}"]:
        resp = _api(context, "GET", path, jwt_token)
        if resp.ok:
            data = resp.json()
            return data.get("player", data)
    return {}


def add_security_domain(page, context, jwt_token, player_id: str, domain: str):
    """
    Adiciona domínio em Segurança da conta Vturb (app.vturb.com/account/settings).
    A segurança é global da conta, não por player.
    """
    print(f"[vturb]   Verificando segurança da conta para '{domain}'...", flush=True)
    try:
        page.goto(
            "https://app.vturb.com/account/settings",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(2000)

        # Clicar em Segurança no menu lateral
        page.locator("text=Segurança").first.click()
        page.wait_for_timeout(1500)

        # Verificar se o domínio já está cadastrado
        if domain in page.inner_text("body"):
            print(f"[vturb]   ✓ Domínio '{domain}' já cadastrado na segurança.", flush=True)
            return

        # Clicar em "+ Adicionar Domínio"
        page.locator("text=Adicionar Domínio").click()
        page.wait_for_timeout(600)

        # Preencher o último input vazio que apareceu
        domain_input = None
        for inp in reversed(page.locator("input").all()):
            try:
                if inp.is_visible(timeout=500) and inp.input_value() == "":
                    domain_input = inp
                    break
            except Exception:
                continue

        if not domain_input:
            print(f"[vturb]   [AVISO] Campo de domínio não encontrado após clicar Adicionar.", flush=True)
            return

        domain_input.fill(domain)
        page.wait_for_timeout(300)

        # Salvar
        page.locator("text=Salvar alterações").click()
        page.wait_for_timeout(2000)

        if domain in page.inner_text("body"):
            print(f"[vturb]   ✓ Domínio '{domain}' confirmado na segurança.", flush=True)
        else:
            print(f"[vturb]   [AVISO] Domínio não encontrado após salvar — verifique manualmente.", flush=True)
    except Exception as e:
        print(f"[vturb]   [AVISO] Erro ao adicionar segurança: {e}", flush=True)


# ---------------------------------------------------------------------------
# Comandos públicos
# ---------------------------------------------------------------------------

def run_setup_lc(lc: str, dominio: str = None, oferta: str = "BrainMary") -> dict:
    """
    Duplica os templates da oferta para o LC fornecido.
    Ordem de tentativa:
      1. Webhook local (Mac com Playwright) via VTURB_WEBHOOK_URL
      2. Login HTTP direto (sem browser)
      3. Playwright local
    Retorna {slug: novo_player_id}.
    """
    # 1. Webhook local
    webhook_url   = _os.getenv("VTURB_WEBHOOK_URL", "").rstrip("/")
    webhook_token = _os.getenv("VTURB_WEBHOOK_TOKEN", "vturb-secret-2026")
    if webhook_url:
        print(f"[vturb] Chamando webhook local: {webhook_url}/vturb/setup", flush=True)
        try:
            resp = _requests.post(
                f"{webhook_url}/vturb/setup",
                json={"lc": lc, "oferta": oferta, "dominio": dominio},
                headers={"X-Token": webhook_token, "Content-Type": "application/json"},
                timeout=300,
            )
            if resp.ok:
                player_ids = resp.json().get("player_ids", {})
                print(f"[vturb] ✓ Webhook OK: {player_ids}", flush=True)
                return player_ids
            print(f"[vturb] Webhook retornou {resp.status_code}: {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"[vturb] Webhook indisponível ({e}) — tentando método direto...", flush=True)

    config          = _load_offer_config(oferta)
    templates       = config["vturb_templates"]
    video_names     = config["vturb_video_names"]
    folder_id       = config["vturb_folder_id"]
    folder_overrides = config.get("vturb_folder_overrides", {})

    # Tenta HTTP direto primeiro
    use_http = False
    http_session = None
    ctx = None
    pw = browser = page = None
    token = None

    try:
        print("[vturb] Tentando login via HTTP...", flush=True)
        http_session, token = _login_http()
        use_http = True
        print("[vturb] Login HTTP OK.", flush=True)
    except RuntimeError as cf_err:
        if "Cloudflare" in str(cf_err):
            raise RuntimeError(
                "VTURB_CLOUDFLARE: Cloudflare bloqueou o acesso ao Vturb neste ambiente. "
                "Duplique os players manualmente no painel do Vturb e informe os IDs."
            ) from cf_err
        print(f"[vturb] HTTP falhou ({cf_err}), tentando Playwright...", flush=True)
        try:
            from playwright.sync_api import sync_playwright as _sp
            pw, browser, ctx, page, token = _launch_and_login()
        except Exception as e2:
            raise RuntimeError(f"Playwright também falhou: {e2}") from cf_err

    def _call(method, path, body=None):
        if use_http:
            return _api_http(http_session, method, path, token, body)
        return _api(ctx, method, path, token, body)

    try:
        result = {}
        for page_slug, template_id in templates.items():
            slug_folder = folder_overrides.get(page_slug, folder_id)
            name = video_names[page_slug].format(lc=lc)
            print(f"[vturb] Duplicando {page_slug}: {name}", flush=True)
            new_player = duplicate_player(http_session if use_http else ctx, token, template_id, name, folder_id=slug_folder)
            new_id = new_player.get("id")
            if not new_id:
                raise RuntimeError(f"Duplicação de {page_slug} não retornou ID. Resposta: {new_player}")
            result[page_slug] = new_id
            print(f"[vturb]   → ID: {new_id}", flush=True)

            patch_resp = _call("PATCH", f"/vturb/v2/players/{new_id}",
                               body={"name": name, "folder_id": slug_folder, "secure": False})
            if patch_resp.ok:
                print(f"[vturb]   ✓ Renomeado, movido para pasta {oferta} e segurança desativada.", flush=True)
            else:
                print(f"[vturb]   [AVISO] PATCH falhou ({patch_resp.status}): {patch_resp.text()[:150]}", flush=True)

        if dominio and not use_http and page:
            add_security_domain(page, ctx, token, None, dominio)

        return result
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()


def run_list():
    pw, browser, ctx, page, token = _launch_and_login()
    try:
        players = list_players(ctx, token)
        print(f"\n{'ID':<30} {'Nome'}")
        print("-" * 90)
        for p in players:
            print(f"{p['id']:<30} {p.get('name', '(sem nome)')}")
        print(f"\nTotal: {len(players)} vídeos")
    finally:
        browser.close()
        pw.stop()


def run_duplicate_cmd(player_id, new_name):
    pw, browser, ctx, page, token = _launch_and_login()
    try:
        players = list_players(ctx, token)
        original = next((p for p in players if p["id"] == player_id), None)
        if not original:
            print(f"ERRO: Player {player_id} não encontrado.", file=sys.stderr)
            sys.exit(1)
        print(f"[vturb] Duplicando: {original['name']}")
        new_player = duplicate_player(ctx, token, player_id, new_name or original["name"] + " (cópia)")
        new_id = new_player.get("id", "???")
        print(f"[vturb] Novo ID   : {new_id}")
        print(f"[vturb] Novo nome : {new_player.get('name', '???')}")
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vturb automation — BrainMary")
    parser.add_argument("--list", action="store_true", help="Listar todos os vídeos")
    parser.add_argument("--setup-lc", metavar="LC", help="Criar vídeos para novo LC (ex: LC160)")
    parser.add_argument("--duplicate", metavar="ID", help="Duplicar um vídeo pelo ID")
    parser.add_argument("--name", metavar="NOME", help="Nome para o vídeo duplicado")
    args = parser.parse_args()

    if args.list:
        run_list()
    elif args.setup_lc:
        ids = run_setup_lc(args.setup_lc)
        print(f"\nVídeos criados para {args.setup_lc}:")
        for slug, vid_id in ids.items():
            print(f"  {slug:<10} → {vid_id}")
    elif args.duplicate:
        run_duplicate_cmd(args.duplicate, args.name)
    else:
        parser.print_help()

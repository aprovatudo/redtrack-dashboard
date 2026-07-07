"""
auth_fegsys.py — Conecta contas Google Ads ao FEG Tracker (data lake).

Modos de uso:
  python auth_fegsys.py --auto          # totalmente automático (abre/fecha perfis via AdsPower API)
  python auth_fegsys.py --auto --dry-run  # simula — mostra quais perfis seriam abertos
  python auth_fegsys.py                 # modo manual (você abre o perfil, script faz o resto)
  python auth_fegsys.py --force         # reprocessa contas já conectadas
"""

import argparse
import json
import os
import re
import time

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

FEGSYS_URL           = "https://tracker.fegsys.com/google-ads-token"
ACCESS_CODE          = os.getenv("FEGSYS_ACCESS_CODE")
ADSPOWER_AUTH_EXT_ID = "chcmmdbpbocmnmbhpbjchdgjjhbnfige"
ADSPOWER_API         = "http://local.adspower.net:50325"

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
STATUSES_FILE = os.path.join(os.path.dirname(__file__), "account_statuses.json")
DONE_FILE     = os.path.join(os.path.dirname(__file__), "fegsys_done.json")


# ── Progresso ─────────────────────────────────────────────────────────────────

def load_done() -> set:
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def mark_done(account_id: str):
    done = load_done()
    done.add(account_id)
    with open(DONE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, indent=2)


def load_queue(force: bool = False) -> list[tuple[str, str]]:
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"Arquivo não encontrado: {ACCOUNTS_FILE}")
        return []

    enabled = None
    if os.path.exists(STATUSES_FILE):
        with open(STATUSES_FILE, encoding="utf-8") as f:
            statuses = json.load(f)
        enabled = {k for k, v in statuses.items() if v.get("status") == "ENABLED"}
        print(f"  Filtro ativo: {len(enabled)} contas ENABLED no Google Ads")

    done  = load_done() if not force else set()
    queue = []
    seen  = set()
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("✅"):
                continue
            line   = line[1:].strip()
            parts  = line.split("|", 1)
            acc_id = parts[0].strip()
            nome   = parts[1].strip() if len(parts) > 1 else acc_id
            if acc_id in seen or acc_id in done:
                continue
            if enabled is not None and acc_id not in enabled:
                continue
            seen.add(acc_id)
            queue.append((acc_id, nome))
    return queue


# ── AdsPower API ───────────────────────────────────────────────────────────────

def list_all_profiles() -> list[dict]:
    """Lista todos os perfis do AdsPower (paginado, com retry em rate-limit)."""
    profiles = []
    page = 1
    while True:
        retries = 3
        data = None
        while retries > 0:
            try:
                r = requests.get(
                    f"{ADSPOWER_API}/api/v1/user/list",
                    params={"page": page, "page_size": 100},
                    timeout=15,
                )
                data = r.json()
            except Exception as e:
                print(f"  Erro na página {page}: {e}")
                break
            if data.get("code") == 0:
                break
            if "Too many request" in data.get("msg", ""):
                retries -= 1
                time.sleep(2)
            else:
                print(f"  AdsPower API erro: {data.get('msg')}")
                retries = 0
        if not data or data.get("code") != 0:
            break
        batch = data.get("data", {}).get("list", [])
        if not batch:
            break
        profiles.extend(batch)
        print(f"  {len(profiles)} perfis carregados...", end="\r")
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.5)  # evita rate-limit entre páginas
    print()
    return profiles


def extract_identifier(text: str) -> str | None:
    """
    Extrai o identificador único da conta para exibição.
    M1952 → "M1952" | "FARM LC130-ALKALEAN" → "LC130"
    """
    m = re.search(r'\b(M\d{3,5})\b', text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r'\b([A-Z]{2,3}\d{2,4})\b', text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def find_profile(profiles: list[dict], text: str) -> dict | None:
    """
    Retorna o perfil AdsPower correspondente ao nome/ID da conta.

    Lógica de matching para contas "M[num]" (ex: M1952, M2031):
      1. serial_number == número  (cobre perfis sem M no nome, ex: "Farm GS - 304")
      2. "M{número}" no nome do perfil  (cobre múltiplos perfis por máquina, ex: "M2031 - FARM - LC203")
         Quando há mais de um perfil com o mesmo M (ex: M2031 com LC200-LC203),
         refina pelo identificador LC/AL do próprio `text` se disponível.

    Para contas "LC/AL/PL[num]" (ex: LC127): busca pelo nome do perfil (word boundary).
    """
    m_match = re.search(r'\bM(\d{3,5})\b', text, re.IGNORECASE)
    if m_match:
        number = m_match.group(1)

        # 1. serial_number exato (cobre "Farm GS - 304" sem M no nome)
        for profile in profiles:
            if str(profile.get("serial_number", "")).strip() == number:
                return profile

        # 2. M{número} no nome do perfil
        m_pattern = re.compile(r'\bM' + re.escape(number) + r'\b', re.IGNORECASE)
        candidates = [p for p in profiles if m_pattern.search(p.get("name", ""))]

        if len(candidates) == 1:
            return candidates[0]

        if len(candidates) > 1:
            # Refina: tenta casar também o identificador LC/AL/PL do `text`
            sub = re.search(r'\b([A-Z]{2,3}\d{2,4})\b', text, re.IGNORECASE)
            if sub:
                sub_id  = sub.group(1).upper()
                sub_pat = re.compile(r'\b' + re.escape(sub_id) + r'\b', re.IGNORECASE)
                refined = [p for p in candidates if sub_pat.search(p.get("name", ""))]
                if refined:
                    return refined[0]
            # Sem refinamento possível: retorna o primeiro candidato
            return candidates[0]

        return None

    # LC/AL/PL: busca no nome do perfil
    lc_match = re.search(r'\b([A-Z]{2,3}\d{2,4})\b', text, re.IGNORECASE)
    if lc_match:
        identifier = lc_match.group(1).upper()
        pattern = re.compile(r'\b' + re.escape(identifier) + r'\b', re.IGNORECASE)
        for profile in profiles:
            if pattern.search(profile.get("name", "")):
                return profile

        # Fallback: "AL25" → "FARMAL 25" / "FARMAL25"
        # Contas antigas usam "FARMAL {n}" em vez de "FARM - AL{n}"
        pm = re.match(r'^([A-Z]+)(\d+)$', identifier)
        if pm:
            prefix, number = pm.group(1), pm.group(2)
            alt = re.compile(r'FARM' + re.escape(prefix) + r'\s*0*' + re.escape(number.lstrip("0") or number) + r'\b', re.IGNORECASE)
            for profile in profiles:
                if alt.search(profile.get("name", "")):
                    return profile

    return None


def open_profile(profile_id: str) -> str | None:
    """Abre o perfil no AdsPower e retorna o WebSocket (puppeteer) URL."""
    try:
        r = requests.get(
            f"{ADSPOWER_API}/api/v1/browser/start",
            params={"user_id": profile_id},
            timeout=30,
        )
        data = r.json()
    except Exception as e:
        print(f"    ❌ Erro ao abrir perfil: {e}")
        return None
    if data.get("code") != 0:
        print(f"    ❌ AdsPower recusou abrir perfil: {data.get('msg')}")
        return None
    ws = data.get("data", {}).get("ws", {}).get("puppeteer", "")
    return ws if ws else None


def close_profile(profile_id: str):
    """Fecha o perfil no AdsPower."""
    try:
        r = requests.get(
            f"{ADSPOWER_API}/api/v1/browser/stop",
            params={"user_id": profile_id},
            timeout=10,
        )
        if r.json().get("code") == 0:
            print("    Browser fechado.")
        else:
            print(f"    Aviso ao fechar browser: {r.json().get('msg')}")
    except Exception:
        pass


# ── TOTP via extensão AdsPower ─────────────────────────────────────────────────

def get_totp_from_popup(context, gmail: str) -> str | None:
    for path in ["popup.html", "index.html", "popup/index.html", "options.html"]:
        p = None
        try:
            p = context.new_page()
            p.goto(f"chrome-extension://{ADSPOWER_AUTH_EXT_ID}/{path}", timeout=4000)
            p.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)
            text = p.evaluate("document.body.innerText") or ""
            p.close(); p = None
            if not text.strip():
                continue
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                if gmail.lower() in line.lower():
                    for j in range(i + 1, min(i + 6, len(lines))):
                        m = re.search(r'\b(\d{6})\b', lines[j])
                        if m:
                            return m.group(1)
            codes = re.findall(r'\b\d{6}\b', text)
            if codes:
                return codes[0]
        except Exception:
            if p:
                try: p.close()
                except: pass
    return None


# ── Fluxo FEG OAuth ────────────────────────────────────────────────────────────

def connect_fegsys(ws: str, account_id: str, password: str = "") -> bool | None:
    """
    Executa o fluxo completo do FEG Tracker para uma conta.
    Retorna True (conectado), False (erro) ou None (pulado — 2FA no celular ou senha ausente).
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            context = browser.contexts[0]
            page    = context.new_page()

            # Etapa 1: código de acesso
            page.goto(FEGSYS_URL, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1)

            form_input = page.locator("input[name='code']")
            if form_input.count() > 0:
                form_input.fill(ACCESS_CODE)
                page.locator("button[type='submit']").click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(2)

            # Etapa 2: botão "Conectar conta Google Ads"
            oauth_link = page.locator("a[href='/api/google-ads/oauth/start']")
            if oauth_link.count() == 0:
                print("    ❌ Botão 'Conectar conta Google Ads' não encontrado")
                page.close()
                return False

            oauth_link.first.click()
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)

            # Etapa 3: monitora fluxo OAuth por até 3 minutos
            _totp_done = False
            deadline   = time.time() + 180

            while time.time() < deadline:
                url = page.url

                # Sucesso: voltou para o FEG
                if url.startswith("https://tracker.fegsys.com") and "accounts.google.com" not in url:
                    if "ok=1" in url:
                        try:
                            text = page.evaluate("document.body.innerText") or ""
                        except Exception:
                            text = ""
                        label = account_id
                        if "Conta" in text:
                            try: label = text.split("Conta")[1].split()[0]
                            except Exception: pass
                        print(f"    ✅ Conectado: {label}")
                        page.close()
                        return True
                    elif "error=" in url:
                        error = url.split("error=")[-1][:80]
                        print(f"    ❌ Erro FEG: {error}")
                        page.close()
                        return False
                    page.close()
                    return False

                # Seletor de conta Google
                acct = page.locator("div[data-identifier]")
                if acct.count() > 0:
                    try:
                        label = acct.first.get_attribute("data-identifier") or ""
                        print(f"    Selecionando conta: {label}")
                    except Exception:
                        pass
                    acct.first.click()
                    time.sleep(2)
                    page.wait_for_load_state("domcontentloaded")
                    continue

                # Tela de senha (challenge/pwd ou input[type=password] no Google)
                if "accounts.google.com" in url:
                    pwd_input = page.locator("input[type='password'], input[name='Passwd']")
                    if pwd_input.count() > 0:
                        if password:
                            pwd_input.first.fill(password)
                            time.sleep(0.5)
                            btn = page.locator("button:has-text('Avançar'), button:has-text('Next')")
                            if btn.count() > 0:
                                btn.first.click()
                                print(f"    Senha preenchida")
                                time.sleep(2)
                                page.wait_for_load_state("domcontentloaded")
                        else:
                            print("    ⏸  Senha solicitada mas não disponível no perfil — pulando")
                            page.close()
                            return None
                        continue

                # 2FA TOTP
                if "signin/challenge/totp" in url and not _totp_done:
                    try:
                        text  = page.evaluate("document.body.innerText") or ""
                        m     = re.search(r'[\w.+-]+@[\w.-]+\.\w+', text)
                        gmail = m.group(0) if m else ""
                    except Exception:
                        gmail = ""
                    code = get_totp_from_popup(context, gmail)
                    if code:
                        inp = page.locator(
                            "input[name='totpPin'], input[type='tel'], "
                            "#totpPin, input[autocomplete='one-time-code']"
                        )
                        if inp.count() > 0:
                            inp.first.fill(code)
                            time.sleep(0.5)
                            btn = page.locator("button:has-text('Avançar'), button:has-text('Next')")
                            if btn.count() > 0:
                                btn.first.click()
                                print(f"    2FA: {code}")
                                _totp_done = True
                                time.sleep(2)
                                page.wait_for_load_state("domcontentloaded")
                    else:
                        print("    ⏸  2FA não encontrado na extensão — 2FA está no celular, pulando")
                        page.close()
                        return None
                    continue

                # Tela "app não verificado"
                if "oauth/warning" in url:
                    adv = page.locator("a:has-text('Avançado'), a:has-text('Advanced')")
                    if adv.count() > 0:
                        adv.first.click()
                        time.sleep(1)
                        unsafe = page.locator(
                            "#proceed-link, a:has-text('não seguro'), a:has-text('unsafe')"
                        )
                        if unsafe.count() > 0:
                            unsafe.first.click()
                            print("    Avançado → não seguro")
                            time.sleep(2)
                            page.wait_for_load_state("domcontentloaded")
                    else:
                        for txt in ["Continuar", "Continue"]:
                            btn = page.locator(f"button:has-text('{txt}'), a:has-text('{txt}')")
                            if btn.count() > 0:
                                btn.first.click()
                                time.sleep(2)
                                page.wait_for_load_state("domcontentloaded")
                                break
                    continue

                # Telas de consentimento
                for txt in ["Continuar", "Continue", "Permitir", "Allow"]:
                    btn = page.locator(f"button:has-text('{txt}')")
                    if btn.count() > 0:
                        btn.first.click()
                        time.sleep(2)
                        page.wait_for_load_state("domcontentloaded")
                        break

                time.sleep(2)

            print("    ❌ Tempo esgotado (3 min)")
            page.close()
            return False

    except Exception as e:
        print(f"    ❌ Erro: {e}")
        return False


# ── Modo automático ────────────────────────────────────────────────────────────

def run_auto(force: bool = False, dry_run: bool = False, delay: int = 6):
    """Abre/fecha perfis automaticamente via AdsPower API — sem intervenção manual."""
    queue = load_queue(force=force)
    total = len(queue)

    if not total:
        done = load_done()
        print(f"Todas as contas ✅ já estão conectadas ao FEG ({len(done)} contas).")
        return

    print(f"\nCarregando perfis do AdsPower...")
    profiles = list_all_profiles()
    if not profiles:
        print("❌ Nenhum perfil retornado pelo AdsPower. Verifique se o app está aberto.")
        return
    print(f"{len(profiles)} perfil(is) encontrado(s)")

    # Pré-valida correspondências
    matched   = []
    unmatched = []
    for acc_id, nome in queue:
        profile = find_profile(profiles, nome) or find_profile(profiles, acc_id)
        if profile:
            matched.append((acc_id, nome, profile))
        else:
            identifier = extract_identifier(nome) or extract_identifier(acc_id) or "?"
            unmatched.append((acc_id, nome, identifier))

    print(f"\n{'─'*60}")
    print(f"  FEG Tracker — MODO AUTOMÁTICO")
    print(f"  {len(matched)} conta(s) com perfil encontrado")
    print(f"  {len(unmatched)} conta(s) sem correspondência (serão puladas)")
    if dry_run:
        print(f"\n  [DRY RUN — nenhuma ação será executada]")
    print(f"{'─'*60}\n")

    if unmatched:
        print("Contas sem perfil AdsPower correspondente:")
        for acc_id, nome, ident in unmatched:
            print(f"  {acc_id} | {nome}  (buscou: {ident})")
        print()

    if dry_run:
        print("Correspondências que seriam executadas:")
        for acc_id, nome, profile in matched:
            print(f"  {acc_id} | {nome}  →  perfil: {profile['name']} ({profile['user_id']})")
        print()
        return

    ok = err = skip = 0
    for idx, (acc_id, nome, profile) in enumerate(matched, 1):
        profile_id   = profile["user_id"]
        profile_name = profile["name"]
        print(f"[{idx}/{len(matched)}] {acc_id} | {nome}")
        print(f"    Perfil: {profile_name} ({profile_id})")
        print(f"    Abrindo browser...")

        ws = open_profile(profile_id)
        if not ws:
            err += 1
            print()
            continue

        time.sleep(delay)  # aguarda extensões carregarem

        result = connect_fegsys(ws, acc_id, password=profile.get("password", ""))
        if result is True:
            mark_done(acc_id)
            ok += 1
        elif result is None:
            skip += 1
        else:
            err += 1

        close_profile(profile_id)
        time.sleep(3)
        print()

    print(f"{'─'*60}")
    print(f"  Concluído: {ok} conectados | {skip} 2FA-celular | {len(unmatched)} sem perfil | {err} erros")
    print(f"  Total no data lake: {len(load_done())} conta(s)")
    print(f"{'─'*60}")


# ── Modo manual ────────────────────────────────────────────────────────────────

def get_active_ws() -> str | None:
    """Detecta browser aberto via AdsPower (lsof + /json/version)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"],
            stderr=subprocess.DEVNULL, text=True
        )
        for line in out.splitlines():
            if any(k in line for k in ["SunBrowse", "chrome", "Chromium"]):
                for part in line.split():
                    if ":" in part:
                        port = part.split(":")[-1]
                        if port.isdigit():
                            try:
                                r = requests.get(f"http://localhost:{port}/json/version", timeout=1)
                                ws = r.json().get("webSocketDebuggerUrl", "")
                                if ws:
                                    return ws
                            except Exception:
                                pass
    except Exception:
        pass
    return None


def run_queue(force: bool = False):
    """Modo manual: você abre o perfil, o script executa o OAuth."""
    queue = load_queue(force=force)
    total = len(queue)

    if not total:
        done = load_done()
        print(f"Todas as contas ✅ já estão conectadas ao FEG ({len(done)} contas).")
        return

    print(f"\n{'─'*55}")
    print(f"  FEG Tracker — MODO MANUAL — {total} conta(s)")
    print(f"  Para cada conta: abra o perfil no AdsPower → pressione Enter")
    print(f"{'─'*55}\n")

    ok = err = skip = 0
    for idx, (acc_id, nome) in enumerate(queue, 1):
        print(f"[{idx}/{total}] {acc_id} | {nome}")
        resp = input("  Abriu o perfil? (Enter=continuar / s=pular / q=sair): ").strip().lower()

        if resp == "q":
            print("Interrompido.")
            break
        if resp == "s":
            skip += 1
            print()
            continue

        ws = get_active_ws()
        if not ws:
            print("  ❌ Nenhum browser encontrado. Abra o perfil e tente novamente.\n")
            err += 1
            continue

        result = connect_fegsys(ws, acc_id)
        if result is True:
            mark_done(acc_id)
            ok += 1
        elif result is None:
            skip += 1
            print("    (2FA no celular — pulado)")
        else:
            err += 1
        print()

    print(f"{'─'*55}")
    print(f"  Concluído: {ok} conectados | {skip} 2FA-celular | {err} erros")
    print(f"  Total no data lake: {len(load_done())} conta(s)")
    print(f"{'─'*55}")


# ── Debug ─────────────────────────────────────────────────────────────────────

def debug_profiles(search: str = ""):
    """Imprime perfis para diagnóstico. Com --search filtra por texto no nome."""
    profiles = list_all_profiles()
    if not profiles:
        print("Nenhum perfil retornado.")
        return
    print(f"{len(profiles)} perfil(is) carregados.\n")

    if search:
        found = [p for p in profiles if search.lower() in p.get("name", "").lower()]
        print(f"=== Perfis com '{search}' no nome ({len(found)} encontrados) ===")
        for p in found:
            print(f"  serial={p.get('serial_number'):>6}  user_id={p.get('user_id')}  nome={p.get('name')}")
    else:
        print("=== Campos do primeiro perfil ===")
        print(json.dumps(profiles[0], indent=2, ensure_ascii=False))
        print("\n=== Nomes dos 10 primeiros perfis ===")
        for p in profiles[:10]:
            keys = {k: v for k, v in p.items() if v not in (None, "", [], {})}
            print(keys)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Abre/fecha perfis automaticamente via AdsPower API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula o modo --auto: mostra correspondências sem executar",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocessa contas já conectadas",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=6,
        metavar="SEGUNDOS",
        help="Segundos para aguardar após abrir o browser (padrão: 6)",
    )
    parser.add_argument(
        "--debug-profiles",
        action="store_true",
        help="Imprime campos brutos da API do AdsPower e sai",
    )
    parser.add_argument(
        "--search",
        metavar="TEXTO",
        default="",
        help="Busca perfis cujo nome contém TEXTO (usar com --debug-profiles)",
    )
    args = parser.parse_args()

    if args.debug_profiles:
        debug_profiles(search=args.search)
    elif args.auto or args.dry_run:
        run_auto(force=args.force, dry_run=args.dry_run, delay=args.delay)
    else:
        run_queue(force=args.force)

"""
Upload SFTP para Hostinger — usa SSH por plano (cobre todos os domínios sem senha por domínio).
Substitui domínio/IDs diretamente na memória a partir do ZIP.

Uso:
    python ftp_upload.py --dominio mynewdomain.online \
        --campaign-id <id> --vsl01-player <id> --micro01-player <id> \
        [--zip ~/Downloads/brainmary.zip]
"""

import argparse
import json
import os
import random
import re
import zipfile
from io import BytesIO
from pathlib import Path

try:
    import paramiko
except ImportError:
    raise ImportError("Instale paramiko: pip install paramiko")

_DIR            = os.path.dirname(__file__)
_PLANS_FILE     = os.path.join(_DIR, "ftp_plans.json")
_CACHE_FILE     = os.path.join(_DIR, "ftp_domain_cache.json")
_CONFIGS_FILE   = os.path.join(_DIR, "offer_configs.json")
_TEMPLATES_DIR  = os.path.join(_DIR, "templates")
_VARIATIONS_DIR = os.path.join(_DIR, "templates", "variations")

# Fallback para ofertas sem "pages" definido no offer_configs.json
_DEFAULT_PAGE_REPLACEMENTS = {
    "premary":  ["domain", "campaign"],
    "vsl01":    ["domain", "campaign", "vsl01"],
    "micro01":  ["domain", "campaign", "micro01"],
}


def _load_offer_config(oferta: str) -> dict:
    with open(_CONFIGS_FILE, encoding="utf-8") as f:
        configs = json.load(f)
    if oferta not in configs:
        raise ValueError(f"Oferta '{oferta}' não encontrada em offer_configs.json. Disponíveis: {list(configs.keys())}")
    return configs[oferta]


def _template_zip_path(oferta: str) -> str:
    return os.path.join(_TEMPLATES_DIR, f"{oferta}.zip")


def _load_plans() -> dict:
    if os.path.exists(_PLANS_FILE):
        with open(_PLANS_FILE, encoding="utf-8") as f:
            return json.load(f)
    env_json = os.environ.get("FTP_PLANS_JSON")
    if env_json:
        return json.loads(env_json)
    raise FileNotFoundError(
        f"ftp_plans.json não encontrado e FTP_PLANS_JSON não definido. "
        "Configure os secrets do Streamlit com [ftp_plano_a] ... [ftp_plano_d]."
    )


def _load_cache() -> dict:
    if os.path.exists(_CACHE_FILE):
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict):
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _try_sftp(host: str, port: int, user: str, passwd: str):
    """Tenta conectar via SFTP. Retorna (ssh, sftp) ou (None, None)."""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=port, username=user, password=passwd, timeout=15)
        sftp = ssh.open_sftp()
        return ssh, sftp
    except Exception as e:
        print(f"         erro: {type(e).__name__}: {e}")
        return None, None


def _find_domain_root(sftp, dominio: str) -> str | None:
    """Descobre o diretório public_html do domínio no servidor."""
    # Hostinger Cloud: /home/{user}/domains/{domain}/public_html
    # Fallback: /domains/{domain}/public_html
    try:
        home = sftp.getcwd() or "/"
    except Exception:
        home = "/"

    candidates = [
        f"{home}/domains/{dominio}/public_html",
        f"/home/u854618711/domains/{dominio}/public_html",  # substituído pelo account_id real abaixo
        f"/domains/{dominio}/public_html",
        f"/public_html",
    ]
    for path in candidates:
        try:
            sftp.stat(path)
            return path
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


def _find_domain_root_for_plan(sftp, account_id: str, dominio: str) -> str | None:
    """Descobre o diretório public_html usando o account_id do plano."""
    try:
        home = sftp.getcwd() or f"/home/{account_id}"
    except Exception:
        home = f"/home/{account_id}"

    candidates = [
        f"{home}/domains/{dominio}/public_html",
        f"/home/{account_id}/domains/{dominio}/public_html",
        f"/domains/{dominio}/public_html",
        f"{home}/public_html",
    ]
    for path in candidates:
        try:
            sftp.stat(path)
            return path
        except Exception:
            continue
    return None


def detect_plan(dominio: str):
    """
    Detecta automaticamente em qual plano o domínio está via SFTP/SSH.
    Credenciais são por plano — cobre todos os domínios sem configuração adicional.
    Retorna (nome_plano, config_com_root, ssh, sftp).
    """
    plans  = _load_plans()
    cache  = _load_cache()

    # Verificar cache primeiro
    if dominio in cache:
        saved = cache[dominio]
        plano_name = saved.get("plano")
        root       = saved.get("root")
        if plano_name in plans and root:
            plan = plans[plano_name]
            print(f"[sftp] Cache: {plano_name} / root={root}")
            ssh, sftp = _try_sftp(plan["host"], plan["ssh_port"], plan["account_id"], plan["password"])
            if sftp:
                return plano_name, {**plan, "root": root}, ssh, sftp
            print("[sftp] Cache inválido, re-detectando...")
            del cache[dominio]

    print(f"[sftp] Detectando plano para '{dominio}'...")
    for plano_name, plan in plans.items():
        print(f"[sftp]   Testando {plano_name} ({plan['host']} porta {plan['ssh_port']})...")
        ssh, sftp = _try_sftp(plan["host"], plan["ssh_port"], plan["account_id"], plan["password"])
        if not sftp:
            continue

        root = _find_domain_root_for_plan(sftp, plan["account_id"], dominio)
        if root is None:
            print(f"[sftp]   Conectou em {plano_name} mas diretório do domínio não encontrado.")
            sftp.close(); ssh.close()
            continue

        print(f"[sftp]   ✓ {plano_name} / root={root} — salvando no cache.")
        cache[dominio] = {"plano": plano_name, "root": root}
        _save_cache(cache)
        return plano_name, {**plan, "root": root}, ssh, sftp

    raise ConnectionError(
        f"Domínio '{dominio}' não encontrado em nenhum plano.\n"
        "Verifique se o domínio está adicionado na Hostinger e se a senha SSH está definida nos 3 planos."
    )


def _ensure_dir(sftp, path: str):
    parts = path.strip("/").split("/")
    current = ""
    for part in parts:
        current = f"/{current}/{part}".replace("//", "/")
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
            except Exception:
                pass


def _build_replacements(dominio: str, campaign_id: str, player_ids: dict,
                        page: str, config: dict) -> dict:
    page_keys = config.get("pages", _DEFAULT_PAGE_REPLACEMENTS).get(page, [])
    base = {
        "domain":   (config["template_domain"],   f"fg.{dominio}"),
        "campaign": (config["template_campaign"],  campaign_id),
    }
    for slug, new_id in player_ids.items():
        tpl_key = f"template_{slug}_player"
        if tpl_key in config:
            base[slug] = (config[tpl_key], new_id)
    return {base[k][0]: base[k][1] for k in page_keys if k in base and base[k][1]}


def _get_variations(oferta: str, page: str) -> dict:
    """Carrega headlines e imagens alternativas para uma oferta/página."""
    base = os.path.join(_VARIATIONS_DIR, oferta, page)
    result = {}

    headlines_path = os.path.join(base, "headlines.json")
    if os.path.exists(headlines_path):
        with open(headlines_path, encoding="utf-8") as f:
            headlines = json.load(f)
        if headlines:
            result["headlines"] = headlines

    images_dir = os.path.join(base, "images")
    if os.path.isdir(images_dir):
        _img_exts = {".jpg", ".jpeg", ".webp", ".png"}
        images = [
            os.path.join(images_dir, fn)
            for fn in os.listdir(images_dir)
            if not fn.startswith(".") and Path(fn).suffix.lower() in _img_exts
        ]
        if images:
            result["images"] = images

    return result


def _apply_headline_variation(html_text: str, headlines: list, rng) -> str:
    chosen = rng.choice(headlines)
    new_html = re.sub(
        r'(<h1\b[^>]*>)\s*.*?\s*(</h1>)',
        lambda m: m.group(1) + "\n" + chosen + "\n" + m.group(2),
        html_text,
        count=1,
        flags=re.DOTALL,
    )
    if new_html != html_text:
        print(f"    [variação] Headline: {chosen[:70]}{'...' if len(chosen) > 70 else ''}")
    return new_html


def upload_from_zip(
    dominio: str,
    campaign_id: str,
    player_ids: dict = None,
    oferta: str = "BrainMary",
    zip_path: str = None,
    adspect_index_php: bytes = None,
    # aliases legados
    vsl01_player: str = None,
    micro01_player: str = None,
):
    """Lê o ZIP da oferta, faz substituições em memória e envia via SFTP."""
    config = _load_offer_config(oferta)

    # Suporte a chamadas antigas com vsl01_player/micro01_player
    if player_ids is None:
        player_ids = {}
        if vsl01_player:
            player_ids["vsl01"] = vsl01_player
        if micro01_player:
            player_ids["micro01"] = micro01_player

    if zip_path is None:
        zip_path = _template_zip_path(oferta)
    zip_path = os.path.expanduser(zip_path)
    if not os.path.exists(zip_path):
        raise FileNotFoundError(
            f"ZIP da oferta '{oferta}' não encontrado em: {zip_path}\n"
            "Faça upload do arquivo ZIP no dashboard antes de executar o setup."
        )

    plano_name, plan, ssh, sftp = detect_plan(dominio)
    root = plan["root"]
    print(f"[sftp] Conectado a {plan['host']} ({plano_name}) → {root}")

    # Identifica páginas que são pre-landers (receberão variações)
    prelander_slugs = {
        p["slug"]
        for p in config.get("redtrack", {}).get("pages", [])
        if p.get("role") == "prelander"
    }
    _rng = random.Random()

    # Adspect: upload index.php e safe.html na raiz
    if adspect_index_php:
        safe_html_path = os.path.join(_DIR, "templates", "safe_page_ed.html")
        with sftp.open(f"{root}/index.php", "wb") as f:
            f.write(adspect_index_php)
        print(f"  ✓ {root}/index.php  (Adspect)")
        if os.path.exists(safe_html_path):
            with open(safe_html_path, "rb") as fh:
                safe_content = fh.read()
            with sftp.open(f"{root}/safe.html", "wb") as f:
                f.write(safe_content)
            print(f"  ✓ {root}/safe.html  (safe page ED)")

    text_exts = {".html", ".js", ".css", ".json", ".txt", ".xml", ".svg"}

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            pages_cfg = config.get("pages", _DEFAULT_PAGE_REPLACEMENTS)
            all_names = zf.namelist()

            for page in pages_cfg.keys():
                page_files = [n for n in all_names if n.startswith(f"{page}/") and not n.endswith("/")]
                if not page_files:
                    print(f"  [AVISO] Pasta '{page}' não encontrada no ZIP — pulando.")
                    continue

                replacements = _build_replacements(dominio, campaign_id, player_ids, page, config)
                for old_click, new_click in config.get("click_remap", []):
                    replacements[old_click] = new_click

                # Carrega variações se for pre-lander e houver variações disponíveis
                page_vars = _get_variations(oferta, page) if page in prelander_slugs else {}
                if page_vars:
                    print(f"    [variação] {len(page_vars.get('headlines', []))} headlines, "
                          f"{len(page_vars.get('images', []))} imagens disponíveis")

                print(f"\n[sftp] Enviando {page}/ ({len(page_files)} arquivo(s))")

                for zip_name in sorted(page_files):
                    remote_path = f"{root}/{zip_name}"
                    _ensure_dir(sftp, str(Path(remote_path).parent))

                    content = zf.read(zip_name)
                    ext = Path(zip_name).suffix.lower()

                    # Substituição de imagem hero (apenas no pre-lander)
                    if page_vars.get("images") and ext in {".jpg", ".jpeg", ".webp", ".png"}:
                        filename = Path(zip_name).name.lower()
                        if filename in {"imagem.jpeg", "imagem.jpg", "image.webp", "hero.jpg", "hero.jpeg", "hero.webp"}:
                            chosen_img = _rng.choice(page_vars["images"])
                            with open(chosen_img, "rb") as img_f:
                                content = img_f.read()
                            print(f"    [variação] Imagem: {os.path.basename(chosen_img)}")

                    if ext in text_exts:
                        text = content.decode("utf-8", errors="replace")
                        for old, new in replacements.items():
                            count = text.count(old)
                            if count:
                                text = text.replace(old, new)
                                print(f"    {old[:45]!r} → {new[:45]!r} ({count}x)")
                        # Variação de headline no HTML do pre-lander
                        if ext == ".html" and page_vars.get("headlines"):
                            text = _apply_headline_variation(text, page_vars["headlines"], _rng)
                        content = text.encode("utf-8")

                    with sftp.open(remote_path, "wb") as f:
                        f.write(content)
                    print(f"  ✓ {remote_path}")
    finally:
        sftp.close()
        ssh.close()

    print(f"\n[sftp] Upload concluído para {dominio} ({plano_name}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload SFTP BrainMary — zip direto para Hostinger")
    parser.add_argument("--dominio",         required=True)
    parser.add_argument("--campaign-id",     required=True)
    parser.add_argument("--vsl01-player",    required=True)
    parser.add_argument("--micro01-player",  required=True)
    parser.add_argument("--oferta",          default="BrainMary")
    parser.add_argument("--zip",             default=None)
    args = parser.parse_args()
    upload_from_zip(
        dominio=args.dominio,
        campaign_id=args.campaign_id,
        vsl01_player=args.vsl01_player,
        micro01_player=args.micro01_player,
        oferta=args.oferta,
        zip_path=args.zip,
    )

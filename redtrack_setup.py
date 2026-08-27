"""
Automação de setup RedTrack para novos domínios (YouTube/Google Ads).
Suporta qualquer oferta configurada em offer_configs.json.

Uso:
    python redtrack_setup.py --lc=LC160 --conta=123-456-7890 --dominio=mybrandnewdomain.online
    python redtrack_setup.py --lc=M1953 --conta=191-633-1113 --dominio=example.lat --oferta=HorseWood

Pré-requisito:
  - O domínio de tracking (fg.{dominio}) já deve estar registrado no RedTrack.
  - As páginas da oferta devem estar ativas na Hostinger.
"""

import os
import sys
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("REDTRACK_API_KEY")
BASE_URL = "https://api.redtrack.io"

_CONFIGS_FILE = os.path.join(os.path.dirname(__file__), "offer_configs.json")

# Configurações fixas do source Google Ads (iguais para todas as ofertas)
_SOURCE_SUBS = [
    {"value": "{replace}", "alias": "utm_campaign", "hint": "Campaign name", "role": "rt_campaign"},
    {"value": "{keyword}", "hint": "Bidded keyword", "role": "rt_keyword"},
    {"value": "{matchtype}", "hint": "Keyword match type"},
    {"value": "{adgroupid}", "hint": "Ad group ID", "role": "gid"},
    {"value": "{creative}", "hint": "Creative ID", "role": "aid"},
    {"value": "{campaignid}", "hint": "Campaign ID", "role": "cid"},
    {"value": "{device}", "hint": "Device type"},
    {"value": "{adposition}", "hint": "Ad position"},
    {"value": "{network}", "hint": "Network type"},
    {"value": "{placement}", "hint": "Website placement", "role": "pid"},
    {"value": "Google", "alias": "utm_source", "hint": "Source", "role": "rt_source"},
    {"value": "{wbraid}", "alias": "wbraid"},
    {"value": "{gbraid}", "alias": "gbraid"},
    {"value": "{_grupo}", "alias": "adgroup", "role": "rt_adgroup"},
    {"value": "{_anuncio}", "alias": "creative", "role": "rt_ad"},
    {"value": ""}, {"value": ""}, {"value": ""}, {"value": ""}, {"value": ""},
]


def _load_redtrack_config(oferta: str) -> dict:
    with open(_CONFIGS_FILE, encoding="utf-8") as f:
        all_configs = json.load(f)
    if oferta not in all_configs:
        raise ValueError(f"Oferta '{oferta}' não encontrada em offer_configs.json.")
    cfg = all_configs[oferta].get("redtrack")
    if not cfg:
        raise ValueError(
            f"Oferta '{oferta}' não tem seção 'redtrack' em offer_configs.json.\n"
            "Adicione lander_title, prelander_title, stream_title, campaign_title, offer_ids e pages."
        )
    return cfg


def _get(path, **params):
    params["api_key"] = API_KEY
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE_URL}{path}", params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout GET {path}, aguardando {wait}s...")
            time.sleep(wait)
    raise requests.exceptions.Timeout(f"GET {path} falhou após 3 tentativas")


def _post(path, payload):
    for attempt in range(3):
        try:
            r = requests.post(
                f"{BASE_URL}{path}",
                params={"api_key": API_KEY},
                json=payload,
                timeout=60,
            )
            if not r.ok:
                print(f"  ERRO {r.status_code}: {r.text[:500]}")
                r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout POST {path}, aguardando {wait}s...")
            time.sleep(wait)
    raise requests.exceptions.Timeout(f"POST {path} falhou após 3 tentativas")


def _put(path, payload):
    for attempt in range(3):
        try:
            r = requests.put(
                f"{BASE_URL}{path}",
                params={"api_key": API_KEY},
                json=payload,
                timeout=60,
            )
            if not r.ok:
                print(f"  ERRO {r.status_code}: {r.text[:500]}")
                r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout PUT {path}, aguardando {wait}s...")
            time.sleep(wait)
    raise requests.exceptions.Timeout(f"PUT {path} falhou após 3 tentativas")


def find_domain(tracking_domain: str) -> str:
    """Retorna o domain_id do tracking domain fg.{dominio} no RedTrack.
    Se não existir, registra automaticamente e lembra de adicionar o CNAME no DNS.
    """
    domains = _get("/domains", limit=2000)
    items = domains if isinstance(domains, list) else domains.get("items", [])
    for d in items:
        if d.get("url", "").lower() == tracking_domain.lower():
            return d["id"]
    print(f"  [domain] '{tracking_domain}' não encontrado — registrando no Redtrack...")
    result = _post("/domains", {"url": tracking_domain, "type": "track"})
    domain_id = result.get("id")
    if not domain_id:
        raise ValueError(f"Falha ao registrar '{tracking_domain}' no Redtrack: {result}")
    print(f"  [domain] Registrado (id={domain_id})")
    print(f"  [domain] ⚠️  Adicione o CNAME no DNS da conta: {tracking_domain} → tzegi.ttrk.io (proxy OFF)")
    return domain_id


def create_source(lc_code: str, account_number: str, dry_run: bool = False) -> dict:
    title = f"Google Ads | {account_number} | {lc_code}"

    if not dry_run:
        sources = _get("/sources", limit=3000)
        items = sources if isinstance(sources, list) else sources.get("items", [])
        for s in items:
            if s.get("title", "").strip() == title:
                print(f"  [1/6] Source já existe: {title} (id={s['id']})")
                return s

    payload = {
        "title": title,
        "type": "i",
        "alias": "google",
        "currency": "USD",
        "cost_level": "ad",
        "preset_id": "5f631fd2a49037000154d075",
        "subs": _SOURCE_SUBS,
        "integrations": {"fraudscore": False, "source_name": "", "params": {}},
        "integration_types": {
            "cost_update": True,
            "blacklist_placement": True,
            "pause_campaign": True,
            "pause_creative": True,
            "pause_adgroup": True,
            "pause_and_restart_campaign": True,
            "pause_and_restart_ad_group": True,
            "pause_and_restart_creative": True,
        },
    }
    print(f"  [1/6] Criando source: {title}")
    if dry_run:
        return {"id": "DRY_SOURCE_ID", "title": title}
    return _post("/sources", payload)


def _empty_filters() -> dict:
    base = {k: {"values": [], "kind": 0, "active": False, "exclude": False, "comparison_type": "EQ"}
            for k in ["country","region","city","isp","browser","browser_version","os","os_version",
                      "device_brand","device_model","languages","referrer","domain_referrer","ip",
                      "device_type","connection_type","proxy_type","fraud"]}
    base["subs"] = {
        "items": {f"sub{i}": "" for i in range(1, 21)},
        "kind": 0, "active": False, "exclude": False, "comparison_type": "EQ",
    }
    base["unique_visitor"] = {"kind": 0, "active": False, "exclude": False, "comparison_type": "EQ"}
    return base


def create_landing(title: str, url: str, page_type: str, domain_id: str, dry_run: bool = False) -> dict:
    if dry_run:
        return {"id": f"DRY_{page_type.upper()}_ID", "title": title, "url": url}
    landings = _get("/landings", limit=3000)
    items = landings if isinstance(landings, list) else landings.get("items", [])
    for l in items:
        if l.get("title", "").strip() == title:
            print(f"         (já existe, reutilizando id={l['id']})")
            return l
    return _post("/landings", {"title": title, "url": url, "type": page_type, "domain_id": domain_id, "listicle": False})


def create_funnel_template(
    lc_code: str,
    base_domain: str,
    prelander_id: str,
    lander_ids: list,
    cfg: dict,
    offer_ids: list = None,
    dry_run: bool = False,
) -> dict:
    tracking_domain = f"fg.{base_domain}"
    title = cfg["stream_title"].format(lc=lc_code, domain=tracking_domain)
    ef = _empty_filters()
    all_offer_ids = offer_ids if offer_ids is not None else cfg["offer_ids"]

    print(f"  [5/6] Funnel template: {title}")
    if dry_run:
        return {"id": "DRY_STREAM_ID", "title": title}

    streams = _get("/streams", limit=3000)
    items = streams if isinstance(streams, list) else streams.get("items", [])
    existing = next((s for s in items if s.get("title", "").strip() == title), None)

    if existing:
        print(f"         (já existe, reutilizando id={existing['id']})")
        return existing

    prelandings = [{"id": prelander_id, "weight": 100, "filters": ef}] if prelander_id else []
    equal_weight = round(100 / len(lander_ids)) if lander_ids else 100
    landings = [{"id": lid, "weight": equal_weight, "filters": ef} for lid in lander_ids]
    offers   = [{"id": oid, "weight": 100, "filters": ef} for oid in all_offer_ids]
    print(f"         {len(lander_ids)} landers | peso {equal_weight}% cada | {len(all_offer_ids)} offers")
    payload = {
        "title": title,
        "prelandings": prelandings,
        "landings": landings,
        "offers": offers,
        "filters": ef,
        "template": True,
    }
    return _post("/streams", payload)


def create_campaign(
    lc_code: str,
    account_number: str,
    base_domain: str,
    source_id: str,
    domain_id: str,
    stream: dict,
    cfg: dict,
    dry_run: bool = False,
) -> dict:
    tracking_domain = f"fg.{base_domain}"
    title = cfg["campaign_title"].format(lc=lc_code, conta=account_number, domain=tracking_domain, gestor=cfg.get("_gestor", ""))

    payload = {
        "title": title,
        "source_id": source_id,
        "domain_id": domain_id,
        "cost_model": "CPC",
        "redirect_type": 1,
        "rev_share": 100,
        "tags": ["Prime-GH", "Gestor-GH"],
        "integrations": {"fraudscore": False, "cost_update": True},
        "streams": [
            {
                "id": stream["id"],
                "weight": 100,
                "optimization": {
                    "is_enabled": False, "count": 0, "limit": 0, "metric": "cr",
                    "subs": [], "winner_share_limit": 70, "threshold": 0,
                    "multiplicator": 5, "clicks_limit": 0, "conversion_types": [],
                },
                "stream": stream,
            }
        ],
    }

    print(f"  [6/6] Campanha: {title}")
    if dry_run:
        return {"id": "DRY_CAMPAIGN_ID", "title": title}
    campaigns = _get("/campaigns", limit=3000)
    items = campaigns if isinstance(campaigns, list) else campaigns.get("items", [])
    for c in items:
        if c.get("title", "").strip() == title:
            print(f"         (já existe, reutilizando id={c['id']})")
            return c
    return _post("/campaigns", payload)


def setup(lc_code: str, account_number: str, base_domain: str, oferta: str = "BrainMary", dry_run: bool = False, gestor: str = None):
    tracking_domain = f"fg.{base_domain}"
    cfg = _load_redtrack_config(oferta)
    aff_id = cfg.get("aff_id") or ""

    # Resolver gestor
    cfg = dict(cfg)
    if "gestor_offer_ids" in cfg and gestor:
        gestor_map = cfg["gestor_offer_ids"]
        if gestor in gestor_map:
            cfg["offer_ids"] = gestor_map[gestor]
    cfg["_gestor"] = gestor or ""

    gestor_label = f" [{gestor}]" if gestor else ""
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Setup RedTrack [{oferta}{gestor_label}]: {lc_code} | {account_number} | {base_domain}")
    print("=" * 70)

    # 0. Localizar domínio
    print(f"  [0/6] Buscando domínio '{tracking_domain}'...")
    if dry_run:
        domain_id = "DRY_DOMAIN_ID"
        print(f"         (dry run) domain_id = {domain_id}")
    else:
        domain_id = find_domain(tracking_domain)
        print(f"         domain_id = {domain_id}")

    # 1. Source
    source = create_source(lc_code, account_number, dry_run)
    source_id = source.get("id", "?")
    print(f"         source_id = {source_id}")

    # Montar lista completa de páginas (próprias + parceiro de funnel)
    all_page_entries = []
    seen_slugs = set()
    for page in cfg["pages"]:
        if page["slug"] not in seen_slugs:
            all_page_entries.append((page, cfg, aff_id))
            seen_slugs.add(page["slug"])

    all_offer_ids = list(cfg["offer_ids"])
    if "burntide_offer_ids" in cfg:
        for oid in cfg["burntide_offer_ids"]:
            if oid not in all_offer_ids:
                all_offer_ids.append(oid)
    partner_name = cfg.get("funnel_partner")
    if partner_name:
        pcfg = _load_redtrack_config(partner_name)
        paff_id = pcfg.get("aff_id") or ""
        for page in pcfg["pages"]:
            if page["slug"] not in seen_slugs:
                all_page_entries.append((page, pcfg, paff_id))
                seen_slugs.add(page["slug"])
        for oid in pcfg["offer_ids"]:
            if oid not in all_offer_ids:
                all_offer_ids.append(oid)
        print(f"         funnel_partner={partner_name} | {len(all_offer_ids)} offers no total")

    # 2–N. Pre-lander e Landers
    prelander_id = None
    lander_ids = []
    total_pages = len(all_page_entries)

    for i, (page, page_cfg, page_aff_id) in enumerate(all_page_entries, start=2):
        slug  = page["slug"]
        label = page["label"]
        ptype = page["type"]
        role  = page["role"]

        if "title_override" in page:
            t = page["title_override"].format(lc=lc_code, label=label, domain=tracking_domain)
        elif role == "prelander":
            t = page_cfg["prelander_title"].format(lc=lc_code, label=label, domain=tracking_domain)
        else:
            t = page_cfg["lander_title"].format(lc=lc_code, label=label, domain=tracking_domain)

        url = f"https://{base_domain}/{slug}?aff_id={page_aff_id}" if page_aff_id else f"https://{base_domain}/{slug}"
        print(f"  [{i}/{total_pages + 2}] {role.capitalize()}: {slug} → {url}")

        obj = create_landing(t, url, ptype, domain_id, dry_run)
        obj_id = obj.get("id", "?")
        print(f"         id = {obj_id}")

        if role == "prelander":
            prelander_id = obj_id
        else:
            lander_ids.append(obj_id)

    # 5. Funnel template
    stream = create_funnel_template(lc_code, base_domain, prelander_id, lander_ids, cfg, all_offer_ids, dry_run)
    stream_id = stream.get("id", "?")
    print(f"         stream_id = {stream_id}")

    # 6. Campanha
    campaign = create_campaign(
        lc_code, account_number, base_domain,
        source_id, domain_id, stream, cfg,
        dry_run,
    )
    campaign_id = campaign.get("id", "?")
    print(f"         campaign_id = {campaign_id}")

    print("\n✓ Concluído!")
    result = {
        "lc_code": lc_code,
        "account_number": account_number,
        "base_domain": base_domain,
        "tracking_domain": tracking_domain,
        "domain_id": domain_id,
        "source_id": source_id,
        "prelander_id": prelander_id,
        "lander_ids": lander_ids,
        "campaign_id": campaign_id,
        "trackback_url": f"https://{tracking_domain}/{campaign_id}",
    }
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    lc_code = None
    account_number = None
    base_domain = None
    oferta = "BrainMary"
    dry_run = "--dry-run" in sys.argv

    for arg in sys.argv[1:]:
        if arg.startswith("--lc="):
            lc_code = arg.split("=", 1)[1].upper()
        elif arg.startswith("--conta="):
            account_number = arg.split("=", 1)[1]
        elif arg.startswith("--dominio="):
            base_domain = arg.split("=", 1)[1]
        elif arg.startswith("--oferta="):
            oferta = arg.split("=", 1)[1]

    if not all([lc_code, account_number, base_domain]):
        print("Uso: python redtrack_setup.py --lc=LC160 --conta=123-456-7890 --dominio=example.lat [--oferta=HorseWood] [--dry-run]")
        sys.exit(1)

    setup(lc_code, account_number, base_domain, oferta=oferta, dry_run=dry_run)

"""
Setup completo — integra RedTrack + Vturb + upload FTP direto do ZIP.
Suporta qualquer número de players/VSLs definidos em offer_configs.json.

Uso:
    python brainmary_setup.py --lc LC160 --conta 123-456-7890 --dominio mynewdomain.online
    python brainmary_setup.py --lc LC160 --conta 123-456-7890 --dominio mynewdomain.online --oferta MaxForce
    python brainmary_setup.py --lc LC160 --conta 123-456-7890 --dominio mynewdomain.online --dry-run
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

_CONFIGS_FILE = os.path.join(os.path.dirname(__file__), "offer_configs.json")


def _load_offer_config(oferta):
    with open(_CONFIGS_FILE, encoding="utf-8") as f:
        configs = json.load(f)
    if oferta not in configs:
        raise ValueError(f"Oferta '{oferta}' não encontrada em offer_configs.json.")
    return configs[oferta]


def run_redtrack(lc, conta, dominio, oferta, dry_run, gestor=None):
    from redtrack_setup import setup
    print("\n" + "=" * 60)
    print(f"[1/3] REDTRACK — {lc} / {conta} / {dominio}")
    print("=" * 60)
    return setup(lc_code=lc, account_number=conta, base_domain=dominio, oferta=oferta, dry_run=dry_run, gestor=gestor)


def run_vturb(lc, dominio, oferta, dry_run):
    from vturb_automation import run_setup_lc
    print("\n" + "=" * 60)
    print(f"[2/3] VTURB — duplicar vídeos para {lc}")
    print("=" * 60)
    if dry_run:
        config = _load_offer_config(oferta)
        return {slug: f"DRY_RUN_{slug.upper()}_ID" for slug in config.get("vturb_templates", {})}
    return run_setup_lc(lc, dominio=dominio, oferta=oferta)


def run_adspect(lc, dominio, oferta, dry_run):
    from adspect_setup import get_or_create_stream, get_index_php
    import os, json
    print("\n" + "=" * 60)
    print(f"[0/3] ADSPECT — stream para {lc}")
    print("=" * 60)
    # Descobrir slug da pre-lander a partir do offer_config
    configs_path = os.path.join(os.path.dirname(__file__), "offer_configs.json")
    with open(configs_path, encoding="utf-8") as f:
        configs = json.load(f)
    pages = configs.get(oferta, {}).get("redtrack", {}).get("pages", [])
    prelander_slug = next((p["slug"] for p in pages if p.get("role") == "prelander"), "premax")
    money_url = f"https://{dominio}/{prelander_slug}/"
    if dry_run:
        print(f"[dry-run] Adspect: simulando stream para {money_url}")
        return None, None
    stream   = get_or_create_stream(lc, oferta, money_url)
    index_php = get_index_php(stream["stream_id"])
    print(f"[adspect] ✓ Stream ID: {stream['stream_id']}", flush=True)
    return stream, index_php


def run_ftp(dominio, campaign_id, player_ids, oferta, zip_path, dry_run, adspect_index_php=None):
    from ftp_upload import upload_from_zip
    print("\n" + "=" * 60)
    print(f"[3/3] FTP — upload para {dominio}")
    print("=" * 60)
    if dry_run:
        print(f"[dry-run] FTP: simulando upload do ZIP para {dominio}")
        return
    upload_from_zip(
        dominio=dominio,
        campaign_id=campaign_id,
        player_ids=player_ids,
        oferta=oferta,
        zip_path=zip_path or None,
        adspect_index_php=adspect_index_php,
    )


def run_github_pages(conta, dominio, campaign_id, player_ids, lander_ids, oferta, zip_path, dry_run):
    from github_pages_upload import upload_pages, update_redtrack_lander_urls
    print("\n" + "=" * 60)
    print(f"[3/3] GITHUB PAGES — upload para {oferta}-lander/{conta}/")
    print("=" * 60)
    if dry_run:
        print(f"[dry-run] GitHub Pages: simulando upload para {oferta.lower()}-lander/{conta}/")
        return
    page_urls = upload_pages(
        conta=conta,
        oferta=oferta,
        dominio=dominio,
        campaign_id=campaign_id,
        player_ids=player_ids,
        zip_path=zip_path or None,
    )
    if lander_ids:
        update_redtrack_lander_urls(lander_ids, page_urls, oferta)


def main():
    # Parser base — argumentos conhecidos + pass-through para --player-{slug}=
    parser = argparse.ArgumentParser(description="Setup completo de oferta")
    parser.add_argument("--lc",            required=True)
    parser.add_argument("--conta",         required=True)
    parser.add_argument("--dominio",       required=True)
    parser.add_argument("--oferta",        default="BrainMary")
    parser.add_argument("--zip",           default=None)
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--skip-redtrack", action="store_true")
    parser.add_argument("--skip-vturb",    action="store_true")
    parser.add_argument("--skip-ftp",      action="store_true")
    parser.add_argument("--skip-adspect",  action="store_true", help="Pular criação de stream no Adspect")
    parser.add_argument("--github-pages",  action="store_true", help="Usar GitHub Pages em vez de FTP")
    parser.add_argument("--campaign-id",   default=None)
    parser.add_argument("--gestor",        default=None, help="Gestor da conta (ex: GH ou AN)")
    parser.add_argument("--webhook-url",   default=None, help="URL do webhook Vturb local")
    parser.add_argument("--webhook-token", default=None, help="Token do webhook Vturb")
    args, remaining = parser.parse_known_args()

    # Garante que os env vars do webhook estejam disponíveis para run_setup_lc
    if args.webhook_url:
        os.environ["VTURB_WEBHOOK_URL"]   = args.webhook_url
    if args.webhook_token:
        os.environ["VTURB_WEBHOOK_TOKEN"] = args.webhook_token

    # Parsear --player-{slug}=<id> dinamicamente
    player_ids = {}
    for arg in remaining:
        if arg.startswith("--player-"):
            parts = arg[len("--player-"):].split("=", 1)
            if len(parts) == 2:
                player_ids[parts[0]] = parts[1]

    campaign_id = args.campaign_id
    rt = None
    adspect_index_php = None

    # 0. Adspect
    if not args.skip_adspect and not args.github_pages:
        _, adspect_index_php = run_adspect(args.lc, args.dominio, args.oferta, args.dry_run)

    # 1. RedTrack
    if not args.skip_redtrack:
        rt = run_redtrack(args.lc, args.conta, args.dominio, args.oferta, args.dry_run, gestor=args.gestor)
        if rt:
            campaign_id = campaign_id or rt.get("campaign_id")
    if not campaign_id:
        print("\nERRO: campaign_id não disponível. Use --campaign-id ou rode sem --skip-redtrack.")
        sys.exit(1)

    # 2. Vturb
    vturb_cloudflare_blocked = False
    if not args.skip_vturb:
        try:
            vt = run_vturb(args.lc, args.dominio, args.oferta, args.dry_run)
            if vt:
                for slug, pid in vt.items():
                    player_ids.setdefault(slug, pid)
        except RuntimeError as e:
            if "VTURB_CLOUDFLARE" in str(e):
                vturb_cloudflare_blocked = True
                print("\n⚠  VTURB — Cloudflare bloqueou o acesso neste ambiente.")
                print("   Duplique os players manualmente no painel do Vturb.")
                print("   Depois rode o upload separadamente com:")
                print(f"   --skip-redtrack --skip-adspect --campaign-id {campaign_id} \\")
                config_tmp = _load_offer_config(args.oferta)
                for slug in config_tmp.get("vturb_templates", {}):
                    print(f"   --player-{slug}=<ID_DO_PLAYER_{slug.upper()}>", end=" ")
                print()
            else:
                raise

    if not vturb_cloudflare_blocked:
        config = _load_offer_config(args.oferta)
        expected_slugs = list(config.get("vturb_templates", {}).keys())
        missing = [s for s in expected_slugs if s not in player_ids]
        if missing:
            print(f"\nERRO: player IDs faltando para: {missing}")
            sys.exit(1)

    # 3. Upload (FTP ou GitHub Pages)
    lander_ids = rt.get("lander_ids", []) if rt else []
    if vturb_cloudflare_blocked:
        print("\n⚠  FTP pulado — aguardando IDs dos players Vturb.")
    elif args.github_pages:
        run_github_pages(args.conta, args.dominio, campaign_id, player_ids, lander_ids, args.oferta, args.zip, args.dry_run)
    elif not args.skip_ftp:
        run_ftp(args.dominio, campaign_id, player_ids, args.oferta, args.zip, args.dry_run, adspect_index_php)

    # Resumo final
    print("\n" + "=" * 60)
    print("SETUP CONCLUÍDO")
    print("=" * 60)
    print(f"  LC          : {args.lc}")
    print(f"  Conta       : {args.conta}")
    print(f"  Domínio     : fg.{args.dominio}")
    print(f"  Campaign ID : {campaign_id}")
    for slug, pid in player_ids.items():
        print(f"  Vturb {slug:<10}: {pid}")


if __name__ == "__main__":
    main()

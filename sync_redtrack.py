"""
Script de sincronização automática — roda via GitHub Actions.
Verifica novos Traffic Channels no Redtrack e adiciona ao accounts_to_invite.txt.
"""
import os
import re
import json
import requests

ARQUIVO_CONTAS  = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
ARQUIVO_SERIAL  = os.path.join(os.path.dirname(__file__), "last_serial.json")
REDTRACK_BASE   = "https://api.redtrack.io"
REDTRACK_KEY    = os.environ.get("REDTRACK_API_KEY", "")


def ler_ultimo_serial() -> int:
    if os.path.exists(ARQUIVO_SERIAL):
        with open(ARQUIVO_SERIAL, encoding="utf-8") as f:
            return json.load(f).get("last_serial", 0)
    return 0


def salvar_ultimo_serial(serial: int):
    with open(ARQUIVO_SERIAL, "w", encoding="utf-8") as f:
        json.dump({"last_serial": serial}, f)


def carregar_existentes() -> set:
    existentes = set()
    if not os.path.exists(ARQUIVO_CONTAS):
        return existentes
    with open(ARQUIVO_CONTAS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for prefix in ["✅", "⏳", "🚫"]:
                if line.startswith(prefix):
                    line = line[1:].strip()
                    break
            acc_id = line.split(" | ")[0].strip()
            existentes.add(acc_id)
    return existentes


def main():
    if not REDTRACK_KEY:
        print("REDTRACK_API_KEY não configurada.")
        return

    ultimo = ler_ultimo_serial()
    existentes = carregar_existentes()
    print(f"Último serial: #{ultimo} | Contas na lista: {len(existentes)}")

    novos = []
    maior_serial = ultimo
    page = 1

    while True:
        r = requests.get(f"{REDTRACK_BASE}/sources", params={
            "api_key": REDTRACK_KEY,
            "limit": 100,
            "page": page,
        }, timeout=20)
        items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        if not items:
            break

        for s in items:
            serial = s.get("serial_number", 0)
            if serial > maior_serial:
                maior_serial = serial
            if serial <= ultimo:
                continue

            name = s.get("title", "")
            network_id = s.get("network_id", "")
            campaign_pattern = s.get("campaign_pattern", "")
            ids = re.findall(r'\d{3}-\d{3}-\d{4}',
                             f"{network_id} {campaign_pattern} {name}")
            for acc_id in ids:
                if acc_id not in existentes:
                    nome_match = re.search(r'\|\s*([^\|]+)$', name)
                    nome = nome_match.group(1).strip() if nome_match else name.split("|")[-1].strip()
                    novos.append((serial, acc_id, nome))
                    existentes.add(acc_id)
                    break

        if len(items) < 100:
            break
        page += 1

    if novos:
        with open(ARQUIVO_CONTAS, "a", encoding="utf-8") as f:
            for _, acc_id, nome in sorted(novos):
                f.write(f"   {acc_id} | {nome}\n")
        print(f"{len(novos)} nova(s) conta(s) adicionada(s):")
        for serial, acc_id, nome in sorted(novos):
            print(f"  #{serial} | {acc_id} | {nome}")
    else:
        print("Nenhuma conta nova encontrada.")

    if maior_serial > ultimo:
        salvar_ultimo_serial(maior_serial)
        print(f"Último serial atualizado para #{maior_serial}")


if __name__ == "__main__":
    main()

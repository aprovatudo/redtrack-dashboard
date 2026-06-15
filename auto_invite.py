"""
Automação de convites MCC — roda via GitHub Actions.
1. Cancela convites PENDING com mais de 1 dia
2. Envia novos convites para preencher os 20 slots
3. Atualiza accounts_to_invite.txt
"""
import os
import re
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

ARQUIVO_CONTAS     = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
ARQUIVO_TIMESTAMPS = os.path.join(os.path.dirname(__file__), "invite_timestamps.json")
MCC_ID             = os.environ.get("GOOGLE_ADS_MCC_ID", "").replace("-", "")
DEV_TOKEN          = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
CLIENT_ID          = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET      = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN      = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
BASE_ADS           = "https://googleads.googleapis.com/v21"
PENDING_MAX_HORAS  = 24   # cancela após 1 dia


def get_access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "developer-token": DEV_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }


def carregar_timestamps() -> dict:
    if os.path.exists(ARQUIVO_TIMESTAMPS):
        with open(ARQUIVO_TIMESTAMPS, encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_timestamps(ts: dict):
    with open(ARQUIVO_TIMESTAMPS, "w", encoding="utf-8") as f:
        json.dump(ts, f, ensure_ascii=False, indent=2)


def carregar_contas() -> list:
    contas = []
    with open(ARQUIVO_CONTAS, encoding="utf-8") as f:
        for line in f:
            line_raw = line.rstrip("\n")
            stripped = line_raw.strip()
            if not stripped:
                continue
            if stripped.startswith("✅"):
                status = "✅ Aceito"
                stripped = stripped[1:].strip()
            elif stripped.startswith("⏳"):
                status = "⏳ Pendente"
                stripped = stripped[1:].strip()
            elif stripped.startswith("🚫"):
                status = "🚫 Suspensa"
                stripped = stripped[1:].strip()
            else:
                status = "📤 Não enviado"
            parts = stripped.split(" | ", 1)
            acc_id = parts[0].strip()
            nome   = parts[1].strip() if len(parts) > 1 else "?"
            contas.append({"id": acc_id, "nome": nome, "status": status})
    return contas


def salvar_contas(contas: list):
    with open(ARQUIVO_CONTAS, "w", encoding="utf-8") as f:
        for c in contas:
            if c["status"] == "✅ Aceito":
                prefix = "✅"
            elif c["status"] == "⏳ Pendente":
                prefix = "⏳"
            elif c["status"] == "🚫 Suspensa":
                prefix = "🚫"
            else:
                prefix = "  "
            f.write(f"{prefix} {c['id']} | {c['nome']}\n")


def buscar_pending(token: str) -> dict:
    """Retorna {acc_id: resource_name} para convites PENDING."""
    r = requests.post(f"{BASE_ADS}/customers/{MCC_ID}/googleAds:search",
        headers=headers(token),
        json={"query": """
            SELECT customer_client_link.resource_name,
                   customer_client_link.client_customer,
                   customer_client_link.status
            FROM customer_client_link
            WHERE customer_client_link.status = PENDING
        """},
        timeout=15)
    result = {}
    for row in r.json().get("results", []):
        link = row.get("customerClientLink", {})
        raw  = link.get("clientCustomer", "").replace("customers/", "")
        fmt  = f"{raw[:3]}-{raw[3:6]}-{raw[6:]}" if len(raw) >= 7 else raw
        result[fmt] = link.get("resourceName", "")
    return result


def cancelar_invite(token: str, resource_name: str) -> bool:
    r = requests.post(
        f"{BASE_ADS}/customers/{MCC_ID}/customerClientLinks:mutate",
        headers=headers(token),
        json={"operation": {"update": {"resourceName": resource_name,
                                        "status": "CANCELED"},
                             "updateMask": "status"}},
        timeout=15)
    return r.status_code == 200


def enviar_invite(token: str, acc_id: str) -> tuple[bool, str]:
    acc_clean = acc_id.replace("-", "")
    r = requests.post(
        f"{BASE_ADS}/customers/{MCC_ID}/customerClientLinks:mutate",
        headers=headers(token),
        json={"operation": {"create": {
            "clientCustomer": f"customers/{acc_clean}",
            "status": "PENDING"
        }}},
        timeout=15)
    if r.status_code == 200:
        return True, r.json().get("result", {}).get("resourceName", "ok")
    msg = r.json().get("error", {}).get("message", r.text[:100])
    return False, msg


def main():
    print(f"=== Auto Invite — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")

    token      = get_access_token()
    contas     = carregar_contas()
    timestamps = carregar_timestamps()
    agora      = datetime.now(timezone.utc)

    # ── 1. Verifica quais PENDING devem ser cancelados (> 1 dia) ──────────
    pending_api = buscar_pending(token)
    cancelados  = 0

    for acc_id, resource_name in pending_api.items():
        enviado_em = timestamps.get(acc_id)
        if not enviado_em:
            continue
        dt_envio = datetime.fromisoformat(enviado_em)
        horas_passadas = (agora - dt_envio).total_seconds() / 3600

        if horas_passadas >= PENDING_MAX_HORAS:
            print(f"  ⌛ Cancelando {acc_id} ({horas_passadas:.1f}h pendente)...")
            if cancelar_invite(token, resource_name):
                # Marca como 🚫 no arquivo
                for c in contas:
                    if c["id"] == acc_id:
                        c["status"] = "🚫 Suspensa"
                        break
                timestamps.pop(acc_id, None)
                cancelados += 1
                print(f"     ✓ Cancelado e marcado como 🚫")

    # ── 2. Sincroniza status ✅/⏳ da API ─────────────────────────────────
    for acc_id in list(pending_api.keys()):
        if acc_id not in [c for c in [timestamps]]:  # ainda está ativo
            for c in contas:
                if c["id"] == acc_id and c["status"] == "📤 Não enviado":
                    c["status"] = "⏳ Pendente"

    # ── 3. Envia novos convites para preencher slots ───────────────────────
    pending_atual = len(buscar_pending(token))
    slots_livres  = 20 - pending_atual
    print(f"\nSlots livres: {slots_livres}/20 | Cancelados agora: {cancelados}")

    enviados = 0
    for c in contas:
        if slots_livres <= 0:
            break
        if c["status"] != "📤 Não enviado":
            continue

        ok, msg = enviar_invite(token, c["id"])
        if ok:
            c["status"] = "⏳ Pendente"
            timestamps[c["id"]] = agora.isoformat()
            enviados += 1
            slots_livres -= 1
            print(f"  ✓ Enviado: {c['id']} | {c['nome']}")
        else:
            print(f"  ✗ Falhou: {c['id']} | {msg[:80]}")
        time.sleep(0.3)

    # ── 4. Salva alterações ───────────────────────────────────────────────
    salvar_contas(contas)
    salvar_timestamps(timestamps)
    print(f"\nResumo: {enviados} enviados, {cancelados} cancelados/marcados 🚫")


if __name__ == "__main__":
    main()

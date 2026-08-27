"""
Integração com a API do Adspect.
Cria streams e baixa o index.php de integração por conta.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

_API_KEY  = os.getenv("ADSPECT_API_KEY", "")
_BASE_URL = "https://api.adspect.net/v1"
_AUTH     = (_API_KEY, "")


def _req(method: str, path: str, **kwargs):
    r = requests.request(
        method,
        f"{_BASE_URL}{path}",
        auth=_AUTH,
        headers={"Content-Type": "application/json"},
        timeout=30,
        **kwargs,
    )
    return r


def get_or_create_stream(lc: str, oferta: str, money_url: str) -> dict:
    """Retorna stream existente se já houver um com esse nome, senão cria."""
    name = f"{oferta} {lc}"
    streams = list_streams()
    existing = next((s for s in streams if s.get("name") == name), None)
    if existing:
        print(f"[adspect] Stream existente encontrado: {existing['stream_id']} ({name})", flush=True)
        return existing
    return create_stream(lc, oferta, money_url)


def create_stream(lc: str, oferta: str, money_url: str) -> dict:
    """
    Cria um stream no Adspect para o LC/oferta informados.
    money_url = URL da pre-lander (onde usuários reais serão enviados).
    Retorna o objeto do stream criado (inclui stream_id).
    """
    payload = {
        "name": f"{oferta} {lc}",
        "preset": "GoogleAds",
        "mode": "Filter",
        "money_pages": [
            {
                "page": money_url,
                "action": "302",
                "arg_passthru": True,
                "weight": 100,
                "enabled": True,
            }
        ],
        "safe_pages": [
            {
                "page": "safe.html",
                "action": "local",
                "arg_passthru": False,
            }
        ],
        "click_id": "{p:gclid}",
        "allow_google_proxy": False,
        "allow_apple_proxy": False,
        "filter_level": 2,
        "countries": ["US"],
        "languages": ["en"],
        "enable_fp": True,
        "require_language": True,
    }
    r = _req("POST", "/streams", json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar stream Adspect: {r.status_code} {r.text[:300]}")
    stream = r.json()
    # Corrigir nome (preset sobrescreve o campo name na criação)
    stream_id = stream["stream_id"]
    _req("PATCH", f"/streams/{stream_id}", json={"name": f"{oferta} {lc}"})
    stream["name"] = f"{oferta} {lc}"
    print(f"[adspect] Stream criado: {stream_id} → {stream['name']}", flush=True)
    return stream


def get_index_php(stream_id: str) -> bytes:
    """Baixa o index.php de integração para o stream."""
    r = _req("GET", f"/streams/{stream_id}/file", params={"name": "index.php"})
    if r.status_code != 200:
        raise RuntimeError(f"Erro ao baixar index.php: {r.status_code} {r.text[:200]}")
    print(f"[adspect] index.php baixado ({len(r.content)} bytes)", flush=True)
    return r.content


def update_stream_money_url(stream_id: str, money_url: str):
    """Atualiza a URL da money page de um stream existente."""
    r = _req("GET", f"/streams/{stream_id}")
    if r.status_code != 200:
        raise RuntimeError(f"Erro ao buscar stream {stream_id}: {r.status_code}")
    stream = r.json()
    pages = stream.get("money_pages", [])
    if pages:
        pages[0]["page"] = money_url
    else:
        pages = [{"page": money_url, "action": "302", "arg_passthru": True, "weight": 100, "enabled": True}]
    r2 = _req("PATCH", f"/streams/{stream_id}", json={"money_pages": pages})
    if r2.status_code not in (200, 204):
        raise RuntimeError(f"Erro ao atualizar stream: {r2.status_code} {r2.text[:200]}")
    print(f"[adspect] ✓ money_url atualizada: {money_url}", flush=True)


def list_streams() -> list:
    """Lista todos os streams da conta."""
    r = _req("GET", "/streams")
    r.raise_for_status()
    return r.json()


def delete_stream(stream_id: str):
    """Remove um stream."""
    r = _req("DELETE", f"/streams/{stream_id}")
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Erro ao deletar stream {stream_id}: {r.status_code}")
    print(f"[adspect] Stream {stream_id} removido.", flush=True)

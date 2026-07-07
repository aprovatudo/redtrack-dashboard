"""
redtrack_recorder.py — Captura todas as chamadas de API do RedTrack enquanto você navega.

Uso:
  python redtrack_recorder.py

O script abre o RedTrack no browser. Você navega normalmente e cria:
  1. Um domínio
  2. Uma lander
  3. Um funnel template
  4. Uma campanha

Ao fechar o browser, salva todas as requisições capturadas em redtrack_api_map.json
"""

import atexit
import json
import time
from datetime import datetime
from playwright.sync_api import sync_playwright

OUTPUT = "redtrack_api_map.json"
REDTRACK_URL = "https://app.redtrack.io/dashboard"

# Endpoints que NÃO interessam (ruído)
IGNORE_PATTERNS = [
    "google-analytics", "analytics", "hotjar", "intercom",
    "sentry", "crisp", "segment", "mixpanel", "amplitude",
    ".css", ".js", ".png", ".jpg", ".svg", ".ico", ".woff",
    "socket.io", "websocket",
]


def should_capture(url: str) -> bool:
    url_lower = url.lower()
    for pattern in IGNORE_PATTERNS:
        if pattern in url_lower:
            return False
    # Só captura chamadas para o próprio redtrack
    return "redtrack.io" in url_lower


captured = []
current_label = {"value": "geral"}


def _salvar_e_resumir():
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(captured, f, ensure_ascii=False, indent=2)

    methods = {}
    for item in captured:
        key = f"{item['method']} {item['url'].split('?')[0]}"
        methods[key] = methods.get(key, 0) + 1

    print(f"\n✅ {len(captured)} requisições capturadas → {OUTPUT}")
    print("\nEndpoints únicos encontrados:")
    for endpoint, count in sorted(methods.items()):
        print(f"  [{count:2d}x] {endpoint}")


def on_request(request):
    if not should_capture(request.url):
        return
    if request.method in ("GET",) and "/api/" not in request.url:
        return  # ignora GETs de recursos estáticos
    captured.append({
        "label":    current_label["value"],
        "method":   request.method,
        "url":      request.url,
        "headers":  dict(request.headers),
        "body":     _safe_body(request),
        "ts":       datetime.now().isoformat(),
    })


def on_response(response):
    if not should_capture(response.url):
        return
    if response.request.method == "GET" and "/api/" not in response.url:
        return
    # Atualiza o último item capturado com o status e body da resposta
    for item in reversed(captured):
        if item["url"] == response.url and "response_status" not in item:
            item["response_status"] = response.status
            try:
                item["response_body"] = response.json()
            except Exception:
                item["response_body"] = response.text()[:500]
            break


def _safe_body(request):
    try:
        body = request.post_data
        if not body:
            return None
        try:
            return json.loads(body)
        except Exception:
            return body
    except Exception:
        return None


def main():
    print("=" * 60)
    print("  RedTrack API Recorder")
    print("=" * 60)
    print()
    print("O browser vai abrir no RedTrack.")
    print("Faça login se necessário e então:")
    print()
    print("  1. Crie UM domínio de teste")
    print("  2. Crie UMA lander de teste")
    print("  3. Crie UM funnel template de teste")
    print("  4. Crie UMA campanha de teste")
    print()
    print("Ao terminar, FECHE o browser.")
    print("As requisições serão salvas em:", OUTPUT)
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        page.on("request",  on_request)
        page.on("response", on_response)

        page.goto(REDTRACK_URL)

        # Aguarda o browser ser fechado pelo usuário
        try:
            while True:
                if not browser.is_connected():
                    break
                time.sleep(1)
        except (Exception, KeyboardInterrupt):
            pass

    _salvar_e_resumir()


if __name__ == "__main__":
    atexit.register(_salvar_e_resumir)
    main()

"""
Servidor local que expõe o Vturb (Playwright) via HTTP.
Rode no Mac com: python vturb_webhook.py
Exponha com:     cloudflared tunnel --url http://localhost:7654
"""
import json
import os
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.getenv("VTURB_WEBHOOK_TOKEN", "vturb-secret-2026")
PORT  = int(os.getenv("VTURB_WEBHOOK_PORT", "7654"))
DIR   = os.path.dirname(os.path.abspath(__file__))

# Script inline que roda run_setup_lc em processo isolado (evita conflito de event loop)
_RUNNER = """
import sys, json
sys.path.insert(0, sys.argv[1])
lc, oferta, dominio = sys.argv[2], sys.argv[3], sys.argv[4] if sys.argv[4] != "__none__" else None
from vturb_automation import run_setup_lc
result = run_setup_lc(lc, dominio=dominio, oferta=oferta)
print("__RESULT__" + json.dumps(result))
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/vturb/setup":
            self._send(404, {"error": "not found"})
            return

        if self.headers.get("X-Token") != TOKEN:
            self._send(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        lc      = body.get("lc")
        oferta  = body.get("oferta", "BrainMary")
        dominio = body.get("dominio") or "__none__"

        if not lc:
            self._send(400, {"error": "lc é obrigatório"})
            return

        print(f"[webhook] Setup: lc={lc} oferta={oferta} dominio={dominio}", flush=True)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _RUNNER, DIR, lc, oferta, dominio],
                capture_output=True, text=True, timeout=300,
            )
            output = proc.stdout + proc.stderr
            # Imprime saída do subprocess no log
            for line in output.splitlines():
                print(f"  {line}", flush=True)

            # Extrai resultado JSON da linha marcada
            result_line = next((l for l in output.splitlines() if l.startswith("__RESULT__")), None)
            if result_line:
                player_ids = json.loads(result_line[len("__RESULT__"):])
                print(f"[webhook] ✓ player_ids={player_ids}", flush=True)
                self._send(200, {"player_ids": player_ids})
            else:
                err = output.strip().splitlines()[-1] if output.strip() else "Sem saída do processo"
                print(f"[webhook] ✗ Falhou: {err}", flush=True)
                self._send(500, {"error": err})
        except subprocess.TimeoutExpired:
            self._send(500, {"error": "Timeout após 300s"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[webhook] {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[webhook] Iniciando na porta {PORT}...")
    print(f"[webhook] Token: {TOKEN}")
    print(f"[webhook] Para expor: cloudflared tunnel --url http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

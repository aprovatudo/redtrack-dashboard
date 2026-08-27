"""
Servidor local que expõe o Vturb (Playwright) via HTTP.
Rode no Mac com: python vturb_webhook.py
Exponha com:     cloudflared tunnel --url http://localhost:7654
"""
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.getenv("VTURB_WEBHOOK_TOKEN", "vturb-secret-2026")
PORT  = int(os.getenv("VTURB_WEBHOOK_PORT", "7654"))


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
        dominio = body.get("dominio")

        if not lc:
            self._send(400, {"error": "lc é obrigatório"})
            return

        print(f"[webhook] Setup: lc={lc} oferta={oferta} dominio={dominio}", flush=True)
        try:
            from vturb_automation import run_setup_lc
            player_ids = run_setup_lc(lc, dominio=dominio, oferta=oferta)
            print(f"[webhook] ✓ player_ids={player_ids}", flush=True)
            self._send(200, {"player_ids": player_ids})
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

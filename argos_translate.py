#!/usr/bin/env python3
"""Server di traduzione locale su Argos Translate.

Gira nel venv (python3.12) perche' argostranslate non supporta il python 3.9
di sistema con cui parte live_translate.py. Comunica su HTTP invece che via
subprocess: caricare i modelli costa ~2s, farlo a ogni frase e' insostenibile.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import argostranslate.translate as tr

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8778
_cache = {}


def get(src, dst):
    key = (src, dst)
    if key not in _cache:
        langs = tr.get_installed_languages()
        a = next((l for l in langs if l.code == src), None)
        b = next((l for l in langs if l.code == dst), None)
        _cache[key] = a.get_translation(b) if a and b else None
    return _cache[key]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
            t = get(b.get("src", "pt"), b.get("dst", "it"))
            out = t.translate(b.get("text", "")) if t else ""
            code = 200 if out else 404
        except Exception as exc:  # noqa: BLE001
            out, code = str(exc), 500
        body = json.dumps({"text": out}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        pairs = []
        for l in tr.get_installed_languages():
            for t in l.translations_from:
                pairs.append(f"{l.code}-{t.to_lang.code}")
        body = json.dumps({"pairs": sorted(set(pairs))}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"argos su :{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

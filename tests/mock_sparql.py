"""
Mini-SPARQL-Endpunkt (rdflib) mit dem QLever-/SPARQL-1.1-Protokoll – nur für Tests,
damit MCP-Server und Label-Service-Umschreibung ohne laufenden QLever geprüft werden können.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import rdflib


def serve(graph: rdflib.Graph, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # still
            pass

        def _run(self, query: str):
            try:
                res = graph.query(query)
                body = res.serialize(format="json")
                self.send_response(200)
                self.send_header("Content-Type", "application/sparql-results+json")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:  # QLever antwortet ebenfalls mit JSON + "exception"
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"exception": str(e), "query": query}).encode())

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if "cmd" in qs:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"name-index":"mock"}')
                return
            self._run(qs.get("query", [""])[0])

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            ctype = self.headers.get("Content-Type", "")
            if "application/sparql-query" in ctype:
                query = raw
            else:
                query = parse_qs(raw).get("query", [""])[0]
            self._run(query)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"

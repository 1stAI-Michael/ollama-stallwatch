#!/usr/bin/env python3
"""OpenAI-style /v1/rerank -> TEI-native /rerank adapter for bge-reranker-v2-m3.

Coordinator (DIWA gateway) sends to TI-30 the same shape it sends to GX-10:
    POST /v1/rerank
    { "model": "bge-reranker-v2-m3", "query": "...", "documents": ["d0", "d1"] }

TEI-native (huggingface/text-embeddings-inference) accepts:
    POST /rerank
    { "query": "...", "texts": ["d0", "d1"], "raw_scores": true }

This adapter translates request/response in both directions using only the Python
stdlib (no pip install), so the container image can be a plain slim python.

Env:
    TEI_URL       upstream /rerank (default http://tei-rerank/rerank)
    TEI_HEALTH    upstream /health (default http://tei-rerank/health)
    TEI_INFO      upstream /info   (default http://tei-rerank/info)
    MODEL_NAME    fallback model name in response (default bge-reranker-v2-m3)
    PORT          listen port (default 80)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEI_URL = os.environ.get("TEI_URL", "http://tei-rerank/rerank")
TEI_HEALTH = os.environ.get("TEI_HEALTH", "http://tei-rerank/health")
TEI_INFO = os.environ.get("TEI_INFO", "http://tei-rerank/info")
MODEL_NAME = os.environ.get("MODEL_NAME", "bge-reranker-v2-m3")


def _tei_call(query: str, docs: list[str], raw_scores: bool = True,
              timeout: float = 60.0) -> list[dict]:
    body = json.dumps({"query": query, "texts": docs,
                       "raw_scores": raw_scores}).encode("utf-8")
    req = urllib.request.Request(
        TEI_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/info":
            # fA-1488 (2): weitergereicht, damit die Gegenseite unseren
            # max_client_batch_size kennt. Vorher 404 — und eine unbekannte Grenze
            # zaehlt beim Deckeln der Rerank-Stapel nicht mit (fA-1478).
            try:
                with urllib.request.urlopen(TEI_INFO, timeout=5) as r:
                    payload = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except urllib.error.HTTPError as e:
                self._send_json(e.code, {"error": "upstream /info", "detail": e.reason})
                return
            except urllib.error.URLError as e:
                self._send_json(503, {"error": f"upstream unreachable: {e}"})
                return
        if self.path in ("/health", "/healthz", "/ready"):
            try:
                with urllib.request.urlopen(TEI_HEALTH, timeout=2) as r:
                    self.send_response(r.status)
                    self.end_headers()
                    return
            except urllib.error.URLError as e:
                self._send_json(503, {"error": f"upstream unreachable: {e}"})
                return
        self._send_json(404, {"error": f"not found: {self.path}"})

    def do_POST(self) -> None:
        if self.path not in ("/v1/rerank", "/rerank"):
            self._send_json(404, {"error": f"not found: {self.path}"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return
        query = body.get("query")
        docs = body.get("documents") or body.get("texts") or []
        if not query or not docs:
            self._send_json(400, {"error": "query and documents (or texts) required"})
            return
        # fA-1488 (1): `raw_scores` wird DURCHGEREICHT statt verschluckt.
        # Michael-Entscheid 18.10.2026 (fA-1288, Teil d). Gemessen war vorher:
        # false und true gaben beide Logits, waehrend ti10/ti11 bei false Sigmoid
        # liefern — gleiche Gewichte, gleiche Software, nur der Adapter dazwischen.
        #
        # VORGABE BLEIBT ABSICHTLICH `True`, also Logits wie bisher, und nicht TEIs
        # eigene Vorgabe `false`: seit 520b958 rechnet das Gateway eine Logit-Antwort
        # selbst auf 0..1, wenn der Aufrufer nicht ausdruecklich raw_scores:true
        # schickt (fA-1288). Wuerden wir hier ohne Angabe Sigmoid liefern, rechnete
        # das Gateway ein zweites Mal um — aus 0,56 wuerde 0,64, und niemand saehe es.
        # Wer eine Skala will, sagt sie jetzt; wer nichts sagt, bekommt unveraendert
        # das, was er heute schon bekommt. Das ist eine Entscheidung, kein Versehen.
        roh = body.get("raw_scores")
        raw_scores = True if roh is None else bool(roh)
        try:
            tei_results = _tei_call(query, docs, raw_scores=raw_scores)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
            return
        except urllib.error.URLError as e:
            self._send_json(503, {"error": f"upstream unreachable: {e}"})
            return
        # Translate TEI-native [{index, score}, ...] to OpenAI/GX-10 shape.
        results = [
            {"index": r["index"], "relevance_score": r["score"]}
            for r in tei_results
        ]
        prompt_tokens = sum(len(d.split()) for d in docs) + len(query.split())
        payload = {
            "object": "list",
            "model": body.get("model") or MODEL_NAME,
            "results": results,
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }
        self._send_json(200, payload)

    def log_message(self, format: str, *args) -> None:
        # Keep stdout quiet in production; uncomment for debugging.
        return


def main() -> int:
    port = int(os.environ.get("PORT", "80"))
    print(
        f"rerank-adapter listening on 0.0.0.0:{port} -> {TEI_URL}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

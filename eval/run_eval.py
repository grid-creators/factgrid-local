#!/usr/bin/env python3
"""
run_eval.py – zwei Betriebsarten:

  --smoke     Referenz-SPARQL aus eval/questions.jsonl direkt gegen QLever ausführen
              (prüft Index + Prefix-Injektion + Label-Service-Umschreibung; kein LLM nötig)
  --agent     zusätzlich jede Frage durch agent/mini_agent.py beantworten lassen und
              Antwort, Tool-Aufrufe und Laufzeit nach eval/runs/ protokollieren

Die Checks sind absichtlich grob (Zeilenzahl, enthält ID); Ziel ist Regression, nicht Benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from factgrid_mcp import qlever  # noqa: E402


def check(rule: str, res: qlever.Result) -> bool:
    if rule == "single_number":
        return len(res.rows) == 1 and res.rows[0][0].replace("-", "").isdigit()
    if rule.startswith("rows>="):
        return len(res.rows) >= int(rule.split(">=")[1])
    if rule.startswith("contains:"):
        needle = rule.split(":", 1)[1]
        return any(needle in c for r in res.rows for c in r)
    return True


def smoke(questions: list[dict]) -> int:
    failed = 0
    for q in questions:
        if not q.get("reference_sparql"):
            continue
        try:
            res = qlever.run_query(q["reference_sparql"], limit=200, timeout_s=120)
            ok = check(q.get("check", ""), res)
            print(f"{'OK  ' if ok else 'FAIL'} {q['id']}: {len(res.rows)} Zeilen; erste: {res.rows[0] if res.rows else '-'}")
            failed += 0 if ok else 1
        except qlever.QLeverError as e:
            failed += 1
            print(f"FAIL {q['id']}: {str(e).splitlines()[0]}")
    return failed


async def agent_runs(questions: list[dict], model: str | None) -> None:
    sys.path.insert(0, str(ROOT / "agent"))
    import mini_agent  # noqa: E402
    from fastmcp import Client

    async with Client(mini_agent.SERVER) as client:
        for q in questions:
            log = await mini_agent.answer(client, q["question"], model or mini_agent.MODEL, verbose=False)
            sparqls = [c["args"].get("query") for c in log["calls"] if c["tool"] == "sparql"]
            print(f"\n=== {q['id']} ({log['seconds']} s, {len(log['calls'])} Tool-Aufrufe)\n{log.get('answer', '')[:800]}")
            if sparqls:
                print("--- letzte SPARQL ---\n" + sparqls[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--only", help="nur diese Frage-ID")
    a = ap.parse_args()
    questions = [json.loads(l) for l in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.only:
        questions = [q for q in questions if q["id"] == a.only]
    rc = 0
    if a.smoke or not a.agent:
        rc = smoke(questions)
    if a.agent:
        asyncio.run(agent_runs(questions, a.model))
    sys.exit(1 if rc else 0)


if __name__ == "__main__":
    main()

"""Сценарный eval агента первой линии: правильные инструменты, запрещённые действия, суть ответа.

Запуск: python -m scripts.eval_agent [--only id1,id2] [--judge]
Модель берётся из .env (LLM_PROVIDER / CLAUDE_MODEL) — удобно сравнивать Haiku, Opus и локальный Qwen.
"""
import argparse
import json
import re
import time

from app import agent, config, llm, ops as ops_mod

JUDGE = {"type": "object", "properties": {"score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "why": {"type": "string"}},
         "required": ["score", "why"], "additionalProperties": False}


def check(case, res, o):
    used = [t["tool"] for t in res["trace"]]
    miss = [e for e in case["expect_tools"] if not any(x in used for x in e.split("|"))]
    bad = [f for f in case["forbid_tools"] if f in used]
    ans = bool(re.search(case["expect_answer"], res["answer"], re.I))
    ho = (not case.get("expect_handoff")) or any(a["type"] == "handoff" and a["client_id"] == case["client"] for a in o.actions)
    return {"tools": used, "missing": miss, "forbidden_used": bad, "answer_ok": ans, "handoff_ok": ho, "pass": not miss and not bad and ans and ho}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--judge", action="store_true")
    a = ap.parse_args()
    cases = json.loads((config.DATA / "eval" / "agent_cases.json").read_text(encoding="utf-8"))
    if a.only:
        cases = [c for c in cases if c["id"] in a.only.split(",")]
    out = []
    for c in cases:
        o = ops_mod.reset()
        for kind, target in c["faults"]:
            o.net.inject(kind, target)
        o.tick(c["minutes"])
        t0 = time.time()
        res = agent.answer(c["client"], [{"role": "user", "content": c["message"]}])
        r = {"id": c["id"], "sec": round(time.time() - t0, 1), **check(c, res, o), "emotion": res["emotion"]["level"], "answer": res["answer"]}
        if a.judge:
            j = llm.complete_json("Ты — руководитель поддержки провайдера. Оцени ответ ИИ-специалиста клиенту по 5-балльной шкале: точность по данным, "
                                  "решение проблемы, отсутствие лишних шагов, тон. Дай короткое обоснование.",
                                  json.dumps({"сообщение": c["message"], "ответ": res["answer"], "действия": r["tools"]}, ensure_ascii=False), JUDGE)
            r["judge"] = j
        out.append(r)
        print(f"{'✅' if r['pass'] else '❌'} {c['id']:24} {r['sec']:>5}s tools={','.join(r['tools'])} "
              f"{'miss=' + str(r['missing']) if r['missing'] else ''}{' forbidden=' + str(r['forbidden_used']) if r['forbidden_used'] else ''}"
              f"{'' if r['answer_ok'] else ' answer?'}{'' if r['handoff_ok'] else ' handoff?'}{' judge=' + str(r['judge']['score']) if a.judge else ''}")
    model = config.CLAUDE_MODEL if config.PROVIDER == "claude" else config.OLLAMA_MODEL
    passed = sum(r["pass"] for r in out)
    print(f"\n{model}: {passed}/{len(out)} сценариев пройдено")
    open(f"reports/agent_eval_{model.replace(':', '_')}.json", "w").write(json.dumps({"model": model, "passed": passed, "total": len(out), "cases": out}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

"""Контроль звонков менеджеров по продажам.

Звонок → разбор по этапам продажи и скрипту (LLM) + автоматические проверки (цены, запрещённые обещания, тон с учётом
манеры клиента) → зоны риска → обратная связь менеджеру сразу → аналитика РОП со срезами по этапам воронки, скриптам
и менеджерам → РОП дописывает свою обратную связь поверх автоматической.
Состояние клиента (резкая манера, недовольство, пренебрежение) используется, чтобы правильно читать его реакцию,
а не чтобы ставить ему метки."""
import json
import re
from datetime import datetime
from itertools import count
from statistics import mean

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, llm
from . import quality
from .common import load, money_from, price_check

router = APIRouter(prefix="/api/sales", tags=["sales"])
CACHE = config.DATA / "sales" / "analysis_cache.json"
FEEDBACK = config.DATA / "runtime" / "sales_feedback.json"
SYSTEM = """Ты — опытный руководитель отдела продаж интернет-провайдера. Разбери звонок МЕНЕДЖЕРА (не клиента) по этапам продажи.
Для каждого этапа: score 2 — сделано хорошо, 1 — частично, 0 — не сделано или сделано плохо; evidence — точная цитата менеджера или «нет»;
mistake — в чём ошибка (пусто, если 2); better — как надо было сказать, одной готовой фразой.
objections — все возражения клиента: тип, цитата клиента, отработал ли менеджер (уточнил, дал аргумент или альтернативу, не давил).
missed_signals — сигналы готовности купить или важные слова клиента, которые менеджер пропустил.
feedback — обратная связь САМОМУ менеджеру на «ты», доброжелательно и конкретно: good — что получилось, fix — 1–3 главных исправления, phrase — фраза, которую стоит взять в работу.
Резкая или простая манера речи клиента — это не конфликт; оценивай, как менеджер работал с реальной ситуацией. Пиши по-русски."""


class State:
    def __init__(self):
        self.script = load("sales/script.json")
        self.calls = load("sales/calls.json")
        self.results = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        self.feedback = json.loads(FEEDBACK.read_text(encoding="utf-8")) if FEEDBACK.exists() else []
        self._ids = count(len(self.feedback) + 1)
        tariffs = load("onboarding/tariffs.json")
        self.allowed = money_from(tariffs)
        self.forbidden = load("comms/rules.json")["forbidden"]


S = State()
_mt = {}


def _refresh():
    """Подхватить результаты, которые посчитал scripts/analyze_sales.py в отдельном процессе."""
    for path, attr in ((CACHE, "results"), (FEEDBACK, "feedback")):
        if path.exists() and _mt.get(path) != path.stat().st_mtime:
            _mt[path] = path.stat().st_mtime
            setattr(S, attr, json.loads(path.read_text(encoding="utf-8")))


def stage_ids(script_id):
    return [s for s in S.script["scripts"][script_id]["stages"] if s != "compliance"]


def schema(script_id):
    ids = stage_ids(script_id)
    st = {"type": "object", "additionalProperties": False, "required": ["id", "score", "evidence", "mistake", "better"],
          "properties": {"id": {"type": "string", "enum": ids}, "score": {"type": "integer", "enum": [0, 1, 2]}, "evidence": {"type": "string"},
                         "mistake": {"type": "string"}, "better": {"type": "string"}}}
    ob = {"type": "object", "additionalProperties": False, "required": ["type", "quote", "handled"],
          "properties": {"type": {"type": "string", "enum": list(S.script["objection_types"])}, "quote": {"type": "string"}, "handled": {"type": "boolean"}}}
    fb = {"type": "object", "additionalProperties": False, "required": ["good", "fix", "phrase"],
          "properties": {"good": {"type": "string"}, "fix": {"type": "array", "items": {"type": "string"}}, "phrase": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["stages", "objections", "missed_signals", "outcome_reason", "feedback"],
            "properties": {"stages": {"type": "array", "items": st}, "objections": {"type": "array", "items": ob},
                           "missed_signals": {"type": "array", "items": {"type": "string"}}, "outcome_reason": {"type": "string"}, "feedback": fb}}


def compliance(dialog) -> dict:
    """Автоматически, без LLM: цены сверяются с прайсом, обещания — со списком запрещённых формулировок."""
    said = " ".join(t for r, t in dialog if r == "agent")
    heard = {float(x) for t in (t for r, t in dialog if r == "client") for x in re.findall(r"\d+", t)}
    fees = {t["fee"] for t in load("onboarding/tariffs.json")["tariffs"]}
    ok = S.allowed | heard | {float(abs(f - h)) for f in fees for h in heard}
    issues = [f"цена {p:g} ₽ не совпадает с прайсом" for p in price_check(said, ok)["unknown"]]
    for f in S.forbidden:
        m = re.search(f["pattern"], said.lower())
        if m:
            issues.append(f"«{m.group(0)}» — {f['why']}")
    return {"id": "compliance", "score": 0 if issues else 2, "evidence": "; ".join(issues) or "цены и обещания в порядке",
            "mistake": "; ".join(issues), "better": "называть цены и условия только по актуальному прайсу, без превосходных степеней" if issues else ""}


def analyze(call: dict) -> dict:
    sc = S.script["scripts"][call["script"]]
    stages = [s for s in S.script["stages"] if s["id"] in sc["stages"]]
    crit = "\n".join(f"- {s['id']}: {s['name']} — {s['desc']}" for s in stages if s["id"] != "compliance")
    dialog = "\n".join(f"{'Клиент' if r == 'client' else 'Менеджер'}: {t}" for r, t in call["dialog"])
    r = llm.complete_json(SYSTEM, f"Скрипт: {sc['name']}. Цель звонка: {sc['goal']}. Итог: {call['outcome']}.\nЭтапы:\n{crit}\n\nЗвонок:\n{dialog}", schema(call["script"]))
    got = {s["id"]: s for s in r["stages"]}
    r["stages"] = [got.get(i, {"id": i, "score": 0, "evidence": "нет", "mistake": "этап пропущен", "better": ""}) for i in stage_ids(call["script"])]
    if "compliance" in sc["stages"]:
        r["stages"].append(compliance(call["dialog"]))
    c = quality.conduct(call["dialog"], None, call["manager"])
    r["client_state"] = c["client"]["summary"]
    r["manager_tone_issues"] = c["issues"]
    r["risk_zones"] = [s["id"] for s in r["stages"] if s["score"] == 0] + (["tone"] if c["operator"]["hostility"] >= 1 else [])
    r["total"] = round(100 * sum(s["score"] for s in r["stages"]) / (2 * len(r["stages"])))
    r["model"] = config.CLAUDE_MODEL if config.PROVIDER == "claude" else config.OLLAMA_MODEL
    r["analyzed_at"] = datetime.now().isoformat(timespec="minutes")
    return r


def _save():
    CACHE.write_text(json.dumps(S.results, ensure_ascii=False, indent=1), encoding="utf-8")
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK.write_text(json.dumps(S.feedback, ensure_ascii=False, indent=1), encoding="utf-8")


def auto_feedback(call, r):
    """Обратная связь менеджеру уходит сразу после разбора звонка."""
    S.feedback = [f for f in S.feedback if not (f["call_id"] == call["id"] and f["author"] == "ИИ")]
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    S.feedback.append({"id": f"FB-{next(S._ids):04d}", "manager": call["manager"], "call_id": call["id"], "author": "ИИ", "at": r["analyzed_at"],
                       "risk_zones": [names.get(z, "Тон") for z in r["risk_zones"]], "good": r["feedback"]["good"], "fix": r["feedback"]["fix"],
                       "phrase": r["feedback"]["phrase"], "status": "отправлено менеджеру"})


def analyze_call(cid: str) -> dict:
    call = next((c for c in S.calls if c["id"] == cid), None)
    if not call:
        raise HTTPException(404)
    r = analyze(call)
    _refresh()
    S.results[cid] = r
    auto_feedback(call, r)
    _save()
    _mt.update({CACHE: CACHE.stat().st_mtime, FEEDBACK: FEEDBACK.stat().st_mtime})
    return r


def dashboard(manager: str | None = None, script: str | None = None) -> dict:
    """Аналитика РОП: матрицы «менеджер × этап» и «скрипт × этап», слабые звенья, возражения, конверсия."""
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    calls = [c for c in S.calls if c["id"] in S.results and (not manager or c["manager"] == manager) and (not script or c["script"] == script)]
    stage_ids_all = [s["id"] for s in S.script["stages"]]

    def matrix(key):
        rows = {}
        for c in calls:
            k = c[key] if key == "manager" else S.script["scripts"][c["script"]]["name"]
            row = rows.setdefault(k, {"calls": 0, "won": 0, "scores": {}})
            row["calls"] += 1
            row["won"] += c["outcome"] in ("сделка", "встреча")
            for s in S.results[c["id"]]["stages"]:
                row["scores"].setdefault(s["id"], []).append(s["score"])
        return {k: {"calls": v["calls"], "conversion": round(v["won"] / v["calls"], 2),
                    "stages": {sid: round(mean(v["scores"][sid]) / 2 * 100) for sid in stage_ids_all if sid in v["scores"]}} for k, v in rows.items()}

    funnel = {}
    for c in calls:
        for s in S.results[c["id"]]["stages"]:
            f = funnel.setdefault(s["id"], {"scores": [], "fails": []})
            f["scores"].append(s["score"])
            if s["score"] < 2:
                f["fails"].append({"call_id": c["id"], "manager": c["manager"], "script": S.script["scripts"][c["script"]]["name"], "score": s["score"],
                                   "evidence": s["evidence"], "mistake": s["mistake"], "better": s["better"]})
    funnel = {k: {"name": names[k], "avg": round(mean(v["scores"]) / 2 * 100), "fail_share": round(len(v["fails"]) / len(v["scores"]), 2), "fails": v["fails"]}
              for k, v in funnel.items()}
    weak = []
    for dim, mx in (("менеджер", matrix("manager")), ("скрипт", matrix("script"))):
        for who, row in mx.items():
            for sid, val in row["stages"].items():
                if val < 60:
                    weak.append({"dimension": dim, "who": who, "stage": names[sid], "stage_id": sid, "score": val})
    weak.sort(key=lambda w: w["score"])
    obj = {}
    for c in calls:
        for o in S.results[c["id"]]["objections"]:
            x = obj.setdefault(S.script["objection_types"].get(o["type"], o["type"]), {"count": 0, "handled": 0, "examples": []})
            x["count"] += 1
            x["handled"] += o["handled"]
            x["examples"].append({"call_id": c["id"], "manager": c["manager"], "quote": o["quote"], "handled": o["handled"]})
    weakest = min(funnel.items(), key=lambda kv: kv[1]["avg"]) if funnel else None
    return {"calls_analyzed": len(calls), "calls_total": len(S.calls), "stages": [{"id": s["id"], "name": s["name"]} for s in S.script["stages"]],
            "by_manager": matrix("manager"), "by_script": matrix("script"), "funnel": funnel, "weak_links": weak[:12],
            "objections": {k: {**v, "handled_share": round(v["handled"] / v["count"], 2)} for k, v in obj.items()},
            "main_problem": f"Слабее всего этап «{weakest[1]['name']}»: {weakest[1]['avg']}% от нормы, ошибки в {round(weakest[1]['fail_share'] * 100)}% звонков" if weakest else None,
            "managers": sorted({c["manager"] for c in S.calls}), "scripts": {k: v["name"] for k, v in S.script["scripts"].items()}}


class FeedbackIn(BaseModel):
    manager: str
    text: str
    call_id: str | None = None
    stage_id: str | None = None


@router.get("/calls")
def calls():
    _refresh()
    return [c | {"script_name": S.script["scripts"][c["script"]]["name"], "result": S.results.get(c["id"])} for c in S.calls]


@router.post("/calls/{cid}/analyze")
def analyze_one(cid: str):
    return analyze_call(cid)


@router.get("/dashboard")
def get_dashboard(manager: str | None = None, script: str | None = None):
    _refresh()
    return dashboard(manager or None, script or None)


@router.get("/feedback")
def get_feedback(manager: str | None = None):
    _refresh()
    return [f for f in S.feedback[::-1] if not manager or f["manager"] == manager]


@router.post("/feedback")
def add_feedback(x: FeedbackIn):
    """Обратная связь РОП — дополнение к автоматической, уходит менеджеру."""
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    f = {"id": f"FB-{next(S._ids):04d}", "manager": x.manager, "call_id": x.call_id, "author": "РОП", "at": datetime.now().isoformat(timespec="minutes"),
         "risk_zones": [names[x.stage_id]] if x.stage_id in names else [], "text": x.text, "status": "отправлено менеджеру"}
    S.feedback.append(f)
    _save()
    return f


@router.post("/feedback/{fid}/read")
def read_feedback(fid: str):
    f = next((f for f in S.feedback if f["id"] == fid), None)
    if not f:
        raise HTTPException(404)
    f["status"] = "прочитано"
    _save()
    return f

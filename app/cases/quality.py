"""Кейс «Качество обслуживания»: ИИ-контроль качества каждого диалога (чат или звонок через локальную транскрибацию)
по чек-листу с цитатами-доказательствами, сверка с оценками людей и поиск универсальных решений по типам проблем."""
import json
import re
from statistics import mean

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import emotion, llm
from .common import load

router = APIRouter(prefix="/api/quality", tags=["quality"])
SYSTEM = """Ты — эксперт по качеству клиентского сервиса интернет-провайдера. Оцени ответы СОТРУДНИКА (не клиента) по каждому критерию чек-листа:
2 — сделано хорошо, 1 — частично, 0 — не сделано или сделано плохо. evidence — короткая точная цитата сотрудника (или «нет»), fix — что конкретно надо было сказать иначе (одной фразой).
summary — 1–2 предложения: главное, что повлияло на впечатление клиента. better_reply — как надо было ответить в самый слабый момент диалога.
resolution — итог для клиента: closed (вопрос решён или есть конкретный срок и следующий шаг), partly (решено частично или нет срока), open (не решён, клиента отправили «позже» или «в другой отдел»).
Оценивай тон СОТРУДНИКА относительно реального состояния клиента: резкая или простая манера речи клиента — не агрессия и не повод отвечать холодно или делать замечания. Пиши по-русски."""


class State:
    def __init__(self):
        self.checklist = load("quality/checklist.json")
        self.dialogs = load("quality/dialogs.json")
        self.results: dict[str, dict] = {}


S = State()


def schema():
    ids = [c["id"] for c in S.checklist]
    item = {"type": "object", "properties": {"id": {"type": "string", "enum": ids}, "score": {"type": "integer", "enum": [0, 1, 2]},
                                             "evidence": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["id", "score", "evidence", "fix"], "additionalProperties": False}
    return {"type": "object", "properties": {"scores": {"type": "array", "items": item}, "summary": {"type": "string"}, "better_reply": {"type": "string"},
                                             "resolution": {"type": "string", "enum": ["closed", "partly", "open"]}, "resolution_reason": {"type": "string"}},
            "required": ["scores", "summary", "better_reply", "resolution", "resolution_reason"], "additionalProperties": False}


def client_profile(dialog) -> dict:
    """Состояние клиента по всем его репликам: манера, пиковое недовольство, пиковая враждебность с адресатом."""
    rs = [emotion.rules(t) for role, t in dialog if role == "client"]
    if not rs:
        return {}
    top = max(rs, key=lambda r: r["hostility"])
    manner = "rough" if any(r["manner"] == "rough" for r in rs) else "formal" if all(r["manner"] == "formal" for r in rs) else "neutral"
    return {"manner": emotion.MANNER[manner], "dissatisfaction": max(r["dissatisfaction"] for r in rs), "hostility": top["hostility"],
            "target": emotion.TARGET[top["target"]], "churn_risk": any(r["churn_risk"] for r in rs),
            "summary": emotion.summary(top["hostility"], max(r["dissatisfaction"] for r in rs), manner, "normal")}


TEMPLATE = r"(приносим (свои )?извинения за доставленные неудобства|ваше обращение (очень )?важно|ожидайте|в порядке очереди|обратитесь (в|по)|звоните на другой номер|это не к нам|звоните в другую|я (тут )?(вообще )?ни при ч[её]м|не надо на меня кричать|посмотрите на сайте|там вс[её] написано)"


def conduct(dialog: list, utterances: list | None = None, operator: str | None = None) -> dict:
    """Разбор без LLM: реальное состояние клиента по каждой реплике, поведение сотрудника и его реакция
    на недовольного или резкого клиента. Голос сотрудника — относительно его собственной нормы."""
    feats = utterances or [None] * len(dialog)
    rows = []
    for (role, text), u in zip(dialog, feats):
        v = {k: u[k] for k in ("loudness_db", "words_per_sec", "pitch_std_st") if u and u.get(k) is not None} or None
        key = (f"op:{operator}" if role == "agent" else None) if operator else None
        e = emotion.analyze(text, v, use_llm=False, client_id=key)
        rows.append({"role": role, "text": text, "manner": e["manner"], "dissatisfaction": e["dissatisfaction"], "hostility": e["hostility"],
                     "target": e["target"], "arousal": e["arousal"]["level"], "summary": e["summary"], "template": role == "agent" and bool(re.search(TEMPLATE, text, re.I))})
    cl = [r for r in rows if r["role"] == "client"]
    op = [r for r in rows if r["role"] == "agent"]
    reactions = []
    for i, r in enumerate(rows):
        if r["role"] != "client" or (r["hostility"] < 2 and r["dissatisfaction"] < 2):
            continue
        nxt = next((x for x in rows[i + 1:] if x["role"] == "agent"), None)
        if nxt:
            kind = "ответил резкостью" if nxt["hostility"] >= 1 else "шаблонная отписка" if nxt["template"] else "спокойно и по делу"
            reactions.append({"client": r["text"], "operator": nxt["text"], "client_state": r["summary"], "reaction": kind, "ok": kind == "спокойно и по делу"})
    c_host = max((r["hostility"] for r in cl), default=0)
    o_host = max((r["hostility"] for r in op), default=0)
    issues = []
    if o_host >= 1:
        issues.append("оператор сам допускает резкость или пренебрежение")
    if any(r["arousal"] == "high" for r in op):
        issues.append("оператор повышает голос относительно своей обычной манеры")
    if any(not x["ok"] for x in reactions):
        issues.append("на недовольство клиента ответил не по делу")
    return {"utterances": rows, "client": {"manner": emotion.MANNER["rough" if any(r["manner"] == "rough" for r in cl) else "neutral"],
                                           "dissatisfaction": max((r["dissatisfaction"] for r in cl), default=0), "hostility": c_host,
                                           "summary": emotion.summary(c_host, max((r["dissatisfaction"] for r in cl), default=0),
                                                                      "rough" if any(r["manner"] == "rough" for r in cl) else "neutral", "normal")},
            "operator": {"name": operator, "hostility": o_host, "templates": sum(r["template"] for r in op), "raised_voice": any(r["arousal"] == "high" for r in op)},
            "reactions": reactions, "issues": issues}


def render(dialog):
    return "\n".join(f"{'Клиент' if r == 'client' else 'Сотрудник'}: {t}" for r, t in dialog)


def parse_text(text: str):
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(К|Клиент|О|Оператор|С|Сотрудник|Бот)\s*:\s*(.+)", line, re.I)
        if m:
            out.append(["client" if m.group(1).lower().startswith("к") else "agent", m.group(2).strip()])
        elif out and line.strip():
            out[-1][1] += " " + line.strip()
    return out


def score(dialog: list, operator: str | None = None, utterances: list | None = None) -> dict:
    crit = "\n".join(f"- {c['id']}: {c['name']} — {c['desc']}" for c in S.checklist)
    r = llm.complete_json(SYSTEM, f"Чек-лист:\n{crit}\n\nДиалог:\n{render(dialog)}", schema())
    got = {s["id"]: s for s in r["scores"]}
    r["scores"] = [{**got.get(c["id"], {"id": c["id"], "score": 0, "evidence": "не оценено", "fix": ""}), "name": c["name"]} for c in S.checklist]
    r["total"] = round(100 * sum(s["score"] for s in r["scores"]) / (2 * len(S.checklist)))
    r["client"] = client_profile(dialog)
    r["conduct"] = conduct(dialog, utterances, operator)
    return r


def agreement(results: dict) -> dict:
    pairs = [(d["labels"][c["id"]], results[d["id"]]["scores"][i]["score"]) for d in S.dialogs if d["id"] in results and d.get("labels")
             for i, c in enumerate(S.checklist)]
    if not pairs:
        return {}
    by = {c["id"]: [abs(d["labels"][c["id"]] - results[d["id"]]["scores"][i]["score"]) for d in S.dialogs if d["id"] in results]
          for i, c in enumerate(S.checklist)}
    return {"exact": round(mean(a == b for a, b in pairs), 3), "within_1": round(mean(abs(a - b) <= 1 for a, b in pairs), 3),
            "mae_by_criterion": {k: round(mean(v), 2) for k, v in by.items() if v}, "n_dialogs": len({d["id"] for d in S.dialogs if d["id"] in results})}


def insights() -> dict:
    groups = {}
    for d in S.dialogs:
        if d["id"] in S.results:
            groups.setdefault(d["problem"], []).append((S.results[d["id"]]["total"], d))
    payload = [{"problem": p, "best": render(max(g, key=lambda x: x[0])[1]["dialog"]), "best_score": max(g, key=lambda x: x[0])[0],
                "worst": render(min(g, key=lambda x: x[0])[1]["dialog"]), "worst_score": min(g, key=lambda x: x[0])[0]} for p, g in groups.items()]
    sch = {"type": "object", "properties": {"solutions": {"type": "array", "items": {"type": "object", "properties": {
        "problem": {"type": "string"}, "universal_solution": {"type": "string"}, "script": {"type": "string"}, "anti_pattern": {"type": "string"}},
        "required": ["problem", "universal_solution", "script", "anti_pattern"], "additionalProperties": False}}},
        "required": ["solutions"], "additionalProperties": False}
    r = llm.complete_json("Ты — руководитель клиентского сервиса. Для каждого типа проблемы сравни лучший и худший диалог и выведи универсальное решение: "
                          "что делать в каждом таком случае (universal_solution), готовую фразу-скрипт для сотрудников и бота (script) и чего делать нельзя (anti_pattern). По-русски, кратко.",
                          json.dumps(payload, ensure_ascii=False), sch)
    by_crit = {c["name"]: round(mean(S.results[k]["scores"][i]["score"] for k in S.results) / 2 * 100) for i, c in enumerate(S.checklist)}
    by_agent = {}
    for d in S.dialogs:
        if d["id"] in S.results:
            by_agent.setdefault(d["agent"], []).append(S.results[d["id"]]["total"])
    return {"solutions": r["solutions"], "by_criterion": by_crit, "by_agent": {a: round(mean(v)) for a, v in by_agent.items()}}


class DialogIn(BaseModel):
    text: str | None = None
    dialog: list[list[str]] | None = None
    utterances: list[dict] | None = None
    operator: str | None = None


def operators() -> dict:
    """Сводка для РОП по каждому оператору: без LLM по всем диалогам, плюс итог чек-листа там, где ИИ уже оценил."""
    by = {}
    for d in S.dialogs:
        c = conduct(d["dialog"], None, d["agent"])
        o = by.setdefault(d["agent"], {"dialogs": 0, "scores": [], "closed": 0, "scored": 0, "own_rudeness": 0, "templates": 0,
                                       "hard_clients": 0, "calm_reactions": 0, "issues": []})
        o["dialogs"] += 1
        o["own_rudeness"] += c["operator"]["hostility"] >= 1
        o["templates"] += c["operator"]["templates"] > 0
        o["hard_clients"] += len(c["reactions"])
        o["calm_reactions"] += sum(x["ok"] for x in c["reactions"])
        o["issues"] += [f"{d['id']}: {x}" for x in c["issues"]]
        r = S.results.get(d["id"])
        if r:
            o["scored"] += 1
            o["scores"].append(r["total"])
            o["closed"] += r.get("resolution") == "closed"
    for o in by.values():
        o["avg_score"] = round(sum(o["scores"]) / len(o["scores"])) if o["scores"] else None
        o["closed_share"] = round(o["closed"] / o["scored"], 2) if o["scored"] else None
        o["calm_share"] = round(o["calm_reactions"] / o["hard_clients"], 2) if o["hard_clients"] else None
        del o["scores"]
    return by


@router.get("/dialogs")
def dialogs():
    return {"checklist": S.checklist, "agreement": agreement(S.results),
            "dialogs": [d | {"result": S.results.get(d["id"]), "conduct": conduct(d["dialog"], None, d["agent"])} for d in S.dialogs]}


@router.get("/operators")
def get_operators():
    return operators()


@router.post("/score/{did}")
def score_one(did: str):
    d = next((d for d in S.dialogs if d["id"] == did), None)
    if not d:
        raise HTTPException(404)
    S.results[did] = score(d["dialog"], d["agent"])
    return {"result": S.results[did], "agreement": agreement(S.results)}


@router.post("/score")
def score_custom(x: DialogIn):
    dialog = x.dialog or parse_text(x.text or "")
    if not dialog:
        raise HTTPException(400, "диалог пуст: строки вида «К: …» и «О: …»")
    return {"dialog": dialog, "result": score(dialog, x.operator, x.utterances)}


@router.post("/conduct")
def conduct_only(x: DialogIn):
    """Быстрый разбор без LLM: состояние клиента и поведение сотрудника."""
    dialog = x.dialog or parse_text(x.text or "")
    if not dialog:
        raise HTTPException(400, "диалог пуст")
    return conduct(dialog, x.utterances, x.operator)


@router.post("/insights")
def get_insights():
    if not S.results:
        raise HTTPException(400, "сначала оцените диалоги")
    return insights()

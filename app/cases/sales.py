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
from . import experiments, quality, review
from .common import load, money_from, price_check

router = APIRouter(prefix="/api/sales", tags=["sales"])
CACHE = config.DATA / "sales" / "analysis_cache.json"
FEEDBACK = config.DATA / "runtime" / "sales_feedback.json"
UPLOADED = config.DATA / "runtime" / "sales_calls.json"
SYSTEM = """Ты — опытный руководитель отдела продаж интернет-провайдера. Разбери звонок МЕНЕДЖЕРА (не клиента) по этапам продажи.
Для каждого этапа: score 2 — сделано хорошо, 1 — частично, 0 — не сделано или сделано плохо; evidence — точная цитата менеджера или «нет»;
mistake — в чём ошибка (пусто, если 2); better — как надо было сказать, одной готовой фразой.
objections — все возражения клиента: тип, цитата клиента, отработал ли менеджер (уточнил, дал аргумент или альтернативу, не давил).
missed_signals — сигналы готовности купить или важные слова клиента, которые менеджер пропустил.
feedback — обратная связь САМОМУ менеджеру на «ты», доброжелательно и конкретно: good — что получилось, fix — 1–3 главных исправления, phrase — фраза, которую стоит взять в работу.
goal_result — достигнута ли цель скрипта: achieved, partly, failed. close_type — чем закончился звонок: deal (сделка/запись), meeting (встреча/выезд),
callback_dated (перезвон в конкретную дату/время), callback_vague («перезвоню», «подумаю» без даты), no_next_step (разошлись без договорённости), refused (клиент отказался).
style — стиль МЕНЕДЖЕРА 0–2: pressure (давит, спорит, навязывает), passivity (не предлагает, не ведёт к следующему шагу, отправляет «на сайт»), composure (уравновешенность: 2 — спокоен и держит разговор даже с резким клиентом, 0 — огрызается, оправдывается, раздражается).
wrong_phrases — дословные неудачные фразы менеджера: quote, why (чем вредит продаже), better (как сказать).
Резкая или простая манера речи клиента — это не конфликт; оценивай, как менеджер работал с реальной ситуацией. Пиши по-русски."""


GOAL = {"achieved": "цель достигнута", "partly": "частично", "failed": "не достигнута"}
CLOSE = {"deal": "сделка / запись", "meeting": "встреча / выезд", "callback_dated": "перезвон с датой", "callback_vague": "«перезвоню» без даты",
         "no_next_step": "без следующего шага", "refused": "отказ клиента"}
SEGMENTS = {"hostile": "враждебные клиенты", "agitated": "резкие / на эмоциях", "calm": "спокойные"}
TRAINING = {
    "contact": "Скрипт входа в контакт: представиться, компания, цель, «удобно ли»",
    "needs": "Тренинг «Выявление потребностей»: 2–3 открытых вопроса до любого предложения",
    "offer": "Презентация через пользу: одно предложение под потребность, без чтения прайса",
    "objections": "Работа с возражениями «дорого», «подумаю», «уже есть провайдер»",
    "closing": "Закрытие на следующий шаг: всегда предлагать конкретную дату и время",
    "compliance": "Знание прайса и условий акций; запрет на «лучший», «гарантируем»",
    "calmer": "Наставничество: самоконтроль в конфликтных звонках — разбор 3 записей с наставником, техника «присоединиться и перейти к делу»",
    "salesier": "Продажный тон: брать инициативу, предлагать решение и следующий шаг, не отправлять клиента «на сайт»",
    "hostile": "Работа со сложными клиентами: не оправдываться и не огрызаться, признать проблему и дать конкретику",
}


class State:
    def __init__(self):
        self.script = load("sales/script.json")
        self.calls = load("sales/calls.json") + (json.loads(UPLOADED.read_text(encoding="utf-8")) if UPLOADED.exists() else [])
        self.raw = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        self.results = dict(self.raw)  # эффективный разбор: ИИ + калибровка + решения РОП (см. rebuild)
        self.feedback = json.loads(FEEDBACK.read_text(encoding="utf-8")) if FEEDBACK.exists() else []
        self._ids = count(len(self.feedback) + 1)
        tariffs = load("onboarding/tariffs.json")
        self.allowed = money_from(tariffs)
        self.forbidden = load("comms/rules.json")["forbidden"]
        self.teams = load("sales/teams.json")
        self.practice = load("sales/practice.json")["scenarios"]
        self.sessions: dict[str, dict] = {}


S = State()
_mt = {}


def _seed_feedback():
    """На чистой копии репозитория лента обратной связи собирается из сохранённых разборов."""
    if S.feedback or not S.results:
        return
    for c in S.calls:
        if c["id"] in S.results:
            auto_feedback(c, S.results[c["id"]])


def rebuild():
    """Разбор, который видят все роли: исходный разбор ИИ + калибровка агрессии под людей + решения РОП."""
    calls = {c["id"]: c for c in S.calls}
    S.results = {cid: review.apply(calls[cid], r) if cid in calls else r for cid, r in S.raw.items()}


def _refresh():
    """Подхватить результаты, которые посчитал scripts/analyze_sales.py в отдельном процессе, и решения по спорам."""
    changed = False
    for path, attr in ((CACHE, "raw"), (FEEDBACK, "feedback"), (review.PATH, None)):
        if path.exists() and _mt.get(path) != path.stat().st_mtime:
            _mt[path] = path.stat().st_mtime
            if attr:
                setattr(S, attr, json.loads(path.read_text(encoding="utf-8")))
            elif review.R is not None:
                review.R.update(review._load())
                review._cal["key"] = None
            changed |= attr != "feedback"
    if changed:
        rebuild()


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
    z = {"type": "integer", "enum": [0, 1, 2]}
    sty = {"type": "object", "additionalProperties": False, "required": ["pressure", "passivity", "composure"], "properties": {"pressure": z, "passivity": z, "composure": z}}
    wp = {"type": "object", "additionalProperties": False, "required": ["quote", "why", "better"],
          "properties": {"quote": {"type": "string"}, "why": {"type": "string"}, "better": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["stages", "objections", "missed_signals", "outcome_reason", "feedback", "goal_result", "close_type", "style", "wrong_phrases"],
            "properties": {"stages": {"type": "array", "items": st}, "objections": {"type": "array", "items": ob},
                           "missed_signals": {"type": "array", "items": {"type": "string"}}, "outcome_reason": {"type": "string"}, "feedback": fb,
                           "goal_result": {"type": "string", "enum": list(GOAL)}, "close_type": {"type": "string", "enum": list(CLOSE)},
                           "style": sty, "wrong_phrases": {"type": "array", "items": wp}}}


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
    r = llm.complete_json(SYSTEM + review.precedents_prompt(call["script"]),
                          f"Скрипт: {sc['name']}. Цель звонка: {sc['goal']}. Итог: {call['outcome']}.\nЭтапы:\n{crit}\n\nЗвонок:\n{dialog}", schema(call["script"]))
    got = {s["id"]: s for s in r["stages"]}
    r["stages"] = [got.get(i, {"id": i, "score": 0, "evidence": "нет", "mistake": "этап пропущен", "better": ""}) for i in stage_ids(call["script"])]
    if "compliance" in sc["stages"]:
        r["stages"].append(compliance(call["dialog"]))
    c = quality.conduct(call["dialog"], None, call["manager"])
    r["client_state"] = c["client"]["summary"]
    cl = c["client"]
    r["client_segment"] = "hostile" if cl["hostility"] >= 2 else "agitated" if cl["manner"] == "простая, резкая" or cl["dissatisfaction"] >= 2 else "calm"
    r["manager_tone_issues"] = c["issues"]
    st = dict(r["style"])
    if c["operator"]["hostility"] >= 1:
        st["composure"] = min(st["composure"], 0 if c["operator"]["hostility"] >= 2 else 1)
    if c["operator"]["templates"] and st["passivity"] < 1:
        st["passivity"] = 1
    r["style"] = st
    r["style_verdict"] = verdict(st)
    r["risk_zones"] = [s["id"] for s in r["stages"] if s["score"] == 0] + (["tone"] if st["composure"] == 0 else [])
    r["total"] = round(100 * sum(s["score"] for s in r["stages"]) / (2 * len(r["stages"])))
    r["precedents_used"] = len(review.precedents(call["script"]))
    r["model"] = config.CLAUDE_MODEL if config.PROVIDER == "claude" else config.OLLAMA_MODEL
    r["analyzed_at"] = datetime.now().isoformat(timespec="minutes")
    return r


OPEN_Q = r"\b(что|как|какой|какая|какие|каком|сколько|почему|зачем|для чего|расскажите|подскажите|чем|где|когда)\b[^?]*\?"


def acoustics(u: list | None) -> dict:
    """Звук разговора по стереозаписи с таймкодами: перебивания (наложение реплик), паузы, повышения голоса
    относительно обычной громкости того же человека в этом звонке, пики в дБ."""
    if not u:
        return {}
    u = sorted(u, key=lambda x: x["start"])
    who = lambda x: "manager" if x["role"] in ("operator", "agent") else "client"
    inter, overlap, pauses, resp = {"manager": 0, "client": 0}, 0.0, 0, []
    for i, x in enumerate(u):
        prev = [p for p in u[:i] if who(p) != who(x) and p["start"] < x["start"]]
        talking = [p for p in prev if p["end"] - 0.3 > x["start"]]
        if talking:
            inter[who(x)] += 1
            overlap += min(max(p["end"] for p in talking), x["end"]) - x["start"]
        if i:
            gap = x["start"] - max(p["end"] for p in u[:i])
            pauses += gap > 3
            if who(x) == "manager" and who(u[i - 1]) == "client" and gap > 0:
                resp.append(gap)
    loud, peak = {}, {}
    for role in ("manager", "client"):
        xs = [x["loudness_db"] for x in u if who(x) == role and x.get("loudness_db") is not None]
        if xs:
            med = sorted(xs)[len(xs) // 2]
            loud[role] = sum(v > med + 6 for v in xs)
            peak[role] = max(x.get("loudness_peak_db", x["loudness_db"]) for x in u if who(x) == role and x.get("loudness_db") is not None)
    return {"interruptions": inter, "overlap_s": round(overlap, 1), "long_pauses": pauses, "raised_voice": loud, "peak_db": peak,
            "manager_response_s": round(mean(resp), 1) if resp else None}


def metrics(call: dict) -> dict:
    """Метрики разговора без LLM. Если есть реальные отметки времени из стереозаписи — по ним, иначе оценка по темпу речи 2,3 слова/с."""
    u = call.get("utterances")
    words = lambda role: sum(len(t.split()) for r, t in call["dialog"] if r == role)
    mw, cw = words("agent"), words("client")
    if u:
        dur = max(x["end"] for x in u)
        m_time = sum(x["end"] - x["start"] for x in u if x["role"] == "operator")
        c_time = sum(x["end"] - x["start"] for x in u if x["role"] == "client")
        share = m_time / max(m_time + c_time, 1e-9)
    else:
        dur = (mw + cw) / 2.3 + len(call["dialog"]) * 1.0
        share = mw / max(mw + cw, 1)
    agent = [t for r, t in call["dialog"] if r == "agent"]
    return {"duration_s": round(dur), "manager_talk_share": round(share, 2), "manager_words": mw, "client_words": cw, "turns": len(call["dialog"]),
            "questions": sum(t.count("?") for t in agent), "open_questions": sum(len(re.findall(OPEN_Q, t, re.I)) for t in agent),
            "longest_monologue_words": max((len(t.split()) for t in agent), default=0), "estimated": not u, "acoustics": acoustics(u)}


def team_of(supervisor: str | None) -> set | None:
    if not supervisor:
        return None
    t = next((x for x in S.teams["supervisors"] if x["name"] == supervisor), None)
    return set(t["managers"]) if t else set()


def supervisor_of(manager: str) -> str | None:
    return next((x["name"] for x in S.teams["supervisors"] if manager in x["managers"]), None)


def direction(call):
    return S.script["scripts"][call["script"]].get("direction", "B2C")


def verdict(st: dict) -> str:
    if st["composure"] <= 0.7 or st["pressure"] >= 1.3:
        return "calmer"
    if st["passivity"] >= 1.3:
        return "salesier"
    return "balanced"


VERDICT = {"calmer": "нужно спокойнее: давит или теряет самообладание", "salesier": "нужен более продажный тон: пассивен, не ведёт к сделке",
           "balanced": "уравновешен и ведёт разговор"}


def profile(manager: str) -> dict:
    """Полный профиль менеджера для решения РОП: результат по типам клиентов, закрытие задач, стиль, ошибки, план обучения."""
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    calls = [c for c in S.calls if c["manager"] == manager and c["id"] in S.results]
    if not calls:
        return {"manager": manager, "calls": 0}
    R = [S.results[c["id"]] for c in calls]
    won = lambda cs: round(sum(c["outcome"] in ("сделка", "встреча") for c in cs) / len(cs), 2) if cs else None
    seg = {}
    for c, r in zip(calls, R):
        x = seg.setdefault(r.get("client_segment", "calm"), {"calls": [], "scores": [], "composure": []})
        x["calls"].append(c)
        x["scores"].append(r["total"])
        x["composure"].append(r["style"]["composure"])
    segments = {SEGMENTS[k]: {"calls": len(v["calls"]), "conversion": won(v["calls"]), "avg_score": round(mean(v["scores"])),
                              "composure": round(mean(v["composure"]) / 2 * 100), "ids": [c["id"] for c in v["calls"]]} for k, v in seg.items()}
    style = {k: round(mean(r["style"][k] for r in R), 2) for k in ("pressure", "passivity", "composure")}
    v = verdict(style)
    stage_avg = {sid: round(mean(s["score"] for r in R for s in r["stages"] if s["id"] == sid) / 2 * 100)
                 for sid in names if any(s["id"] == sid for r in R for s in r["stages"])}
    plan = [{"key": k, "title": TRAINING[k], "why": f"этап «{names[k]}» — {val}% от нормы"} for k, val in sorted(stage_avg.items(), key=lambda kv: kv[1]) if val < 60]
    if v in ("calmer", "salesier"):
        plan.insert(0, {"key": v, "title": TRAINING[v], "why": VERDICT[v]})
    hs = seg.get("hostile")
    if hs and (mean(hs["composure"]) < 1.5 or won(hs["calls"]) == 0):
        plan.append({"key": "hostile", "title": TRAINING["hostile"], "why": f"со сложными клиентами самообладание {round(mean(hs['composure']) / 2 * 100)}%"})
    dirs = {}
    for c, r in zip(calls, R):
        x = dirs.setdefault(direction(c), {"calls": [], "scores": []})
        x["calls"].append(c)
        x["scores"].append(r["total"])
    by_dir = {k: {"calls": len(v["calls"]), "conversion": won(v["calls"]), "avg_score": round(mean(v["scores"])),
                  "weak_stage": min(((names[sid], round(mean(st["score"] for c in v["calls"] for st in S.results[c["id"]]["stages"] if st["id"] == sid) / 2 * 100))
                                     for sid in names if any(st["id"] == sid for c in v["calls"] for st in S.results[c["id"]]["stages"])), key=lambda t: t[1])}
              for k, v in dirs.items()}
    M = [metrics(c) for c in calls]
    talk = {"avg_duration_s": round(mean(m["duration_s"] for m in M)), "manager_talk_share": round(mean(m["manager_talk_share"] for m in M), 2),
            "open_questions_per_call": round(mean(m["open_questions"] for m in M), 1), "longest_monologue_words": max(m["longest_monologue_words"] for m in M),
            "estimated": all(m["estimated"] for m in M)}
    return {"manager": manager, "calls": len(calls), "conversion": won(calls), "avg_score": round(mean(r["total"] for r in R)),
            "directions": by_dir, "talk": talk,
            "goal": {GOAL[g]: sum(r.get("goal_result") == g for r in R) for g in GOAL},
            "close": {CLOSE[t]: sum(r.get("close_type") == t for r in R) for t in CLOSE if any(r.get("close_type") == t for r in R)},
            "segments": segments, "style": {k: round(val / 2 * 100) for k, val in style.items()}, "verdict": v, "verdict_text": VERDICT[v],
            "stages": stage_avg,
            "mistakes": [{"call_id": c["id"], "stage": names[s["id"]], "quote": s["evidence"], "mistake": s["mistake"], "better": s["better"]}
                         for c, r in zip(calls, R) for s in r["stages"] if s["score"] == 0][:12],
            "wrong_phrases": [{"call_id": c["id"], **w} for c, r in zip(calls, R) for w in r.get("wrong_phrases", [])][:10],
            "missed_signals": [{"call_id": c["id"], "signal": m} for c, r in zip(calls, R) for m in r.get("missed_signals", [])][:8],
            "plan": plan, "assigned": [f for f in S.feedback if f["manager"] == manager and f.get("kind") == "обучение"],
            "contributions": experiments.credits(manager)}


def team(supervisor: str | None = None) -> list:
    who = team_of(supervisor)
    return [profile(m) for m in sorted({c["manager"] for c in S.calls}) if who is None or m in who]


def department(supervisor: str | None = None) -> dict:
    """Слабые места всего отдела: этапы, направления, типы клиентов, стиль, метрики разговора.
    Каждый пункт раскрывается в список самых слабых менеджеров по нему с метриками разговора."""
    names = {x["id"]: x["name"] for x in S.script["stages"]}
    who = team_of(supervisor)
    profs = [p for p in team(supervisor) if p["calls"]]
    calls = [c for c in S.calls if c["id"] in S.results and (who is None or c["manager"] in who)]
    if not calls:
        return {"items": [], "managers": []}
    items = []

    def add(key, title, value, unit, rank, why):
        items.append({"key": key, "title": title, "value": value, "unit": unit, "why": why,
                      "managers": sorted([{"manager": p["manager"], "value": rank(p), "calls": p["calls"], "conversion": p["conversion"], "verdict": p["verdict_text"],
                                           "talk": p["talk"], "style": p["style"]} for p in profs if rank(p) is not None], key=lambda x: x["value"])})

    for sid, nm in names.items():
        vals = [st["score"] for c in calls for st in S.results[c["id"]]["stages"] if st["id"] == sid]
        if vals:
            add(f"stage:{sid}", f"Этап «{nm}»", round(mean(vals) / 2 * 100), "%", lambda p, sid=sid: p["stages"].get(sid), "доля от нормы по этапу")
    for sk, sn in SEGMENTS.items():
        cs = [c for c in calls if S.results[c["id"]].get("client_segment") == sk]
        if cs:
            add(f"seg:{sk}", f"Работа с клиентами: {sn}", round(mean(S.results[c["id"]]["style"]["composure"] for c in cs) / 2 * 100), "% самообладание",
                lambda p, sn=sn: p["segments"][sn]["composure"] if sn in p["segments"] else None, "уравновешенность менеджера с такими клиентами")
    add("style:pressure", "Давление на клиента", round(mean(p["style"]["pressure"] for p in profs)), "% (меньше — лучше)", lambda p: -p["style"]["pressure"], "чем выше, тем сильнее давит")
    add("style:passivity", "Пассивность (не ведёт к сделке)", round(mean(p["style"]["passivity"] for p in profs)), "% (меньше — лучше)", lambda p: -p["style"]["passivity"], "")
    M = [metrics(c) for c in calls]
    add("talk:share", "Менеджер говорит больше клиента", round(mean(m["manager_talk_share"] for m in M) * 100), "% речи менеджера",
        lambda p: -round(p["talk"]["manager_talk_share"] * 100), "норма продажи — 40–60%: больше — монолог вместо диалога")
    add("talk:questions", "Мало открытых вопросов", round(mean(m["open_questions"] for m in M), 1), "вопросов на звонок",
        lambda p: p["talk"]["open_questions_per_call"], "без вопросов нет выявления потребностей")
    bad = lambda it: it["value"] if "%" in it["unit"] and "меньше" not in it["unit"] and "речи" not in it["unit"] else 100 - it["value"] if "меньше" in it["unit"] else 100 - abs(it["value"] - 50) * 2 if "речи" in it["unit"] else it["value"] * 25
    for it in items:
        it["severity"] = round(bad(it))
    items.sort(key=lambda it: it["severity"])
    return {"items": items, "calls": len(calls), "supervisor": supervisor}


def rop_summary() -> dict:
    """Краткая сводка для РОП: одна строка на супервайзера и его команду + главные проблемы отдела."""
    rows = []
    for t in S.teams["supervisors"]:
        ps = [p for p in team(t["name"]) if p["calls"]]
        if not ps:
            continue
        d = department(t["name"])
        calls = sum(p["calls"] for p in ps)
        rows.append({"supervisor": t["name"], "managers": len(t["managers"]), "calls": calls,
                     "conversion": round(sum(p["conversion"] * p["calls"] for p in ps) / calls, 2),
                     "avg_score": round(sum(p["avg_score"] * p["calls"] for p in ps) / calls),
                     "main_problem": d["items"][0]["title"] if d["items"] else None,
                     "attention": [{"manager": p["manager"], "why": p["verdict_text"] if p["verdict"] != "balanced" else f"средний балл {p['avg_score']}%"}
                                   for p in ps if p["verdict"] != "balanced" or p["avg_score"] < 50],
                     "trainings": sum(len(p["assigned"]) for p in ps),
                     "feedback_sent": sum(1 for f in S.feedback if f["manager"] in t["managers"])})
    dep = department()
    top = dep["items"][:3]
    best = max(rows, key=lambda r: r["conversion"]) if rows else None
    return {"rop": S.teams["rop"], "teams": rows, "top_problems": [{"title": i["title"], "value": i["value"], "unit": i["unit"],
                                                                       "worst": i["managers"][0]["manager"] if i["managers"] else None} for i in top],
            "headline": (f"Главная проблема отдела — {top[0]['title'].lower()}. Лучшая конверсия у команды «{best['supervisor']}» ({round(best['conversion'] * 100)}%)."
                         if top and best else None), "calls": dep["calls"]}


def _save():
    CACHE.write_text(json.dumps(S.raw, ensure_ascii=False, indent=1), encoding="utf-8")
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK.write_text(json.dumps(S.feedback, ensure_ascii=False, indent=1), encoding="utf-8")


def auto_feedback(call, r):
    """Обратная связь менеджеру уходит сразу после разбора звонка."""
    S.feedback = [f for f in S.feedback if not (f["call_id"] == call["id"] and f["author"] == "ИИ")]
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    S.feedback.append({"id": f"FB-{next(S._ids):04d}", "manager": call["manager"], "call_id": call["id"], "author": "ИИ", "at": r["analyzed_at"],
                       "risk_zones": [names.get(z, "Тон") for z in r["risk_zones"]], "good": r["feedback"]["good"], "fix": r["feedback"]["fix"],
                       "phrase": r["feedback"]["phrase"], "style": VERDICT.get(r.get("style_verdict"), ""),
                       "wrong_phrases": r.get("wrong_phrases", [])[:3], "status": "отправлено менеджеру"})


def analyze_call(cid: str) -> dict:
    call = next((c for c in S.calls if c["id"] == cid), None)
    if not call:
        raise HTTPException(404)
    r = analyze(call)
    _refresh()
    S.raw[cid] = r
    rebuild()
    r = S.results[cid]
    auto_feedback(call, r)
    experiments.ingest_call(call, r, S.script)
    _save()
    _mt.update({CACHE: CACHE.stat().st_mtime, FEEDBACK: FEEDBACK.stat().st_mtime})
    return r


def dashboard(manager: str | None = None, script: str | None = None, supervisor: str | None = None) -> dict:
    """Аналитика РОП: матрицы «менеджер × этап» и «скрипт × этап», слабые звенья, возражения, конверсия."""
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    who = team_of(supervisor)
    calls = [c for c in S.calls if c["id"] in S.results and (not manager or c["manager"] == manager) and (not script or c["script"] == script)
             and (who is None or c["manager"] in who)]
    stage_ids_all = [s["id"] for s in S.script["stages"]]

    def matrix(key):
        rows = {}
        for c in calls:
            k = c[key] if key == "manager" else direction(c) if key == "direction" else S.script["scripts"][c["script"]]["name"]
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
        for name, row in mx.items():
            for sid, val in row["stages"].items():
                if val < 60:
                    weak.append({"dimension": dim, "who": name, "stage": names[sid], "stage_id": sid, "score": val})
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
            "by_manager": matrix("manager"), "by_script": matrix("script"), "by_direction": matrix("direction"), "funnel": funnel, "weak_links": weak[:12],
            "objections": {k: {**v, "handled_share": round(v["handled"] / v["count"], 2)} for k, v in obj.items()},
            "main_problem": f"Слабее всего этап «{weakest[1]['name']}»: {weakest[1]['avg']}% от нормы, ошибки в {round(weakest[1]['fail_share'] * 100)}% звонков" if weakest else None,
            "managers": sorted({c["manager"] for c in S.calls if who is None or c["manager"] in who}), "scripts": {k: v["name"] for k, v in S.script["scripts"].items()}}


class CallIn(BaseModel):
    manager: str
    script: str
    outcome: str = "без итога"
    dialog: list
    utterances: list | None = None


def add_call(x: CallIn) -> dict:
    """Реальный звонок: результат /api/transcribe_call (стерео → реплики с таймкодами и громкостью) + кто и по какому скрипту."""
    if x.script not in S.script["scripts"]:
        raise HTTPException(400, "нет такого скрипта")
    ids = [c for c in S.calls if c["id"].startswith("R")]
    c = {"id": f"R{len(ids) + 1:03d}", "manager": x.manager, "script": x.script, "date": datetime.now().date().isoformat(), "outcome": x.outcome,
         "dialog": [list(d) for d in x.dialog], **({"utterances": x.utterances} if x.utterances else {})}
    S.calls.append(c)
    UPLOADED.parent.mkdir(parents=True, exist_ok=True)
    UPLOADED.write_text(json.dumps([c for c in S.calls if c["id"].startswith("R")], ensure_ascii=False, indent=1), encoding="utf-8")
    return c


class AssignIn(BaseModel):
    manager: str
    author: str = "Супервайзер"
    key: str
    comment: str = ""


class FeedbackIn(BaseModel):
    manager: str
    author: str = "Супервайзер"
    text: str
    call_id: str | None = None
    stage_id: str | None = None


rebuild()
_seed_feedback()


@router.get("/calls")
def calls():
    _refresh()
    return [c | {"script_name": S.script["scripts"][c["script"]]["name"], "result": S.results.get(c["id"])} for c in S.calls]


@router.post("/calls")
def post_call(x: CallIn):
    return add_call(x)


@router.post("/calls/{cid}/analyze")
def analyze_one(cid: str):
    return analyze_call(cid)


@router.get("/dashboard")
def get_dashboard(manager: str | None = None, script: str | None = None, supervisor: str | None = None):
    _refresh()
    return dashboard(manager or None, script or None, supervisor or None)


@router.get("/feedback")
def get_feedback(manager: str | None = None):
    _refresh()
    return [f for f in S.feedback[::-1] if not manager or f["manager"] == manager]


@router.post("/feedback")
def add_feedback(x: FeedbackIn):
    """Обратная связь супервайзера (или РОП) — дополнение к автоматической, уходит менеджеру."""
    names = {s["id"]: s["name"] for s in S.script["stages"]}
    f = {"id": f"FB-{next(S._ids):04d}", "manager": x.manager, "call_id": x.call_id, "author": x.author, "at": datetime.now().isoformat(timespec="minutes"),
         "risk_zones": [names[x.stage_id]] if x.stage_id in names else [], "text": x.text, "status": "отправлено менеджеру"}
    S.feedback.append(f)
    _save()
    return f


@router.get("/team")
def get_team(supervisor: str | None = None):
    _refresh()
    return {"managers": team(supervisor or None), "segments": list(SEGMENTS.values()), "close_types": list(CLOSE.values())}


@router.get("/department")
def get_department(supervisor: str | None = None):
    _refresh()
    return department(supervisor or None)


@router.get("/rop")
def get_rop():
    _refresh()
    return rop_summary()


@router.get("/teams")
def get_teams():
    return S.teams


@router.get("/calls/{cid}/metrics")
def get_metrics(cid: str):
    c = next((c for c in S.calls if c["id"] == cid), None)
    if not c:
        raise HTTPException(404)
    return metrics(c)


@router.get("/manager/{name}")
def get_profile(name: str):
    _refresh()
    return profile(name)


@router.post("/assign")
def assign(x: AssignIn):
    """Супервайзер принимает решение: назначает обучение или наставничество — оно уходит менеджеру и попадает в тренажёр."""
    if x.key not in TRAINING:
        raise HTTPException(400, "нет такого модуля")
    f = {"id": f"FB-{next(S._ids):04d}", "manager": x.manager, "call_id": None, "author": x.author, "kind": "обучение",
         "at": datetime.now().isoformat(timespec="minutes"), "risk_zones": [], "text": f"Назначено: {TRAINING[x.key]}" + (f". {x.comment}" if x.comment else ""),
         "module": x.key, "status": "отправлено менеджеру"}
    S.feedback.append(f)
    _save()
    return f


CLIENT_RULES = """Ты играешь КЛИЕНТА интернет-провайдера SkyNet в тренировочном звонке менеджера по продажам. Отвечай только репликой клиента:
1–2 коротких предложения, разговорно, по-русски, без ремарок и пояснений, никогда не говори, что ты ИИ или что это тренировка.
Не облегчай задачу: раскрывай детали о себе только в ответ на хорошие вопросы. Соглашайся на следующий шаг, только если менеджер
выяснил твою ситуацию, ответил на сомнение и предложил конкретный шаг (дата, время, действие). Если менеджер давит или хамит — реагируй как живой человек."""


class PracticeStart(BaseModel):
    manager: str
    scenario: str


class PracticeSay(BaseModel):
    text: str


def practice_start(manager: str, scenario: str) -> dict:
    sc = next((x for x in S.practice if x["id"] == scenario), None)
    if not sc:
        raise HTTPException(404, "нет сценария")
    sid = f"P-{len(S.sessions) + 1:03d}-{datetime.now():%H%M%S}"
    S.sessions[sid] = {"id": sid, "manager": manager, "scenario": sc, "dialog": [["client", sc["opening"]]]}
    return {"session": sid, "title": sc["title"], "client": sc["opening"]}


def practice_say(sid: str, text: str) -> dict:
    ses = S.sessions.get(sid)
    if not ses:
        raise HTTPException(404, "сессия не найдена")
    ses["dialog"].append(["agent", text])
    hist = "\n".join(f"{'Клиент' if r == 'client' else 'Менеджер'}: {t}" for r, t in ses["dialog"])
    for _ in range(2):
        reply = llm.complete(f"{CLIENT_RULES}\n\nТвоя роль: {ses['scenario']['persona']}", f"Разговор до сих пор:\n{hist}\n\nСледующая реплика клиента:")
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", reply):
            break
    reply = re.sub(r"[\u3040-\u30ff\u4e00-\u9fff]+", "", reply)
    reply = re.sub(r"^\s*(Клиент|Client)\s*:\s*", "", reply.strip().strip('"«»')).split("\nМенеджер")[0].strip()
    ses["dialog"].append(["client", reply])
    return {"client": reply, "turns": len(ses["dialog"])}


def practice_finish(sid: str) -> dict:
    ses = S.sessions.pop(sid, None)
    if not ses:
        raise HTTPException(404, "сессия не найдена")
    sc = ses["scenario"]
    call = {"id": sid, "manager": ses["manager"], "script": sc["script"], "outcome": "тренировка", "dialog": ses["dialog"]}
    r = analyze(call)
    S.feedback.append({"id": f"FB-{next(S._ids):04d}", "manager": ses["manager"], "call_id": sid, "author": "Тренажёр", "kind": "тренировка",
                       "at": r["analyzed_at"], "text": f"Тренировка «{sc['title']}»: {r['total']}%", "scenario": sc["id"],
                       "risk_zones": [next((x["name"] for x in S.script["stages"] if x["id"] == z), "Тон") for z in r["risk_zones"]],
                       "good": r["feedback"]["good"], "fix": r["feedback"]["fix"], "phrase": r["feedback"]["phrase"],
                       "style": VERDICT.get(r.get("style_verdict"), ""), "wrong_phrases": r.get("wrong_phrases", [])[:3], "status": "результат тренировки"})
    _save()
    return {"scenario": sc["title"], "dialog": ses["dialog"], "result": r}


def practice_list(manager: str) -> dict:
    assigned = {f.get("module") for f in S.feedback if f["manager"] == manager and f.get("kind") == "обучение"}
    done = [f for f in S.feedback if f["manager"] == manager and f.get("kind") == "тренировка"]
    return {"scenarios": [{"id": x["id"], "title": x["title"], "modules": [TRAINING[m] for m in x["modules"]],
                           "assigned": bool(assigned & set(x["modules"])), "tries": [f["text"] for f in done if f.get("scenario") == x["id"]]} for x in S.practice]}


@router.get("/practice")
def get_practice(manager: str):
    _refresh()
    return practice_list(manager)


@router.post("/practice/start")
def post_practice_start(x: PracticeStart):
    return practice_start(x.manager, x.scenario)


@router.post("/practice/{sid}/say")
def post_practice_say(sid: str, x: PracticeSay):
    return practice_say(sid, x.text)


@router.post("/practice/{sid}/finish")
def post_practice_finish(sid: str):
    _refresh()
    return practice_finish(sid)


@router.post("/feedback/{fid}/read")
def read_feedback(fid: str):
    f = next((f for f in S.feedback if f["id"] == fid), None)
    if not f:
        raise HTTPException(404)
    f["status"] = "прочитано"
    _save()
    return f

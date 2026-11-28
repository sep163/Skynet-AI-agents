"""Обратная связь на сами вердикты ИИ: споры, калибровка агрессии под людей, финальное слово РОП.

- Любой участник может возразить модели по конкретному пункту разбора: оценка этапа, стиль менеджера,
  враждебность клиента, итог звонка, «неправильная фраза». Менеджер → супервайзер (поддержать / отклонить) → РОП.
  Отклонённый спор менеджер может один раз обжаловать — тогда он тоже уходит к РОП.
- РОП выносит финальный вердикт (в том числе вручную, без спора) с комментарием и, по желанию, общим правилом.
  Решение сразу меняет разбор и всю аналитику, а изменённые вердикты и правила РОП подставляются в промпт
  следующих разборов как прецеденты — модель учится на выводах РОП без дообучения.
- Агрессия клиента калибруется под человеческое восприятие: люди оценивают звонки 0–3, оценки разных людей
  усредняются (у каждого своя чувствительность — она показывается как личное смещение), решение РОП весит больше.
  Поверх правил обучается поправка (гребневая регрессия на признаках текста и звука); честная точность — leave-one-out.
Это инструмент развития, а не KPI: споры и оценки не влияют на рейтинг сотрудника."""
import json
import re
from copy import deepcopy
from datetime import datetime
from statistics import mean, pstdev

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, emotion

router = APIRouter(prefix="/api/review", tags=["review"])
PATH = config.DATA / "runtime" / "sales_review.json"
SEED = config.DATA / "sales" / "human_labels.json"
ROLE_W = {"manager": 1.0, "supervisor": 1.5, "rop": 3.0}
ROLE = {"manager": "менеджер", "supervisor": "супервайзер", "rop": "РОП"}
STATUS = {"supervisor": "у супервайзера", "rop": "у РОП", "accepted": "принято", "rejected": "отклонено", "rejected_sup": "отклонено супервайзером"}
FEATS = ["rules_hostility", "dissatisfaction", "rough", "mat_rough", "addressed", "bangs", "caps", "client_interrupts", "client_raised_voice"]
LAMBDA = 2.0
FEAT_NAMES = {"rules_hostility": "враждебность по правилам", "dissatisfaction": "недовольство ситуацией", "rough": "резкая манера", "mat_rough": "мат и просторечие",
              "addressed": "резкость в адрес «вы»", "bangs": "восклицания", "caps": "капс", "client_interrupts": "клиент перебивает", "client_raised_voice": "клиент повышает голос"}


def _load():
    d = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}
    for k in ("disputes", "decisions", "labels", "useful"):
        d.setdefault(k, [])
    return d


R = _load()
_cal = {"model": None, "key": None}


def reset(data: dict | None = None):
    global R
    R = data or {"disputes": [], "decisions": [], "labels": [], "useful": []}
    _cal.update(model=None, key=None)


def _save():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    _cal["key"] = None
    _sales()._mt[PATH] = PATH.stat().st_mtime  # свою запись не перечитываем


def _now():
    return datetime.now().isoformat(timespec="minutes")


def _sales():
    from . import sales
    return sales


# ---------- поля разбора, которые можно оспорить ----------

def fields(call: dict, r: dict) -> list:
    s = _sales()
    names = {x["id"]: x["name"] for x in s.S.script["stages"]}
    out = [{"field": f"stage:{st['id']}", "label": f"Этап «{names.get(st['id'], st['id'])}»", "values": [0, 1, 2], "value": st["score"]} for st in r["stages"]]
    out += [{"field": f"style:{k}", "label": n, "values": [0, 1, 2], "value": r.get("style", {}).get(k)}
            for k, n in (("pressure", "Давление на клиента"), ("passivity", "Пассивность"), ("composure", "Уравновешенность"))]
    ch = r.get("client_hostility") or {}
    out.append({"field": "client_hostility", "label": "Враждебность клиента", "values": [0, 1, 2, 3], "value": ch.get("final", ch.get("model"))})
    out.append({"field": "close_type", "label": "Чем закончился звонок", "values": list(s.CLOSE), "names": s.CLOSE, "value": r.get("close_type")})
    out.append({"field": "goal_result", "label": "Цель скрипта", "values": list(s.GOAL), "names": s.GOAL, "value": r.get("goal_result")})
    out += [{"field": f"wrong_phrase:{i}", "label": f"Неудачная фраза «{w['quote'][:60]}»", "values": ["error", "not_error"],
             "names": {"error": "это ошибка", "not_error": "это не ошибка"}, "value": "error"} for i, w in enumerate(r.get("wrong_phrases", []))]
    return out


def model_value(r: dict, field: str, call: dict | None = None):
    if field.startswith("stage:"):
        return next((s["score"] for s in r["stages"] if s["id"] == field[6:]), None)
    if field.startswith("style:"):
        return r.get("style", {}).get(field[6:])
    if field == "client_hostility":
        return features(call)["rules_hostility"] if call else (r.get("client_hostility") or {}).get("model")
    if field.startswith("wrong_phrase:"):
        return "error"
    return r.get(field)


def _check(call, r, field, value):
    f = next((x for x in fields(call, r) if x["field"] == field), None)
    if not f:
        raise HTTPException(400, f"нет такого пункта разбора: {field}")
    if value not in f["values"]:
        raise HTTPException(400, f"допустимые значения: {f['values']}")
    return f


def _call(cid):
    s = _sales()
    s._refresh()
    call = next((c for c in s.S.calls if c["id"] == cid), None)
    if not call or cid not in s.S.raw:
        raise HTTPException(404, "звонок не найден или ещё не разобран")
    return call, s.S.raw[cid]


# ---------- признаки клиента и калибровка агрессии ----------

def features(call: dict) -> dict:
    """Признаки для калибровки: правила по тексту + звук (если есть стерео с таймкодами)."""
    rs = [emotion.rules(t) for r, t in call["dialog"] if r == "client"] or [emotion.rules("")]
    texts = [t.lower() for r, t in call["dialog"] if r == "client"]
    addressed = sum(bool(re.search(r"\b" + emotion.YOU_PL + r"\b", t)) and bool(re.search(emotion.MAT + "|" + emotion.ROUGH + "|" + emotion.INSULT, t) or "!" in t)
                    for t in texts)
    ac = _sales().acoustics(call.get("utterances")) if call.get("utterances") else {}
    return {"rules_hostility": max(x["hostility"] for x in rs), "dissatisfaction": max(x["dissatisfaction"] for x in rs),
            "rough": int(any(x["manner"] == "rough" for x in rs)), "mat_rough": min(3, sum(x["signals"]["mat"] + x["signals"]["rough"] for x in rs)),
            "addressed": min(2, addressed), "bangs": min(4, sum(x["signals"]["bangs"] for x in rs)), "caps": max(x["signals"]["caps"] for x in rs),
            "client_interrupts": ac.get("interruptions", {}).get("client", 0), "client_raised_voice": ac.get("raised_voice", {}).get("client", 0)}


_seed: list = []


def human_labels() -> list:
    if not _seed and SEED.exists():
        _seed.extend(json.loads(SEED.read_text(encoding="utf-8"))["labels"])
    seed = _seed
    own = {(x["call_id"], x["rater"]) for x in R["labels"]}
    return [x for x in seed if (x["call_id"], x["rater"]) not in own] + R["labels"]


def targets() -> dict:
    """Человеческая оценка по звонку: взвешенное среднее оценок; решение РОП по звонку — эталон с большим весом."""
    by = {}
    for x in human_labels():
        by.setdefault(x["call_id"], []).append(x)
    out = {}
    for cid, xs in by.items():
        w = [ROLE_W.get(x["role"], 1.0) for x in xs]
        out[cid] = {"y": sum(x["value"] * wi for x, wi in zip(xs, w)) / sum(w), "weight": 1.0, "n": len(xs),
                    "spread": round(pstdev([x["value"] for x in xs]), 2) if len(xs) > 1 else 0.0, "ratings": {x["rater"]: x["value"] for x in xs}}
    for d in R["decisions"]:
        if d["field"] == "client_hostility":
            o = out.setdefault(d["call_id"], {"n": 0, "spread": 0.0, "ratings": {}})
            o.update(y=float(d["value"]), weight=3.0, rop=d["value"])
    return out


def _fit(X, y, w):
    mu, sd = X.mean(0), X.std(0)
    keep = sd > 1e-9
    Z = (X[:, keep] - mu[keep]) / sd[keep]
    W = np.diag(w)
    b0 = float((w * y).sum() / w.sum())
    beta = np.linalg.solve(Z.T @ W @ Z + LAMBDA * np.eye(Z.shape[1]), Z.T @ W @ (y - b0)) if Z.shape[1] else np.zeros(0)
    return {"mu": mu, "sd": sd, "keep": keep, "b0": b0, "beta": beta}


def _pred(m, X):
    Z = (X[:, m["keep"]] - m["mu"][m["keep"]]) / m["sd"][m["keep"]]
    return m["b0"] + Z @ m["beta"]


def calibration() -> dict:
    """Обучает поправку к правилам: остаток (человек − правила) ~ признаки. Мало данных — поправка тянется к нулю."""
    s = _sales()
    calls = {c["id"]: c for c in s.S.calls}
    T = {cid: t for cid, t in targets().items() if cid in calls}
    key = (len(human_labels()), len(R["decisions"]), tuple(sorted(T)))
    if _cal["key"] == key and _cal["model"] is not None:
        return _cal["model"]
    ids = sorted(T)
    F = {cid: features(calls[cid]) for cid in ids}
    rep = {"n_calls": len(ids), "n_labels": len(human_labels()), "fitted": False, "features": FEATS}
    if len(ids) >= 6:
        X = np.array([[F[c][f] for f in FEATS] for c in ids], float)
        base = X[:, 0]
        y = np.array([T[c]["y"] for c in ids])
        w = np.array([T[c]["weight"] for c in ids])
        m = _fit(X, y - base, w)
        loo = []
        for i in range(len(ids)):
            mask = np.arange(len(ids)) != i
            mi = _fit(X[mask], (y - base)[mask], w[mask])
            loo.append(float(np.clip(base[i] + _pred(mi, X[i:i + 1])[0], 0, 3)))
        loo = np.array(loo)
        coef = dict.fromkeys(FEATS, 0.0)
        for f, b, sd in zip(np.array(FEATS)[m["keep"]], m["beta"], m["sd"][m["keep"]]):
            coef[str(f)] = round(float(b / sd), 3)
        rep.update(fitted=True, _m=m, bias=round(m["b0"], 2), weights=coef, weights_named={FEAT_NAMES[k]: v for k, v in coef.items()},
                   mae_rules=round(float(np.average(np.abs(base - y), weights=w)), 2), mae_calibrated_loo=round(float(np.average(np.abs(loo - y), weights=w)), 2),
                   exact_rules=round(float(np.mean(np.round(base) == np.round(y))), 2), exact_calibrated_loo=round(float(np.mean(np.round(loo) == np.round(y))), 2))
        rep["calls"] = [{"call_id": c, "manager": calls[c]["manager"], "rules": int(base[i]), "human": round(float(y[i]), 2), "spread": T[c]["spread"],
                         "calibrated": round(float(np.clip(base[i] + _pred(m, X[i:i + 1])[0], 0, 3)), 2), "loo": round(float(loo[i]), 2),
                         "rop": T[c].get("rop"), "ratings": T[c]["ratings"]} for i, c in enumerate(ids)]
    rep["raters"] = raters(T)
    rep["contested"] = [x for x in rep.get("calls", []) if x["rop"] is None and (x["spread"] >= 0.9 or abs(x["rules"] - x["human"]) >= 1.2)]
    _cal.update(model=rep, key=key)
    return rep


def raters(T: dict) -> list:
    """Личная чувствительность к агрессии: насколько человек оценивает строже или мягче остальных по тем же звонкам."""
    out = {}
    for cid, t in T.items():
        rs = t["ratings"]
        for who, v in rs.items():
            others = [x for k, x in rs.items() if k != who]
            if others:
                out.setdefault(who, []).append(v - mean(others))
    return sorted([{"rater": k, "bias": round(mean(v), 2), "calls": len(v),
                    "reading": "строже среднего" if mean(v) >= 0.3 else "мягче среднего" if mean(v) <= -0.3 else "как в среднем"} for k, v in out.items()],
                  key=lambda x: -x["bias"])


def calibrated_hostility(call: dict) -> float | None:
    rep = calibration()
    if not rep.get("fitted"):
        return None
    X = np.array([[features(call)[f] for f in FEATS]], float)
    return round(float(np.clip(X[0, 0] + _pred(rep["_m"], X)[0], 0, 3)), 2)


# ---------- наложение решений людей на разбор ИИ ----------

def final_decisions(cid: str) -> dict:
    out = {}
    for d in R["decisions"]:
        if d["call_id"] == cid:
            out[d["field"]] = d
    return out


def apply(call: dict, r: dict) -> dict:
    """Эффективный разбор: ИИ → калибровка агрессии → решения РОП. Исходный разбор ИИ не меняется."""
    s = _sales()
    x = deepcopy(r)
    dec = final_decisions(call["id"])
    f = features(call)
    cal = calibrated_hostility(call)
    ch = {"model": f["rules_hostility"], "calibrated": cal, "final": round(cal) if cal is not None else f["rules_hostility"],
          "source": "калибровка по оценкам людей" if cal is not None else "правила"}
    if "client_hostility" in dec:
        ch.update(final=dec["client_hostility"]["value"], source="решение РОП")
    x["client_hostility"] = ch
    changed = []
    for field, d in dec.items():
        v = d["value"]
        if field.startswith("stage:"):
            for st in x["stages"]:
                if st["id"] == field[6:] and st["score"] != v:
                    st.update(score=v, model_score=st["score"], human=d["author"], human_comment=d.get("comment", ""))
                    if v == 2:
                        st["mistake"] = ""
                    changed.append(field)
        elif field.startswith("style:"):
            if x["style"].get(field[6:]) != v:
                x["style"][field[6:]] = v
                changed.append(field)
        elif field in ("close_type", "goal_result"):
            if x.get(field) != v:
                x[field], changed = v, changed + [field]
        elif field.startswith("wrong_phrase:") and v == "not_error":
            i = int(field.split(":")[1])
            if i < len(x.get("wrong_phrases", [])):
                x["wrong_phrases"][i]["dismissed"] = True
                changed.append(field)
        elif field == "client_hostility" and v != f["rules_hostility"]:
            changed.append(field)
    x["wrong_phrases"] = [w for w in x.get("wrong_phrases", []) if not w.get("dismissed")]
    h = ch["final"]
    seg = r.get("client_segment", "calm")
    x["client_segment"] = "hostile" if h >= 2 else "agitated" if seg in ("agitated", "hostile") or h >= 1 else "calm"
    x["style_verdict"] = s.verdict(x["style"])
    x["risk_zones"] = [st["id"] for st in x["stages"] if st["score"] == 0] + (["tone"] if x["style"]["composure"] == 0 else [])
    x["total"] = round(100 * sum(st["score"] for st in x["stages"]) / (2 * len(x["stages"])))
    x["review"] = {"changed": changed, "decisions": len(dec), "open_disputes": sum(1 for d in R["disputes"] if d["call_id"] == call["id"] and d["status"] in ("supervisor", "rop"))}
    return x


# ---------- прецеденты РОП для следующих разборов ----------

def precedents(script: str | None = None, limit: int = 8) -> list:
    s = _sales()
    calls = {c["id"]: c for c in s.S.calls}
    ds = [d for d in R["decisions"] if d.get("rule") or d["value"] != d.get("model_value")]
    ds.sort(key=lambda d: d["at"], reverse=True)
    ds.sort(key=lambda d: (calls.get(d["call_id"], {}).get("script") != script, not d.get("rule")))
    return ds[:limit]


def precedents_prompt(script: str | None) -> str:
    ps = precedents(script)
    if not ps:
        return ""
    lines = []
    for d in ps:
        v = lambda x: f"{x}" if not isinstance(x, str) else {**_sales().CLOSE, **_sales().GOAL, "error": "ошибка", "not_error": "не ошибка"}.get(x, x)
        lines.append(f"- {d['label']} (звонок {d['call_id']}): ИИ — {v(d.get('model_value'))}, РОП — {v(d['value'])}."
                     + (f" Довод сотрудника: {d['argument']}." if d.get("argument") else "")
                     + (f" Почему (РОП): {d['comment']}." if d.get("comment") else "") + (f" Правило: {d['rule']}" if d.get("rule") else ""))
    return "\n\nРешения РОП по прошлым спорным разборам — следуй им в похожих ситуациях:\n" + "\n".join(lines)


# ---------- споры и решения ----------

class DisputeIn(BaseModel):
    call_id: str
    field: str
    proposed: int | str
    comment: str = ""
    author: str
    role: str = "manager"


class SupervisorIn(BaseModel):
    decision: str
    author: str
    comment: str = ""


class RopIn(BaseModel):
    decision: str = "set"
    value: int | str | None = None
    comment: str = ""
    rule: str = ""
    author: str = "РОП"


class VerdictIn(BaseModel):
    call_id: str
    field: str
    value: int | str
    comment: str = ""
    rule: str = ""
    author: str = "РОП"


class LabelIn(BaseModel):
    call_id: str
    rater: str
    role: str = "manager"
    value: int


class UsefulIn(BaseModel):
    feedback_id: str
    useful: bool
    who: str
    comment: str = ""


def _notify(manager, call_id, author, text):
    s = _sales()
    s.S.feedback.append({"id": f"FB-{next(s.S._ids):04d}", "manager": manager, "call_id": call_id, "author": author, "kind": "спор",
                         "at": _now(), "risk_zones": [], "text": text, "status": "отправлено менеджеру"})
    s._save()


def _rebuild():
    _save()
    _sales().rebuild()


def dispute(x: DisputeIn) -> dict:
    call, r = _call(x.call_id)
    if x.role not in ROLE:
        raise HTTPException(400, "роль: manager, supervisor или rop")
    eff = _sales().S.results[x.call_id]
    f = _check(call, eff, x.field, x.proposed)
    if any(d["call_id"] == x.call_id and d["field"] == x.field and d["status"] in ("supervisor", "rop") for d in R["disputes"]):
        raise HTTPException(409, "по этому пункту уже есть открытый спор")
    if x.role == "rop":
        return decide_verdict(VerdictIn(call_id=x.call_id, field=x.field, value=x.proposed, comment=x.comment, author=x.author))
    d = {"id": f"DS-{len(R['disputes']) + 1:04d}", "call_id": x.call_id, "manager": call["manager"], "supervisor": _sales().supervisor_of(call["manager"]),
         "field": x.field, "label": f["label"], "model_value": model_value(r, x.field, call), "current": f["value"], "proposed": x.proposed,
         "values": f["values"], "names": f.get("names"),
         "author": x.author, "role": x.role, "comment": x.comment, "at": _now(), "status": "supervisor" if x.role == "manager" else "rop",
         "history": [{"at": _now(), "who": x.author, "role": x.role, "action": "оспорил", "comment": x.comment}], "appealed": False}
    R["disputes"].append(d)
    if x.field == "client_hostility":
        _label(x.call_id, x.author, x.role, int(x.proposed))
    _save()
    return view(d)


def supervisor_decide(did: str, x: SupervisorIn) -> dict:
    d = _get(did)
    if d["status"] != "supervisor":
        raise HTTPException(409, "спор не у супервайзера")
    if x.decision not in ("support", "reject"):
        raise HTTPException(400, "support или reject")
    d["history"].append({"at": _now(), "who": x.author, "role": "supervisor", "action": "поддержал" if x.decision == "support" else "отклонил", "comment": x.comment})
    d["status"] = "rop" if x.decision == "support" else "rejected_sup"
    if x.decision == "reject":
        _notify(d["manager"], d["call_id"], x.author, f"Спор «{d['label']}» отклонён" + (f": {x.comment}" if x.comment else "") + ". Можно обжаловать у РОП.")
    _save()
    return view(d)


def appeal(did: str, comment: str = "") -> dict:
    d = _get(did)
    if d["status"] != "rejected_sup" or d["appealed"]:
        raise HTTPException(409, "обжаловать можно один раз и только отклонённый супервайзером спор")
    d.update(status="rop", appealed=True)
    d["history"].append({"at": _now(), "who": d["manager"], "role": "manager", "action": "обжаловал у РОП", "comment": comment})
    _save()
    return view(d)


def rop_decide(did: str, x: RopIn) -> dict:
    d = _get(did)
    if d["status"] != "rop":
        raise HTTPException(409, "спор не у РОП")
    if x.decision == "reject":
        d["status"] = "rejected"
        value = d["current"]
    else:
        value = d["proposed"] if x.decision == "accept" or x.value is None else x.value
        d["status"] = "accepted"
    d["history"].append({"at": _now(), "who": x.author, "role": "rop", "action": {"reject": "оставил вердикт ИИ", "accept": "принял"}.get(x.decision, f"установил {value}"),
                         "comment": x.comment})
    d["final"] = value
    dec = decide_verdict(VerdictIn(call_id=d["call_id"], field=d["field"], value=value, comment=x.comment, rule=x.rule, author=x.author), dispute_id=did)
    _notify(d["manager"], d["call_id"], x.author, f"Решение РОП по спору «{d['label']}»: " + ("вердикт ИИ оставлен" if x.decision == "reject" else f"исправлено на {value}")
            + (f". {x.comment}" if x.comment else ""))
    return {"dispute": view(d), "decision": dec}


def decide_verdict(x: VerdictIn, dispute_id: str | None = None) -> dict:
    """Финальный вердикт РОП: ручной ввод по любому пункту. Совпадение с ИИ тоже фиксируется — это подтверждение модели."""
    call, r = _call(x.call_id)
    f = _check(call, _sales().S.results[x.call_id], x.field, x.value)
    R["decisions"] = [d for d in R["decisions"] if not (d["call_id"] == x.call_id and d["field"] == x.field)]
    d = {"call_id": x.call_id, "manager": call["manager"], "field": x.field, "label": f["label"], "model_value": model_value(r, x.field, call), "value": x.value,
         "comment": x.comment, "rule": x.rule.strip(), "author": x.author, "at": _now(), "dispute": dispute_id}
    if dispute_id:
        d["argument"] = _get(dispute_id)["comment"]
    R["decisions"].append(d)
    if x.field == "client_hostility":
        _label(x.call_id, x.author, "rop", int(x.value))
    _rebuild()
    return d


def _label(cid, rater, role, value):
    R["labels"] = [x for x in R["labels"] if not (x["call_id"] == cid and x["rater"] == rater)]
    R["labels"].append({"call_id": cid, "rater": rater, "role": role, "value": value, "at": _now()})


def label(x: LabelIn) -> dict:
    if x.value not in (0, 1, 2, 3):
        raise HTTPException(400, "оценка 0–3")
    _call(x.call_id)
    _label(x.call_id, x.rater, x.role, x.value)
    _rebuild()
    return {"ok": True, "call": x.call_id, "calibrated": _sales().S.results[x.call_id]["client_hostility"]}


def _get(did):
    d = next((d for d in R["disputes"] if d["id"] == did), None)
    if not d:
        raise HTTPException(404, "спор не найден")
    return d


def view(d):
    return d | {"status_label": STATUS[d["status"]], "role_label": ROLE.get(d["role"], d["role"])}


def disputes(status: str | None = None, manager: str | None = None, supervisor: str | None = None) -> list:
    return [view(d) for d in R["disputes"][::-1] if (not status or d["status"] in status.split(","))
            and (not manager or d["manager"] == manager) and (not supervisor or d["supervisor"] == supervisor)]


def call_review(cid: str, rater: str | None = None) -> dict:
    call, r = _call(cid)
    eff = _sales().S.results[cid]
    dec = final_decisions(cid)
    return {"call_id": cid, "manager": call["manager"], "fields": [f | {"model": model_value(r, f["field"], call), "decision": dec.get(f["field"])} for f in fields(call, eff)],
            "client_hostility": eff["client_hostility"], "disputes": [view(d) for d in R["disputes"] if d["call_id"] == cid],
            "my_label": next((x["value"] for x in human_labels() if x["call_id"] == cid and x["rater"] == rater), None) if rater else None,
            "labels": [x for x in human_labels() if x["call_id"] == cid]}


def model_quality() -> dict:
    """Насколько ИИ совпадает с людьми: по решениям РОП, по спорам и по полезности обратной связи."""
    fam = lambda f: f.split(":")[0]
    NAMES = {"stage": "оценка этапов", "style": "стиль менеджера", "client_hostility": "враждебность клиента", "close_type": "итог звонка",
             "goal_result": "цель скрипта", "wrong_phrase": "неудачные фразы"}
    by = {}
    for d in R["decisions"]:
        x = by.setdefault(fam(d["field"]), {"checked": 0, "confirmed": 0})
        x["checked"] += 1
        x["confirmed"] += d["value"] == d.get("model_value")
    ds = R["disputes"]
    u = R["useful"]
    return {"by_field": [{"field": NAMES.get(k, k), "checked": v["checked"], "agreement": round(v["confirmed"] / v["checked"], 2)} for k, v in by.items()],
            "agreement": round(sum(v["confirmed"] for v in by.values()) / max(1, sum(v["checked"] for v in by.values())), 2) if by else None,
            "disputes": {"total": len(ds), "open": sum(d["status"] in ("supervisor", "rop") for d in ds), "accepted": sum(d["status"] == "accepted" for d in ds),
                         "rejected": sum(d["status"] in ("rejected", "rejected_sup") for d in ds)},
            "feedback_useful": {"votes": len(u), "useful_share": round(sum(x["useful"] for x in u) / len(u), 2) if u else None},
            "precedents": len(precedents(limit=1000))}


@router.get("/fields/{cid}")
def get_call_review(cid: str, rater: str | None = None):
    return call_review(cid, rater)


@router.get("/disputes")
def get_disputes(status: str | None = None, manager: str | None = None, supervisor: str | None = None):
    _sales()._refresh()
    return disputes(status, manager, supervisor)


@router.post("/disputes")
def post_dispute(x: DisputeIn):
    return dispute(x)


@router.post("/disputes/{did}/supervisor")
def post_supervisor(did: str, x: SupervisorIn):
    return supervisor_decide(did, x)


class AppealIn(BaseModel):
    comment: str = ""


@router.post("/disputes/{did}/appeal")
def post_appeal(did: str, x: AppealIn):
    return appeal(did, x.comment)


@router.post("/disputes/{did}/rop")
def post_rop(did: str, x: RopIn):
    return rop_decide(did, x)


@router.post("/verdict")
def post_verdict(x: VerdictIn):
    return decide_verdict(x)


@router.get("/decisions")
def get_decisions():
    return R["decisions"][::-1]


@router.post("/labels")
def post_label(x: LabelIn):
    return label(x)


@router.get("/calibration")
def get_calibration():
    _sales()._refresh()
    return {k: v for k, v in calibration().items() if not k.startswith("_")}


@router.get("/quality")
def get_quality():
    return model_quality()


@router.get("/precedents")
def get_precedents(script: str | None = None):
    return {"items": precedents(script), "prompt": precedents_prompt(script)}


@router.post("/useful")
def post_useful(x: UsefulIn):
    """Полезна ли была обратная связь: менеджер отмечает прямо в ленте, это видит РОП в качестве модели."""
    R["useful"] = [u for u in R["useful"] if not (u["feedback_id"] == x.feedback_id and u["who"] == x.who)]
    R["useful"].append({"feedback_id": x.feedback_id, "useful": x.useful, "who": x.who, "comment": x.comment, "at": _now()})
    _save()
    return {"ok": True}

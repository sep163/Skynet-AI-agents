"""Кейс «Реклама и коммуникации»: ИИ-редактор пишет сообщения под сегмент и канал, а детерминированный проверяльщик
сверяет каждую цифру с фактами, ловит запрещённые формулировки, жаргон, акции без срока и превышение длины. Отправка — после одобрения человеком."""
import json
import re
from itertools import count

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import llm
from .common import load

router = APIRouter(prefix="/api/comms", tags=["comms"])


class State:
    def __init__(self):
        self.rules = load("comms/rules.json")
        self.facts = {"home": load("onboarding/tariffs.json"), "business": load("b2b/services.json")["services"]}
        self.drafts = []
        self._ids = count(1)


S = State()


def allowed():
    h = S.facts["home"]
    prices = {t["fee"] for t in h["tariffs"]} | {v.get("fee", v.get("price")) for v in h["extras"].values()} | {h["promo"]["first_month_fee"]}
    prices |= {s["monthly"] for s in S.facts["business"]} | {s["once"] for s in S.facts["business"]}
    speeds = {t["mbps"] for t in h["tariffs"]} | {100, 300, 1000}
    return {"prices": {float(p) for p in prices if p is not None}, "speeds": {float(s) for s in speeds}, "channels": {float(t.get("tv_channels", 0)) for t in h["tariffs"]} - {0.0}}


def _num(s):
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def check(text: str, channel: str, segment: str) -> dict:
    r, a, issues = S.rules, allowed(), []
    low = text.lower()
    lim = r["channels"][channel]["max_chars"]
    if len(text) > lim:
        issues.append({"type": "too_long", "msg": f"{len(text)} знаков при лимите {lim} для канала «{r['channels'][channel]['name']}»"})
    for f in r["forbidden"]:
        m = re.search(f["pattern"], low)
        if m:
            issues.append({"type": "forbidden", "msg": f"«{m.group(0)}» — {f['why']}"})
    for m in re.finditer(r"(\d[\d  ]*(?:[.,]\d+)?)\s*(₽|руб|мбит|гбит|%|канал)", low):
        v, unit = _num(m.group(1)), m.group(2)
        if unit in ("₽", "руб") and v not in a["prices"]:
            issues.append({"type": "unknown_price", "msg": f"цена {m.group(0)} не найдена в прайсе"})
        elif unit == "мбит" and v not in a["speeds"]:
            issues.append({"type": "unknown_speed", "msg": f"скорость {m.group(0)} не соответствует ни одному тарифу"})
        elif unit == "гбит" and v * 1000 not in a["speeds"]:
            issues.append({"type": "unknown_speed", "msg": f"скорость {m.group(0)} не соответствует ни одному тарифу"})
        elif unit == "%":
            issues.append({"type": "unknown_percent", "msg": f"скидка {m.group(0)} — в фактах нет такой акции"})
        elif unit == "канал" and v not in a["channels"]:
            issues.append({"type": "unknown_fact", "msg": f"{m.group(0)} — нет в фактах"})
    if any(p in low for p in r["promo_markers"]) and not re.search(r"\bдо\s+\d{1,2}[./]\d{1,2}|\bдо\s+\d{4}-\d{2}-\d{2}|\bдо\s+\d{1,2}\s+[а-я]+|\bдо конца", low):
        issues.append({"type": "promo_no_deadline", "msg": "акция без срока действия"})
    if "бесплатн" in low and not re.search(r"подключени\w*\s+(—\s*)?бесплатн|бесплатн\w*\s+подключ", low):
        issues.append({"type": "forbidden", "msg": "«бесплатно» допустимо только про подключение"})
    if segment != "бизнес":
        for j in r["jargon"]:
            if j.lower() in low:
                issues.append({"type": "jargon", "msg": f"«{j}» — непонятно аудитории «{segment}»"})
    sents = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    avg = sum(len(s.split()) for s in sents) / max(len(sents), 1)
    if avg > 20:
        issues.append({"type": "readability", "msg": f"длинные предложения: в среднем {avg:.0f} слов"})
    return {"ok": not issues, "issues": issues, "chars": len(text), "limit": lim}


GEN = {"type": "object", "properties": {"variants": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "idea": {"type": "string"}},
                                                                                  "required": ["text", "idea"], "additionalProperties": False}}},
       "required": ["variants"], "additionalProperties": False}
REV = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}


def _system(channel, segment):
    r = S.rules
    return (f"Ты — редактор коммуникаций. {r['brand']}\nКанал: {r['channels'][channel]['name']}, не длиннее {r['channels'][channel]['max_chars']} знаков.\n"
            f"Аудитория «{segment}»: {r['segments'][segment]}.\nИспользуй только факты из JSON ниже — никаких других цен, скоростей, скидок и обещаний. "
            f"Акцию упоминай только со сроком. Без превосходных степеней («самый», «лучший», «№1»), без «гарантируем» и «навсегда».\n"
            f"ФАКТЫ: {json.dumps(S.facts, ensure_ascii=False)}")


def generate(goal: str, channel: str, segment: str, n: int = 3) -> dict:
    if channel not in S.rules["channels"] or segment not in S.rules["segments"]:
        raise HTTPException(400, "неизвестный канал или сегмент")
    sys = _system(channel, segment)
    vs = llm.complete_json(sys, f"Задача: {goal}\nНапиши {n} разных варианта (разные идеи подачи).", GEN)["variants"][:n]
    out = []
    for v in vs:
        c = check(v["text"], channel, segment)
        item = {"text": v["text"], "idea": v["idea"], "check": c, "revised": False}
        if not c["ok"]:
            fix = llm.complete_json(sys, f"Исправь текст, устранив проблемы, сохранив идею.\nТекст: {v['text']}\nПроблемы: {json.dumps(c['issues'], ensure_ascii=False)}", REV)
            c2 = check(fix["text"], channel, segment)
            item |= {"before": v["text"], "before_issues": c["issues"], "text": fix["text"], "check": c2, "revised": True}
        out.append(item)
    d = {"id": f"MSG-{next(S._ids):04d}", "goal": goal, "channel": channel, "segment": segment, "variants": out, "status": "ждёт одобрения"}
    S.drafts.append(d)
    return d


class GenIn(BaseModel):
    goal: str
    channel: str
    segment: str


class CheckIn(BaseModel):
    text: str
    channel: str
    segment: str


class ApproveIn(BaseModel):
    variant: int


@router.get("/meta")
def meta():
    return {"channels": S.rules["channels"], "segments": list(S.rules["segments"]), "drafts": S.drafts[::-1]}


@router.post("/generate")
def gen(x: GenIn):
    return generate(x.goal, x.channel, x.segment)


@router.post("/check")
def chk(x: CheckIn):
    return check(x.text, x.channel, x.segment)


@router.post("/drafts/{did}/approve")
def approve(did: str, x: ApproveIn):
    d = next((d for d in S.drafts if d["id"] == did), None)
    if not d:
        raise HTTPException(404)
    if not d["variants"][x.variant]["check"]["ok"]:
        raise HTTPException(400, "вариант не прошёл проверку фактов — сначала исправьте")
    d.update(status="одобрено к отправке", chosen=x.variant)
    return d

"""Общие помощники для кейсов: загрузка данных, проверка цен в ответе, разбор блока «Что дальше»."""
import json
import re
from datetime import datetime, timedelta

from .. import config

# Число с разделителем тысяч («1 500», «12 000») или без. Соседние числа не склеиваются: «3 1000 ₽» — это 1000.
NUM = r"(?<![\d.,])((?:\d{1,3}(?:[  ]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?)"
PRICE = re.compile(NUM + r"\s*(?:₽|руб)", re.I)
MONEY_KEYS = {"fee", "price", "rub", "monthly", "once", "first_month_fee"}
GUARD = True  # False — прогон без автопроверок (для сравнения в eval)
NEXT = re.compile(r"\**Что дальше:?\**:?\s*(.+)$", re.S | re.I)


def load(path: str):
    return json.loads((config.DATA / path).read_text(encoding="utf-8"))


def prices_in(text: str) -> list[float]:
    return [float(m.replace(" ", "").replace(" ", "").replace(",", ".")) for m in PRICE.findall(text)]


def price_check(text: str, allowed: set[float]) -> dict:
    """Доверие: каждая сумма в ответе должна быть из прайса или из расчёта инструмента."""
    found = prices_in(text)
    unknown = sorted({p for p in found if p not in allowed})
    return {"ok": not unknown, "prices": found, "unknown": unknown}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def money_from(obj) -> set[float]:
    """Только денежные поля (fee, price, rub, *_rub…). Скорость, число каналов и номера слотов ценой не считаются."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _num(v) and (k in MONEY_KEYS or k.endswith("_rub")):
                out.add(float(v))
            else:
                out |= money_from(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= money_from(v)
    return out


def split_next(text: str) -> dict:
    m = NEXT.search(text)
    return {"body": text[: m.start()].rstrip() if m else text, "next": m.group(1).strip() if m else None}


def slots(booked: list[dict], start: datetime, n: int = 3, hours=(9, 11, 13, 15, 17, 19), days: int = 4) -> list[dict]:
    res = []
    for d in range(1, days + 1):
        day = (start + timedelta(days=d)).date()
        for h in hours:
            s = datetime(day.year, day.month, day.day, h)
            if not any(b["start"] == s for b in booked):
                res.append({"start": s, "end": s + timedelta(hours=2)})
    return res[:n]


def last_user(history: list[dict]) -> str:
    return next((m["content"] for m in reversed(history) if m["role"] == "user"), "")


def _ok(trace, *tools):
    return any(t["tool"] in tools and not (isinstance(t["result"], dict) and t["result"].get("error")) for t in trace)


def _planned(trace):
    return any(t["tool"] == "get_network_incidents" and t["result"].get("planned_visits") for t in trace if isinstance(t["result"], dict))


CLAIMS = {
    "onboarding": [(r"записал|записываю вас|запись (подтвержд|оформл|создан)|заявк\w* (оформлен|создан|принят)|подтверждаю запись",
                    lambda tr: _ok(tr, "book_installation", "add_to_waitlist"), "Ты сообщаешь клиенту о записи/заявке, но book_installation (или add_to_waitlist) не вызывался.")],
    "support": [(r"(записал|назначил|вызвал|оформил)\w* мастер|выезд\w* (назначен|оформлен|запланирован)|мастер приедет",
                 lambda tr: _ok(tr, "book_technician_visit") or _planned(tr), "Ты говоришь о выезде мастера, но выезд не забронирован (book_technician_visit) и не найден в запланированных."),
                (r"(включил|подключил|активировал)\w* обещанн", lambda tr: _ok(tr, "promise_payment"), "Ты говоришь, что включил обещанный платёж, но promise_payment не вызывался."),
                (r"(создал|оформил|завёл|завел)\w* заявк", lambda tr: _ok(tr, "create_ticket", "book_technician_visit"), "Ты говоришь о созданной заявке, но create_ticket не вызывался.")],
    "b2b": [(r"(подготовил|сформировал|собрал|составил)\w* (кп|коммерческ)", lambda tr: False, "Бот не готовит КП бизнес-клиенту — это делает менеджер; ты можешь только передать запрос."),
            (r"передал\w*|направил\w* (запрос|заявк|менеджер)|менеджер\w* (уже )?(получил|свяжется)", lambda tr: _ok(tr, "save_brief"), "Ты говоришь о передаче менеджеру, но save_brief не вызывался.")],
}


def violations(case: str, text: str, trace: list) -> list[str]:
    return [msg for rx, sat, msg in CLAIMS.get(case, []) if re.search(rx, text, re.I) and not sat(trace)]


def check_all(case, text, trace, allowed_base, calc_hint):
    v = violations(case, text, trace)
    if allowed_base is not None:
        bad = price_check(text, allowed_base | money_from([t["result"] for t in trace]))["unknown"]
        if bad:
            v.append(f"В ответе суммы {', '.join(f'{x:g}' for x in bad)} ₽, которых нет ни в прайсе, ни в расчёте инструмента. Не считай в уме — посчитай через {calc_hint}.")
    return v


def guarded_run(case: str, system: str, history: list, tools: list, allowed_base: set | None = None, calc_hint: str = "инструмент расчёта"):
    """Агентный прогон через граф LangGraph (app/graph.py): agent → tools → guard.
    guard — автопроверки «сказал = сделал» и «каждая сумма из прайса или расчёта»; при расхождении одна попытка исправиться."""
    from .. import graph
    return graph.run(case, system, history, tools, allowed_base, calc_hint)

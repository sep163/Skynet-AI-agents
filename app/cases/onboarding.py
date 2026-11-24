"""Кейс «Подключение новых клиентов»: ИИ-консультант, который за один диалог проверяет адрес, подбирает тариф
под потребности, честно считает платежи и записывает на монтаж. Все суммы в ответе проверяются по прайсу."""
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from itertools import count

from fastapi import APIRouter
from pydantic import BaseModel

from .. import config
from ..kb import get_kb
from ..llm import Tool
from .common import guarded_run, load, numbers_from, price_check, slots, split_next

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])
ROLE = """Ты — консультант SkyNet по подключению домашнего интернета в чате на сайте. С тобой говорит человек, который ещё не клиент.
Отвечай по-русски, коротко (до 6 предложений), понятно для человека без технических знаний. Используй инструменты — они реально проверяют адрес, считают цены и записывают на монтаж."""


class State:
    def __init__(self):
        self.price = load("onboarding/tariffs.json")
        self.coverage = load("onboarding/coverage.json")
        self.applications, self.waitlist = [], []
        self._ids = count(1)
        self.now = lambda: datetime.now().replace(second=0, microsecond=0)


S = State()


def reset():
    global S
    S = State()


def _norm(s):
    s = s.lower().replace("ё", "е")
    s = re.sub(r"\b(ул|улица|пр|проспект|пр-т|ш|шоссе|пос|поселок|д|дом|кв|квартира|г|санкт-петербург|спб)\b\.?", " ", s)
    return " ".join(re.findall(r"[a-zа-я0-9]+", s))


def find_address(addr: str):
    a, nums = _norm(addr), re.findall(r"\d+", addr)
    best, score = None, 0.0
    for c in S.coverage:
        house = re.findall(r"\d+", c["address"])
        if house and house[-1] not in nums:
            continue
        r = SequenceMatcher(None, a, _norm(c["address"])).ratio()
        if r > score:
            best, score = c, r
    return best if score >= 0.6 else None


def check_address(address: str):
    c = find_address(address)
    if not c:
        return {"status": "unknown", "message": "Адреса нет в базе: уточните улицу и номер дома или оставьте заявку в листе ожидания"}
    return c


def tariffs():
    p = S.price
    return {"tariffs": p["tariffs"], "extras": p["extras"], "promo": p["promo"]}


def calc(tariff_id: str, router: str, tv_box: bool, static_ip: bool):
    p, ex = S.price, S.price["extras"]
    q = tariff_id.lower().replace("«", "").replace("»", "")
    t = next((x for x in p["tariffs"] if x["id"] == tariff_id or x["name"].lower() == q), None) or \
        next((x for x in p["tariffs"] if re.findall(r"\d+", q) and str(x["mbps"]) in re.findall(r"\d+", q) and x["tv"] == ("тв" in q or "tv" in q)), None)
    if not t:
        return {"error": f"нет тарифа {tariff_id}", "tariff_ids": {x["id"]: x["name"] for x in p["tariffs"]}}
    monthly = [{"name": f"Тариф «{t['name']}»", "rub": t["fee"]}]
    once = [{"name": ex["connection"]["name"], "rub": ex["connection"]["price"]}]
    if router == "rent":
        monthly.append({"name": ex["router_rent"]["name"], "rub": ex["router_rent"]["fee"]})
    elif router == "buy":
        once.append({"name": ex["router_buy"]["name"], "rub": ex["router_buy"]["price"]})
    if tv_box and t["tv"]:
        monthly.append({"name": ex["tv_box_rent"]["name"], "rub": ex["tv_box_rent"]["fee"]})
    if static_ip:
        monthly.append({"name": ex["static_ip"]["name"], "rub": ex["static_ip"]["fee"]})
    m = sum(x["rub"] for x in monthly)
    promo = t["id"] in p["promo"]["applies_to"]
    first = m - t["fee"] + p["promo"]["first_month_fee"] if promo else m
    return {"monthly": monthly, "monthly_total_rub": m, "one_time": once, "one_time_total_rub": sum(x["rub"] for x in once),
            "first_month_total_rub": first, "promo": f"{p['promo']['name']} до {p['promo']['until']}" if promo else None,
            "router_note": "свой роутер должен иметь гигабитный WAN-порт для тарифов от 500 Мбит/с" if router == "own" else None}


def install_slots(address: str):
    c = find_address(address)
    if not c or c["status"] != "connected":
        return {"error": "монтаж возможен только по подключённым адресам", "address": c}
    booked = [a["slot"] for a in S.applications]
    return [{"index": i, "start": s["start"].isoformat(timespec="minutes"), "end": s["end"].isoformat(timespec="minutes")}
            for i, s in enumerate(slots(booked, S.now() + timedelta(days=c["install_days"] - 1)))]


def book(name: str, phone: str, address: str, tariff_id: str, router: str, tv_box: bool, static_ip: bool, slot_index: int):
    if not name.strip() or len(re.sub(r"\D", "", phone)) < 10:
        return {"error": "нужны имя и телефон клиента"}
    c = find_address(address)
    if not c or c["status"] != "connected":
        return {"error": "адрес не подключён — предложите лист ожидания"}
    free = slots([a["slot"] for a in S.applications], S.now() + timedelta(days=c["install_days"] - 1))
    if not free:
        return {"error": "нет свободных слотов"}
    s = free[min(max(slot_index, 0), len(free) - 1)]
    a = {"id": f"ZAYAVKA-{next(S._ids):04d}", "name": name, "phone": phone, "address": c["address"], "tariff_id": tariff_id,
         "payment": calc(tariff_id, router, tv_box, static_ip), "slot": s, "status": "монтаж записан",
         "next": "SMS с подтверждением и ссылкой на договор — в течение 10 минут; монтажник позвонит за час до приезда"}
    S.applications.append(a)
    return a | {"slot": {k: v.isoformat(timespec="minutes") for k, v in s.items()}}


def wait(name: str, phone: str, address: str):
    w = {"id": f"WAIT-{len(S.waitlist) + 1:04d}", "name": name, "phone": phone, "address": address, "address_info": find_address(address)}
    S.waitlist.append(w)
    return w | {"next": "сообщим по SMS, как только дом будет подключён"}


def tools():
    b = {"type": "boolean"}
    return [
        Tool("check_address", "Проверить, подключён ли дом: connected / construction (с датой) / private (нужен выезд инженера) / none / unknown.",
             {"address": {"type": "string", "description": "улица и дом"}}, check_address),
        Tool("get_tariffs", "Тарифы, доп. услуги и действующая акция с ценами.", {}, tariffs),
        Tool("calc_first_payment", "Точный расчёт: ежемесячный платёж, разовые платежи, первый месяц с учётом акции.",
             {"tariff_id": {"type": "string"}, "router": {"type": "string", "enum": ["rent", "buy", "own"]}, "tv_box": b, "static_ip": b}, calc),
        Tool("get_install_slots", "Свободные слоты монтажа для адреса.", {"address": {"type": "string"}}, install_slots),
        Tool("book_installation", "Записать на монтаж (только после согласия клиента, с именем и телефоном).",
             {"name": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}, "tariff_id": {"type": "string"},
              "router": {"type": "string", "enum": ["rent", "buy", "own"]}, "tv_box": b, "static_ip": b, "slot_index": {"type": "integer"}}, book),
        Tool("add_to_waitlist", "Лист ожидания, если дом не подключён.", {"name": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}}, wait),
        Tool("search_faq", "Ответы на частые вопросы: что нужно для подключения, как проходит монтаж, роутер, оплата, переход от другого провайдера.",
             {"query": {"type": "string"}}, lambda query: get_kb("onboarding/kb").search(query)),
    ]


def answer(history: list[dict]) -> dict:
    system = f"{ROLE}\n\n{(config.DATA / 'onboarding/playbook.md').read_text(encoding='utf-8')}\n\nСегодня {S.now():%d.%m.%Y}."
    text, trace, guard = guarded_run("onboarding", system, history, tools(), numbers_from(S.price), "calc_first_payment")
    allowed = numbers_from(S.price) | numbers_from([t["result"] for t in trace])
    return {"answer": text, **split_next(text), "trace": trace, "trust": price_check(text, allowed), "guard": guard}


class ChatIn(BaseModel):
    messages: list[dict]


@router.post("/chat")
def chat(x: ChatIn):
    return answer(x.messages)


@router.get("/state")
def state():
    return {"applications": S.applications[::-1], "waitlist": S.waitlist[::-1]}


@router.post("/reset")
def do_reset():
    reset()
    return {"ok": True}

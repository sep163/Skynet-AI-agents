"""Кейс «Работа с бизнес-клиентами»: бот НЕ ведёт бизнес-клиента сам. Он собирает данные и проблемы, сверяет понимание
с клиентом и передаёт менеджеру структурированный бриф. Внутри для менеджера — черновик решения с обоснованием;
что и по какой цене предлагать, решает человек."""
import re
from datetime import datetime, timedelta
from itertools import count

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..kb import get_kb
from ..llm import Tool
from .common import guarded_run, load, split_next

router = APIRouter(prefix="/api/b2b", tags=["b2b"])
ROLE = """Ты — ассистент отдела по работе с бизнесом SkyNet на сайте. Твоя задача — не продавать, а быстро и вежливо понять бизнес клиента
и передать персональному менеджеру полный, структурированный запрос, чтобы менеджеру не пришлось переспрашивать.
Отвечай по-русски, деловым, но живым языком, на «вы». Коротко."""
MANAGERS = [{"name": "Елена Воронова", "segment": "малый бизнес и HoReCa"}, {"name": "Павел Крылов", "segment": "сети и корпоративные клиенты"}]
ALIASES = {"коф": "кофейня/ресторан", "кафе": "кофейня/ресторан", "ресторан": "кофейня/ресторан", "coffee": "кофейня/ресторан", "cafe": "кофейня/ресторан",
           "restaurant": "кофейня/ресторан", "офис": "офис", "office": "офис", "интернет-магаз": "интернет-магазин", "e-commerce": "интернет-магазин",
           "online": "интернет-магазин", "магазин": "розничная сеть", "retail": "розничная сеть", "shop": "розничная сеть", "store": "розничная сеть",
           "склад": "склад/логистика", "логист": "склад/логистика", "warehouse": "склад/логистика", "клиник": "клиника", "медиц": "клиника", "clinic": "клиника"}
RULES = [(r"касс|эквайр|оплат|карт", "backup_lte", "касса и эквайринг не должны зависеть от одного канала"),
         (r"падал|авари|простой|отключ|нестабил|пропада", "backup_lte", "были простои связи"),
         (r"гост\w* wi|wi-?fi для гост|гостевой|гости просят|wi-?fi", "wifi_guest", "Wi-Fi для гостей с законной авторизацией"),
         (r"камер|видео|наблюд|видеть зал", "cctv", "видеоконтроль"),
         (r"филиал|между офис|между точк|единая база|общая 1с", "vpn_l2", "связь между точками"),
         (r"сайт|ddos|атак", "ddos", "защита онлайн-сервисов"),
         (r"звон|телефон|запис\w* пациент", "voip", "входящие звонки клиентов"),
         (r"удал[её]нн\w* доступ|сервер|1с извне|статическ", "static_ip", "удалённый доступ к серверу/камерам"),
         (r"\bsla\b|гарант|срок\w* восстанов", "sla_gold", "гарантированные сроки восстановления")]
PER_SITE = {"inet_100", "inet_300", "inet_1000", "backup_lte", "vpn_l2", "wifi_guest"}


class State:
    def __init__(self):
        self.catalog = load("b2b/services.json")
        self.industries = load("b2b/industries.json")
        self.leads = []
        self._ids = count(1)


S = State()


def reset():
    global S
    S = State()


def svc(sid):
    return next((s for s in S.catalog["services"] if s["id"] == sid), None)


def industry(name: str):
    low = name.lower()
    key = next((v for k, v in ALIASES.items() if k in low), None)
    if not key:
        return {"industry": None, "known": list(S.industries)}
    p = S.industries[key]
    return {"industry": key, "critical": p["critical"], "questions": p["questions"]}


def draft_solution(lead: dict) -> dict:
    """Черновик для менеджера: типовые услуги отрасли + правила по словам клиента. Клиент его не видит."""
    text = " ".join([*lead["problems"], *lead["critical_processes"], *lead["requirements"]]).lower()
    locs = max(1, lead["locations"])
    picks = {}
    key = industry(lead["industry"])["industry"]
    for sid in (S.industries[key]["typical"] if key else []):
        picks.setdefault(sid, "типично для отрасли")
    for rx, sid, why in RULES:
        if re.search(rx, text):
            picks[sid] = why
    if locs < 2:
        picks.pop("vpn_l2", None)
    if not any(s.startswith("inet_") for s in picks):
        picks["inet_100" if lead["employees"] < 15 else "inet_300"] = "основной канал"
    cams = re.search(r"(\d+)\s*камер", text)
    lines = []
    for sid, why in picks.items():
        s = svc(sid)
        qty = (int(cams.group(1)) * locs if cams else 2 * locs) if sid == "cctv" else locs if sid in PER_SITE else 1
        lines.append({"service_id": sid, "name": s["name"], "qty": qty, "why": why, "value": s["value"], "monthly_rub": s["monthly"] * qty, "once_rub": s["once"] * qty})
    return {"lines": lines, "monthly_total_rub": sum(l["monthly_rub"] for l in lines), "once_total_rub": sum(l["once_rub"] for l in lines),
            "note": "Черновик ИИ для менеджера: проверьте потребности и состав перед КП."}


def save_brief(company: str, contact_name: str, contact: str, industry_name: str, locations: int, employees: int, current_connection: str,
               critical_processes: list[str], problems: list[str], requirements: list[str], urgency: str, client_confirmed: bool):
    if not client_confirmed:
        return {"error": "сначала кратко перескажите клиенту, что поняли, и получите подтверждение"}
    if not contact.strip():
        return {"error": "нужен телефон или email для связи"}
    mgr = MANAGERS[0] if locations <= 3 and employees < 50 else MANAGERS[1]
    now = datetime.now()
    lead = {"id": f"LEAD-{next(S._ids):04d}", "company": company, "contact_name": contact_name, "contact": contact, "industry": industry_name,
            "locations": locations, "employees": employees, "current_connection": current_connection, "critical_processes": critical_processes,
            "problems": problems, "requirements": requirements, "urgency": urgency, "manager": mgr["name"], "status": "новый — ждёт менеджера",
            "created_at": now.isoformat(timespec="minutes"), "callback_by": (now + timedelta(hours=2)).isoformat(timespec="minutes")}
    lead["draft"] = draft_solution(lead)
    lead["missing"] = [k for k, v in {"текущее подключение": current_connection, "критичные процессы": critical_processes, "проблемы": problems}.items() if not v]
    S.leads.append(lead)
    return {"id": lead["id"], "manager": mgr["name"], "callback_by": lead["callback_by"], "status": lead["status"], "missing_for_manager": lead["missing"]}


def tools():
    arr = {"type": "array", "items": {"type": "string"}}
    return [
        Tool("get_industry_profile", "Что обычно критично для отрасли клиента и какие вопросы стоит задать.", {"industry": {"type": "string"}}, industry),
        Tool("search_b2b_faq", "Общая информация: сроки подключения, договор и ЭДО, SLA, персональный менеджер, публичный Wi-Fi.",
             {"query": {"type": "string"}}, lambda query: get_kb("b2b/kb").search(query)),
        Tool("save_brief", "Передать менеджеру структурированный бриф. Только после того, как клиент подтвердил ваш краткий пересказ (client_confirmed=true).",
             {"company": {"type": "string"}, "contact_name": {"type": "string"}, "contact": {"type": "string", "description": "телефон или email"},
              "industry_name": {"type": "string"}, "locations": {"type": "integer"}, "employees": {"type": "integer"}, "current_connection": {"type": "string"},
              "critical_processes": arr, "problems": arr, "requirements": arr, "urgency": {"type": "string", "enum": ["срочно", "в течение месяца", "планирование"]},
              "client_confirmed": {"type": "boolean"}}, save_brief),
    ]


CONFIRM = re.compile(r"^\W*(да|верно|всё верно|все верно|так|подтверждаю|правильно|ок|ok|угу|да,? всё так)\b", re.I)
BRIEF = {"type": "object", "additionalProperties": False, "properties": {
    "company": {"type": "string"}, "contact_name": {"type": "string"}, "contact": {"type": "string"}, "industry_name": {"type": "string"},
    "locations": {"type": "integer"}, "employees": {"type": "integer"}, "current_connection": {"type": "string"},
    "critical_processes": {"type": "array", "items": {"type": "string"}}, "problems": {"type": "array", "items": {"type": "string"}},
    "requirements": {"type": "array", "items": {"type": "string"}}, "urgency": {"type": "string", "enum": ["срочно", "в течение месяца", "планирование"]}},
    "required": ["company", "contact_name", "contact", "industry_name", "locations", "employees", "current_connection", "critical_processes", "problems", "requirements", "urgency"]}


def extract_brief(history: list[dict]) -> dict:
    """Надёжный путь: бриф извлекается из всего диалога структурированным запросом, не завися от того, вызвала ли модель инструмент."""
    from .. import llm
    dialog = "\n".join(f"{'Клиент' if m['role'] == 'user' else 'Ассистент'}: {m['content']}" for m in history)
    d = llm.complete_json("Извлеки из диалога бриф для менеджера по работе с бизнесом. Только то, что сказал клиент; неизвестное — пустая строка/список/0.", dialog, BRIEF)
    return save_brief(**d, client_confirmed=True)


def answer(history: list[dict]) -> dict:
    system = f"{ROLE}\n\n{(config.DATA / 'b2b/playbook.md').read_text(encoding='utf-8')}"
    text, trace, guard = guarded_run("b2b", system, history, tools(), allowed_base=set(), calc_hint="менеджера: бот не называет цены бизнес-клиенту")
    lead = next((t["result"] for t in reversed(trace) if t["tool"] == "save_brief" and "id" in t["result"]), None)
    asked = len(history) >= 2 and history[-2]["role"] == "assistant" and re.search(r"верно\??|правильно (понял|ли)|как я понял|подтверд", history[-2]["content"], re.I)
    if not lead and (asked or not guard["ok"]) and CONFIRM.search(history[-1]["content"]):
        lead = extract_brief(history)
        trace.append({"tool": "save_brief(extract)", "args": {}, "result": lead})
        if "id" in lead:
            text = (f"Спасибо! Передал ваш запрос {lead['id']} персональному менеджеру — {lead['manager']}. "
                    f"Он изучит задачу и подготовит решение под ваш бизнес.\n\nЧто дальше: {lead['manager']} свяжется с вами до {lead['callback_by'][11:16]} сегодня.")
            guard |= {"ok": True, "fixed_by": "extract_brief"}
    return {"answer": text, **split_next(text), "trace": trace, "guard": guard, "lead": lead if lead and "id" in lead else None}


class ChatIn(BaseModel):
    messages: list[dict]


class DecideIn(BaseModel):
    action: str
    comment: str = ""


@router.post("/chat")
def chat(x: ChatIn):
    return answer(x.messages)


@router.get("/leads")
def leads():
    return S.leads[::-1]


@router.post("/leads/{lid}/decide")
def decide(lid: str, x: DecideIn):
    lead = next((l for l in S.leads if l["id"] == lid), None)
    if not lead:
        raise HTTPException(404)
    lead.update(status={"take": "в работе у менеджера", "proposal": "КП подготовлено менеджером", "more_info": "нужна доп. информация — менеджер позвонит"}.get(x.action, x.action),
                comment=x.comment)
    return lead


@router.post("/reset")
def do_reset():
    reset()
    return {"ok": True}

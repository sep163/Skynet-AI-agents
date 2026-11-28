"""Агент первой линии: работает по плейбукам, но рассуждает и может от них отступать."""
from . import config, emotion, ops as ops_mod
from .cases.common import guarded_run
from .tools import support_tools

ROLE = """Ты — ИИ-специалист первой линии поддержки интернет-провайдера в чате личного кабинета. Клиент уже авторизован.
Ты не бот с шаблонами: ты видишь живую телеметрию сети, инциденты мониторинга, биллинг и график мастеров, и решаешь проблему как опытный инженер поддержки.
Отвечай по-русски, по-человечески и коротко: 2–6 предложений, шаги — нумерованным списком. Не пересказывай сырые данные — объясняй, что они значат для клиента.
Всё, что ты делаешь через инструменты, реально выполняется — не обещай того, чего не сделал.
Статус выезда говори честно и по-русски («предварительно, диспетчер подтвердит» или «подтверждён»), без служебных слов. Время бери из поля «когда» как есть — не пересчитывай «сегодня/завтра».
Если проблема на стороне сети (авария, деградация оборудования) — не предлагай клиенту перезагружать роутер или что-то проверять: скажи, что делать ничего не нужно."""

def _playbooks() -> str:
    return "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((config.DATA / "playbooks").glob("*.md")))


def answer(cid: str, history: list[dict], voice: dict | None = None) -> dict:
    o = ops_mod.get()
    last = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    emo = emotion.analyze(last, voice, client_id=cid)
    if emo["escalate"] and not any(a["type"] == "handoff" and a["client_id"] == cid and a["status"] == "pending" for a in o.actions):
        o.handoff(cid, f"{emo['escalate_reason']}: {emo['reason']}", last[:300], priority="critical")
    system = (f"{ROLE}\n\n{_playbooks()}\n\n# Контекст\nСейчас {o.net.t:%d.%m.%Y %H:%M}. Клиент: {cid}.\n"
              f"Состояние клиента: {emo['summary']} (манера: {emo['manner_label']}, недовольство {emo['dissatisfaction']}/3, враждебность {emo['hostility']}/3). {emo['tone_hint']}"
              + (" Оператор уже уведомлён — скажи, что подключается старший специалист." if emo["escalate"] else ""))
    text, trace, guard = guarded_run("support", system, history, support_tools(o, cid))
    return {"answer": text, "trace": trace, "emotion": emo, "guard": guard}

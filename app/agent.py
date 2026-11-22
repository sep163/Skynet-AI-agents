"""Агент первой линии: работает по плейбукам, но рассуждает и может от них отступать."""
from . import config, emotion, llm, ops as ops_mod
from .tools import support_tools

ROLE = """Ты — ИИ-специалист первой линии поддержки интернет-провайдера в чате личного кабинета. Клиент уже авторизован.
Ты не бот с шаблонами: ты видишь живую телеметрию сети, инциденты мониторинга, биллинг и график мастеров, и решаешь проблему как опытный инженер поддержки.
Отвечай по-русски, по-человечески и коротко: 2–6 предложений, шаги — нумерованным списком. Не пересказывай сырые данные — объясняй, что они значат для клиента.
Всё, что ты делаешь через инструменты, реально выполняется — не обещай того, чего не сделал.
Выезд со статусом tentative — предварительная бронь, ждёт подтверждения диспетчера; confirmed — подтверждён. Говори клиенту честно, какой статус."""

TONE = {0: "", 1: "Клиент недоволен — признай неудобство одной фразой.",
        2: "Клиент раздражён — без шаблонных извинений, сразу конкретика: что видишь и что уже сделал.",
        3: "Клиент в ярости или говорит об уходе — оператор уже уведомлён системой. Коротко покажи, что видишь проблему и что сделано, и скажи, что подключается старший специалист."}


def _playbooks() -> str:
    return "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((config.DATA / "playbooks").glob("*.md")))


def answer(cid: str, history: list[dict], voice: dict | None = None) -> dict:
    o = ops_mod.get()
    last = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    emo = emotion.analyze(last, voice)
    if emo["level"] == 3 and not any(a["type"] == "handoff" and a["client_id"] == cid and a["status"] == "pending" for a in o.actions):
        o.handoff(cid, f"уровень недовольства 3: {emo['reason']}", last[:300], priority="critical")
    system = (f"{ROLE}\n\n{_playbooks()}\n\n# Контекст\nСейчас {o.net.t:%d.%m.%Y %H:%M}. Клиент: {cid}.\n"
              f"Тон последнего сообщения: {emo['level']}/3 ({emo['label']}). {TONE[emo['level']]}")
    text, trace = llm.run_agent(system, history, support_tools(o, cid))
    return {"answer": text, "trace": trace, "emotion": emo}

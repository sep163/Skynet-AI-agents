"""Инструменты агента первой линии. Привязаны к авторизованному клиенту — client_id модель не передаёт и подменить не может."""
from .kb import get_kb
from .llm import Tool
from .network import PARENT
from .ops import CATEGORIES, PRIORITIES, iso

NORMS = {"crc_errors_15m": "норма < 30", "link_drops_15m": "норма 0", "uplink_rx_dbm": "норма от -8 до -18, отказ при -24 и ниже",
         "agg.util": "выше 90% — перегрузка узла", "speed_mbps": "должна быть не ниже скорости тарифа"}


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if k not in ("handled",)}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return iso(x)


def support_tools(ops, cid: str) -> list[Tool]:
    c = ops.client(cid)
    port = c["port"]

    def diagnostics():
        return {"line": ops.net.port_view(port), "tariff_mbps": c["tariff"]["mbps"], "norms": NORMS}

    def incidents():
        incs = ops.sentinel.for_port(port)
        ids = {i["id"] for i in incs}
        return _clean({"incidents": [{k: i.get(k) for k in ("id", "title", "severity", "status", "level", "detected_at", "evidence", "recommendation", "eta_failure_min")} for i in incs],
                       "planned_visits": [visit(j) for j in ops.jobs if j["client_id"] == cid or j["incident_id"] in ids if j["status"] in ("tentative", "confirmed")],
                       "notifications_sent": [n for n in ops.notifications if n["client_id"] == cid][-3:]})

    def when(t):
        d = (t.date() - ops.net.t.date()).days
        return f"{'сегодня' if d == 0 else 'завтра' if d == 1 else t.strftime('%d.%m')} {t:%H:%M}"

    def visit(j):
        return {"мастер": j["tech"], "когда": f"{when(j['start'])}–{j['end']:%H:%M}", "причина": j["reason"],
                "статус": "предварительно, ждёт подтверждения диспетчера" if j["status"] == "tentative" else "подтверждён"}

    def slots():
        return _clean([{"index": i, "start": s["start"], "end": s["end"], "tech": s["tech"]} for i, s in enumerate(ops.free_slots("cable", PARENT[port.split("/")[0]]))])

    return [
        Tool("get_client_profile", "Профиль клиента: имя, тип (b2c/b2b), тариф, адрес, модель роутера, SLA.", {}, lambda: ops.profile(cid)),
        Tool("get_balance", "Баланс, статус услуги (active/blocked), абонплата, использован ли обещанный платёж.", {}, lambda: ops.balance(cid)),
        Tool("get_line_diagnostics", "Живая телеметрия линии клиента: линк, согласованная скорость порта, CRC-ошибки и падения линка за 15 минут, "
             "оптика аплинка дома, загрузка узла агрегации. С нормами для интерпретации.", {}, diagnostics),
        Tool("get_network_incidents", "Инциденты, которые мониторинг уже обнаружил на линии/доме/узле клиента, запланированные выезды и отправленные клиенту уведомления.", {}, incidents),
        Tool("promise_payment", "Включить обещанный платёж на 3 дня (только если услуга заблокирована за неуплату, раз в месяц).", {}, lambda: _clean(ops.promise_payment(cid))),
        Tool("restart_session", "Сбросить сессию абонента на сервере доступа (помогает, если линк есть, а интернета нет).", {}, lambda: _clean(ops.restart_session(cid))),
        Tool("search_kb", "Поиск по базе знаний поддержки: регламенты, инструкции, нормы.",
             {"query": {"type": "string", "description": "Запрос на русском"}}, lambda query: get_kb().search(query)),
        Tool("get_visit_slots", "Ближайшие свободные слоты мастера для адреса клиента.", {}, slots),
        Tool("book_technician_visit", "Забронировать выезд мастера в выбранный слот. Бронь предварительная — диспетчер подтверждает.",
             {"reason": {"type": "string", "description": "Что сломано и что уже проверено"}, "slot_index": {"type": "integer", "description": "index из get_visit_slots, 0 — ближайший"}},
             lambda reason, slot_index: _clean(ops.request_visit(cid, reason, slot_index))),
        Tool("create_ticket", "Создать заявку во 2 линию (когда нужен специалист, но не выезд).",
             {"category": {"type": "string", "enum": CATEGORIES}, "priority": {"type": "string", "enum": PRIORITIES}, "summary": {"type": "string"}},
             lambda category, priority, summary: _clean(ops.create_ticket(cid, category, priority, summary))),
        Tool("handoff_to_operator", "Передать диалог живому оператору с резюме: что выяснено и что сделано.",
             {"reason": {"type": "string"}, "summary": {"type": "string"}}, lambda reason, summary: _clean(ops.handoff(cid, reason, summary))),
    ]

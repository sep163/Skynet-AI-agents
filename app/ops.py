"""Операционный слой: биллинг (мок), график мастеров, очередь действий с политикой автономности, автореакция на инциденты.

Политика ~90% автономности: безопасные действия ИИ выполняет сам (AUTO),
всё, что тратит время людей или трогает магистраль, уходит диспетчеру на одобрение (APPROVAL).
"""
import json
from datetime import datetime, timedelta
from itertools import count

from . import config
from .network import AGGS, BUILDINGS, PARENT, PORTS, Network, level
from .sentinel import Sentinel

AUTO = {"notify_clients", "restart_session", "promise_payment", "open_ticket"}
APPROVAL = {"dispatch_technician", "noc_task", "handoff"}
SKILL = {"cable_degradation": "cable", "flapping": "cable", "cable_fault": "cable", "speed_mismatch": "cable", "client_request": "cable",
         "switch_down": "switch", "optic_degradation": "fiber", "agg_down": "fiber"}
DURATION = {"cable": 60, "switch": 90, "fiber": 120}
CATEGORIES = ["no_internet", "slow_speed", "wifi", "payment", "equipment", "tariff", "other"]
PRIORITIES = ["low", "normal", "high", "critical"]


def _load(name):
    return json.loads((config.DATA / name).read_text(encoding="utf-8"))


def iso(x):
    return x.isoformat(timespec="minutes") if isinstance(x, datetime) else x


class Ops:
    def __init__(self, seed: int = config.SIM_SEED, noise: bool = True):
        self.clients = {c["id"]: c for c in _load("clients.json")}
        self.by_port = {c["port"]: c for c in self.clients.values()}
        self.techs = _load("technicians.json")
        self.net = Network(seed=seed, noise=noise)
        self.sentinel = Sentinel(self.net, tariff_of=lambda p: self.by_port[p]["tariff"]["mbps"] if p in self.by_port else 500)
        self.actions, self.jobs, self.tickets, self.notifications = [], [], [], []
        self._aid, self._jid, self._tid = count(1), count(1), count(1)
        self.autonomous = True
        day = self.net.t.date()
        for tc in self.techs:
            for a, b in tc.get("busy", []):
                at = lambda x: datetime.combine(day, datetime.strptime(x, "%H:%M").time())
                self.book({"tech_id": tc["id"], "tech": tc["name"], "start": at(a), "end": at(b)}, tc["skills"][0], "Плановые работы (подключения)", status="confirmed")

    # --- время ---------------------------------------------------------------
    def tick(self, minutes: int = 1):
        for _ in range(minutes):
            self.net.step(1)
            self.sentinel.scan()
            for inc in list(self.sentinel.open.values()):
                if inc["kind"] not in inc.setdefault("handled", []):
                    inc["handled"].append(inc["kind"])
                    self.respond(inc)

    # --- клиенты и биллинг -----------------------------------------------------
    def client(self, cid):
        c = self.clients.get(cid)
        if not c:
            raise KeyError(f"клиент {cid} не найден")
        return c

    def profile(self, cid):
        c = self.client(cid)
        sw = c["port"].split("/")[0]
        return {k: c[k] for k in ("id", "name", "type", "tariff", "router")} | {"address": f"{BUILDINGS[sw]}, {c['flat']}", "port": c["port"], "sla": c.get("sla")}

    def balance(self, cid):
        c = self.client(cid)
        return {"balance_rub": c["balance"], "status": c["status"], "monthly_fee_rub": c["tariff"]["fee"], "promise_payment_used": c.get("promise_used", False)}

    def promise_payment(self, cid):
        c = self.client(cid)
        if c["status"] != "blocked":
            return {"ok": False, "reason": "услуга не заблокирована, обещанный платёж не нужен"}
        if c.get("promise_used"):
            return {"ok": False, "reason": "обещанный платёж уже использован в этом месяце"}
        c.update(status="active", promise_used=True)
        return self.propose("promise_payment", "Обещанный платёж на 3 дня", client_id=cid, source="agent",
                            details={"until": iso(self.net.t + timedelta(days=3))})

    def restart_session(self, cid):
        c = self.client(cid)
        v = self.net.port_view(c["port"])
        if c["status"] == "blocked":
            return {"ok": False, "reason": "услуга заблокирована за неуплату"}
        if not v["switch_reachable"] or not v["link"]:
            return {"ok": False, "reason": "нет линка на порту — сброс сессии не поможет, проблема в линии/оборудовании", "line": v}
        return self.propose("restart_session", "Сброс сессии абонента", client_id=cid, source="agent")

    def clients_on(self, target):
        lv = level(target)
        sws = [target] if lv == "switch" else AGGS.get(target, [])
        if lv == "port":
            cl = [c for c in self.clients.values() if c["port"] == target]
            return cl, 1
        return [c for c in self.clients.values() if c["port"].split("/")[0] in sws], PORTS * len(sws)

    # --- график мастеров --------------------------------------------------------
    def free_slots(self, skill: str, zone: str, after: datetime | None = None, before: datetime | None = None, n: int = 3):
        after = after or self.net.t
        dur = timedelta(minutes=DURATION[skill])
        res = []
        for d in range(3):
            day = (after + timedelta(days=d)).date()
            for tech in self.techs:
                if skill not in tech["skills"] or zone not in tech["zones"]:
                    continue
                s = datetime.combine(day, datetime.strptime(tech["shift"][0], "%H:%M").time())
                end = datetime.combine(day, datetime.strptime(tech["shift"][1], "%H:%M").time())
                if s < after:
                    s = after.replace(second=0, microsecond=0) + timedelta(minutes=(30 - after.minute % 30) % 30)
                while s + dur <= end:
                    busy = any(j["tech_id"] == tech["id"] and j["status"] != "cancelled" and s < j["end"] and j["start"] < s + dur for j in self.jobs)
                    if not busy and (before is None or s <= before - timedelta(minutes=30)):
                        res.append({"tech_id": tech["id"], "tech": tech["name"], "start": s, "end": s + dur})
                        break
                    s += timedelta(minutes=30)
        return sorted(res, key=lambda x: x["start"])[:n]

    def book(self, slot, skill, reason, incident_id=None, client_id=None, address=None, status="tentative"):
        j = {"id": f"JOB-{next(self._jid):03d}", "tech_id": slot["tech_id"], "tech": slot["tech"], "start": slot["start"], "end": slot["end"],
             "skill": skill, "reason": reason, "incident_id": incident_id, "client_id": client_id, "address": address, "status": status}
        self.jobs.append(j)
        return j

    # --- очередь действий -------------------------------------------------------
    def propose(self, type_, title, source="sentinel", incident_id=None, client_id=None, details=None, priority="normal"):
        a = {"id": f"ACT-{next(self._aid):04d}", "type": type_, "title": title, "source": source, "incident_id": incident_id, "client_id": client_id,
             "details": details or {}, "priority": priority, "created_at": self.net.t, "status": "pending"}
        self.actions.append(a)
        if type_ in AUTO and self.autonomous:
            self._execute(a)
            a["status"] = "done"
        return a

    def decide(self, action_id: str, approve: bool, comment: str = ""):
        a = next((x for x in self.actions if x["id"] == action_id), None)
        if not a or a["status"] != "pending":
            raise ValueError("действие не найдено или уже обработано")
        a.update(status="approved" if approve else "rejected", decided_at=self.net.t, comment=comment)
        if approve:
            self._execute(a)
        elif a["details"].get("job_id"):
            self._job(a["details"]["job_id"])["status"] = "cancelled"
        return a

    def _job(self, jid):
        return next(j for j in self.jobs if j["id"] == jid)

    def _execute(self, a):
        d = a["details"]
        if a["type"] == "notify_clients":
            for cid in d.get("client_ids", []):
                self.notifications.append({"client_id": cid, "t": self.net.t, "incident_id": a["incident_id"], "text": d["text"]})
        elif a["type"] == "dispatch_technician" and d.get("job_id"):
            j = self._job(d["job_id"])
            j["status"] = "confirmed"
            if j["client_id"]:
                self.notifications.append({"client_id": j["client_id"], "t": self.net.t, "incident_id": a["incident_id"],
                                           "text": f"Мастер {j['tech']} приедет {j['start']:%d.%m с %H:%M} до {j['end']:%H:%M}. Причина: {j['reason']}"})

    def complete_job(self, jid):
        j = self._job(jid)
        j["status"] = "done"
        inc = next((i for i in self.sentinel.incidents if i["id"] == j["incident_id"]), None)
        self.net.fix(inc["target"] if inc else self.client(j["client_id"])["port"])
        return j

    def handoff(self, cid, reason, summary, priority="high"):
        return self.propose("handoff", f"Передать диалог оператору: {reason[:60]}", source="agent", client_id=cid,
                            details={"summary": summary}, priority=priority)

    # --- тикеты от агента ---------------------------------------------------------
    def create_ticket(self, cid, category, priority, summary):
        t = {"id": f"T-{next(self._tid):04d}", "client_id": cid, "category": category, "priority": priority, "summary": summary, "t": self.net.t}
        self.tickets.append(t)
        self.propose("open_ticket", f"Заявка {t['id']}: {summary[:60]}", source="agent", client_id=cid, details={"ticket_id": t["id"]}, priority=priority)
        return t

    def request_visit(self, cid, reason, slot_index=0):
        c = self.client(cid)
        port_incs = [i for i in self.sentinel.for_port(c["port"]) if i["status"] == "open" and i["level"] == "port"]
        existing = next((j for j in self.jobs if j["client_id"] == cid and j["status"] in ("tentative", "confirmed")), None)
        if existing:
            return {"already_planned": True, "job": existing}
        slots = self.free_slots("cable", PARENT[c["port"].split("/")[0]])
        if not slots:
            return {"ok": False, "reason": "нет свободных слотов на 3 дня — передайте оператору"}
        s = slots[min(slot_index, len(slots) - 1)]
        j = self.book(s, "cable", reason, incident_id=port_incs[0]["id"] if port_incs else None, client_id=cid, address=self.profile(cid)["address"])
        a = self.propose("dispatch_technician", f"Выезд к {c['name']}: {reason[:60]}", source="agent", incident_id=j["incident_id"], client_id=cid,
                         details={"job_id": j["id"], "slot": {"start": s["start"], "end": s["end"], "tech": s["tech"]}},
                         priority="critical" if c["type"] == "b2b" else "high")
        return {"ok": True, "job": j, "action_id": a["id"], "status": "ждёт подтверждения диспетчера", "other_slots": slots[1:]}

    # --- автореакция на инцидент -------------------------------------------------------
    def respond(self, inc):
        cl, subs = self.clients_on(inc["target"])
        ids = [c["id"] for c in cl]
        b2b = any(c["type"] == "b2b" for c in cl)
        k, tg = inc["kind"], inc["target"]
        zone = PARENT.get(tg.split("/")[0], tg)
        addr = BUILDINGS.get(tg.split("/")[0], tg)
        if k in ("cable_degradation", "flapping", "speed_mismatch") and ids:
            txt = {"speed_mismatch": "Мы видим, что ваше подключение работает на 100 Мбит/с вместо скорости тарифа. Проверьте, что кабель плотно вставлен в WAN-порт роутера; если не поможет — напишите в чат, назначим мастера.",
                   }.get(k, "Наш мониторинг заметил ошибки на вашей линии. Возможны кратковременные обрывы — мы уже планируем визит мастера, напишем время.")
            self.propose("notify_clients", f"Уведомить клиента о проблеме на линии ({tg})", incident_id=inc["id"], details={"client_ids": ids, "text": txt})
        if k in ("switch_down", "agg_down"):
            txt = f"В вашем доме ({addr}) авария на оборудовании, бригада уже в работе. Звонить в поддержку не нужно — пришлём сообщение, когда связь восстановится."
            self.propose("notify_clients", f"Уведомить {len(ids)} клиентов с карточками (всего ~{subs} абонентов) об аварии", incident_id=inc["id"],
                         details={"client_ids": ids, "text": txt, "subscribers": subs}, priority="critical")
        if k == "congestion":
            self.propose("noc_task", f"Перегрузка {tg}: перераспределить трафик/расширить аплинк", incident_id=inc["id"], details={"evidence": inc["evidence"]}, priority="high")
        skill = SKILL.get(k)
        if not skill or (k == "speed_mismatch") or any(j["incident_id"] == inc["id"] and j["status"] != "cancelled" for j in self.jobs):
            return
        before = self.net.t + timedelta(minutes=inc["eta_failure_min"]) if inc.get("eta_failure_min") else None
        urgent = inc["severity"] == "critical" or b2b or before is not None
        slots = self.free_slots(skill, zone, before=before) or self.free_slots(skill, zone)
        if not slots:
            self.propose("noc_task", f"Нет свободных мастеров ({skill}) для {inc['id']}", incident_id=inc["id"], priority="critical")
            return
        s = slots[0]
        j = self.book(s, skill, inc["title"], incident_id=inc["id"], client_id=ids[0] if level(tg) == "port" and ids else None, address=addr)
        late = before is not None and s["start"] > before - timedelta(minutes=30)
        self.propose("dispatch_technician", f"{'СРОЧНО: ' if urgent else ''}{inc['title']} — {addr}", incident_id=inc["id"],
                     client_id=j["client_id"], priority="critical" if urgent else "normal",
                     details={"job_id": j["id"], "slot": {"start": s["start"], "end": s["end"], "tech": s["tech"]}, "evidence": inc["evidence"],
                              "deadline": before, "warning": "ближайший слот позже прогнозируемого отказа" if late else None,
                              "alternatives": [{"start": x["start"], "tech": x["tech"]} for x in slots[1:]]})


ops = Ops()


def reset(seed: int = config.SIM_SEED):
    global ops
    ops = Ops(seed=seed)
    return ops


def get() -> Ops:
    return ops

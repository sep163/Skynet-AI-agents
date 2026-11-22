"""Симулятор сети провайдера: топология, поминутная телеметрия, инъекция неисправностей.

В проде этот модуль заменяется адаптером к SNMP/Zabbix/NetFlow с тем же интерфейсом:
history(obj) -> список поминутных снимков, t -> текущее время.
"""
import math
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta

AGGS = {"agg-1": ["sw-01", "sw-02", "sw-03"], "agg-2": ["sw-04", "sw-05", "sw-06"]}
BUILDINGS = {
    "sw-01": "ул. Савушкина, 12", "sw-02": "ул. Савушкина, 14", "sw-03": "Комендантский пр., 7",
    "sw-04": "пр. Энгельса, 33", "sw-05": "Выборгское ш., 5", "sw-06": "БЦ «Лиговский», Лиговский пр., 50",
}
PARENT = {sw: agg for agg, sws in AGGS.items() for sw in sws}
PORTS = 24
HIST = 240
RX_FAIL = -24.0
START = datetime(2026, 11, 28, 9, 0)
FAULT_KINDS = ["cable_degradation", "speed_downgrade", "optic_degradation", "switch_power", "congestion"]


def pid(sw, n):
    return f"{sw}/{n}"


def all_ports():
    return [pid(sw, n) for sw in PARENT for n in range(1, PORTS + 1)]


def level(target: str) -> str:
    return "port" if "/" in target else "switch" if target.startswith("sw") else "agg"


class Network:
    def __init__(self, seed: int = 7, start: datetime = START, noise: bool = True):
        self.rng, self.t, self.noise = random.Random(seed), start, noise
        self.faults: list[dict] = []
        self.ports = {p: {"link": True, "speed": 1000, "crc": 0} for p in all_ports()}
        self.switches = {sw: {"reachable": True, "rx_base": -8 + self.rng.uniform(-1.5, 1.5), "rx_dbm": None} for sw in PARENT}
        self.aggs = {a: {"util": 40.0, "latency_ms": 3.0, "loss": 0.0} for a in AGGS}
        self.idle: dict[str, int] = {}
        self.hist = defaultdict(lambda: deque(maxlen=HIST))
        self._baseline()
        self._record()

    def inject(self, kind: str, target: str, at: datetime | None = None) -> dict:
        start = at or self.t
        sw = self.switches.get(target)
        lag = {"cable_degradation": 40, "speed_downgrade": 0, "switch_power": 0, "congestion": 0,
               "optic_degradation": math.ceil((sw["rx_base"] - RX_FAIL) / 0.12) if sw else 0}[kind]
        f = {"id": f"F{len(self.faults) + 1}", "kind": kind, "target": target, "start": start,
             "impact_at": start + timedelta(minutes=lag), "fixed_at": None}
        self.faults.append(f)
        return f

    def fix(self, target: str):
        for f in self.faults:
            if f["target"] == target and not f["fixed_at"]:
                f["fixed_at"] = self.t

    def step(self, minutes: int = 1):
        for _ in range(minutes):
            self.t += timedelta(minutes=1)
            self._baseline()
            for f in self.faults:
                self._apply(f)
            self._record()

    def _baseline(self):
        h = self.t.hour + self.t.minute / 60
        load = 30 + 50 * math.exp(-(((h - 21) / 3) ** 2))
        for a in self.aggs.values():
            u = min(99.0, load + self.rng.uniform(-4, 4))
            a.update(util=u, latency_ms=3 + max(0, u - 80) * 0.4, loss=0.0)
        for s in self.switches.values():
            s.update(reachable=True, rx_dbm=s["rx_base"] + self.rng.uniform(-0.3, 0.3))
        for p, s in self.ports.items():
            s.update(speed=1000, crc=self.rng.choice((0,) * 40 + (1, 2, 5)) if self.noise else 0)
            if self.noise and p not in self.idle and self.rng.random() < 0.0005:
                self.idle[p] = self.rng.randint(10, 120)
            if p in self.idle:
                self.idle[p] -= 1
                s["link"] = False
                if self.idle[p] <= 0:
                    del self.idle[p]
            else:
                s["link"] = True

    def _apply(self, f):
        e = (self.t - f["start"]).total_seconds() / 60
        if e < 0 or f["fixed_at"]:
            return
        k, tg = f["kind"], f["target"]
        if k == "cable_degradation":
            s = self.ports[tg]
            s["crc"] += int(3 + 2.5 * e)
            s["link"] = False if e >= 70 else (e % 6) >= 2 if e >= 40 else s["link"]
        elif k == "speed_downgrade":
            self.ports[tg]["speed"] = 100
        elif k == "optic_degradation":
            s = self.switches[tg]
            s["rx_dbm"] = s["rx_base"] - 0.12 * e + self.rng.uniform(-0.3, 0.3)
            if s["rx_dbm"] <= RX_FAIL:
                s["reachable"] = False
        elif k == "switch_power":
            self.switches[tg]["reachable"] = False
        elif k == "congestion":
            self.aggs[tg].update(util=97.0 + self.rng.uniform(-1, 1), latency_ms=45 + self.rng.uniform(0, 15), loss=1.5 + self.rng.uniform(0, 1))

    def _record(self):
        for sw, s in self.switches.items():
            self.hist[sw].append({"t": self.t, "reachable": s["reachable"], "rx_dbm": round(s["rx_dbm"], 2) if s["reachable"] else None})
        for a, s in self.aggs.items():
            self.hist[a].append({"t": self.t, **{k: round(v, 2) for k, v in s.items()}})
        for p, s in self.ports.items():
            ok = self.switches[p.split("/")[0]]["reachable"]
            self.hist[p].append({"t": self.t, **s} if ok else None)

    def port_view(self, p: str) -> dict:
        h = list(self.hist[p])[-15:]
        last = h[-1]
        sw = p.split("/")[0]
        vals = [x for x in h if x]
        flaps = sum(1 for a, b in zip(vals, vals[1:]) if a["link"] and not b["link"])
        return {"port": p, "switch_reachable": last is not None, "link": last and last["link"], "speed_mbps": last and last["speed"],
                "crc_errors_15m": sum(x["crc"] for x in vals), "link_drops_15m": flaps,
                "uplink_rx_dbm": self.hist[sw][-1]["rx_dbm"], "agg": {k: v for k, v in self.hist[PARENT[sw]][-1].items() if k != "t"}}

    def summary(self) -> dict:
        return {"t": self.t, "switches": [{"id": sw, "building": BUILDINGS[sw], "agg": PARENT[sw], **{k: v for k, v in self.hist[sw][-1].items() if k != "t"},
                                          "ports_down": sum(1 for n in range(1, PORTS + 1) if not self.ports[pid(sw, n)]["link"])} for sw in PARENT],
                "aggs": [{"id": a, **{k: v for k, v in self.hist[a][-1].items() if k != "t"}} for a in AGGS]}

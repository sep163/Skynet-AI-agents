"""Сентинел: непрерывно сканирует телеметрию и открывает инциденты до того, как клиенты начнут жаловаться.

Правила намеренно прозрачные: дежурный видит, почему сработало (evidence), и что предлагается сделать.
"""
from datetime import timedelta
from itertools import count
from statistics import mean

from .network import AGGS, PARENT, PORTS, RX_FAIL, all_ports, level

SEV = {"warning": 1, "major": 2, "critical": 3}
CABLE = {"cable_degradation", "flapping", "cable_fault"}
INFO = {
    "cable_degradation": ("Деградация кабеля до клиента", "Выезд монтажника: проверить/переобжать кабель и патч-корд до отказа линии"),
    "flapping": ("Линк клиента флапает", "Выезд монтажника: кабель или разъём, линия скоро упадёт"),
    "cable_fault": ("Линия клиента упала после роста ошибок", "Срочный выезд монтажника: замена кабеля/разъёма"),
    "speed_mismatch": ("Порт согласовался ниже тарифа", "Удалённо: попросить клиента проверить кабель/WAN-порт роутера; если не помогло — выезд"),
    "optic_degradation": ("Падает уровень оптики на аплинке коммутатора", "Плановая замена/чистка SFP и патч-корда до отказа (бригада ВОЛС)"),
    "switch_down": ("Коммутатор недоступен", "Проверить питание в доме/УПС, выезд к коммутатору"),
    "agg_down": ("Недоступен узел агрегации", "Авария магистрали: дежурный инженер NOC + выезд"),
    "congestion": ("Перегрузка узла агрегации", "Перераспределить трафик / расширить аплинк (инженер NOC)"),
}


def slope(ys):
    n = len(ys)
    xm, ym = (n - 1) / 2, mean(ys)
    d = sum((i - xm) ** 2 for i in range(n))
    return sum((i - xm) * (y - ym) for i, y in enumerate(ys)) / d if d else 0.0


class Sentinel:
    def __init__(self, net, tariff_of=lambda port: 500):
        self.net, self.tariff_of = net, tariff_of
        self.open: dict[str, dict] = {}
        self.incidents: list[dict] = []
        self._ids = count(1)

    def scan(self) -> list[dict]:
        """Один проход. Возвращает только что открытые инциденты."""
        now, new = self.net.t, []
        for target, kind, sev, ev, extra in self._alerts():
            inc = self.open.get(target)
            if not inc:
                inc = {"id": f"INC-{next(self._ids):04d}", "target": target, "level": level(target), "kind": kind, "first_kind": kind,
                       "severity": sev, "status": "open", "detected_at": now, "evidence": ev}
                self.open[target] = inc
                self.incidents.append(inc)
                new.append(inc)
            elif SEV[sev] >= SEV[inc["severity"]]:
                if inc["kind"] != kind:
                    inc["escalated_at"] = now
                inc.update(kind=kind, severity=sev, evidence=ev)
            inc.update(updated_at=now, title=INFO[inc["kind"]][0], recommendation=INFO[inc["kind"]][1], **extra)
        for target, inc in list(self.open.items()):
            if now - inc["updated_at"] >= timedelta(minutes=15):
                inc.update(status="resolved", resolved_at=now)
                del self.open[target]
        return new

    def for_port(self, port: str, since_min: int = 180) -> list[dict]:
        sw = port.split("/")[0]
        keys = {port, sw, PARENT[sw]}
        return [i for i in self.incidents if i["target"] in keys and (i["status"] == "open" or self.net.t - i["updated_at"] <= timedelta(minutes=since_min))]

    def _alerts(self):
        h, out = self.net.hist, []
        down = {sw for sw in PARENT if all(x["reachable"] is False for x in list(h[sw])[-2:])}
        for agg, sws in AGGS.items():
            if set(sws) <= down:
                out.append((agg, "agg_down", "critical", f"Не отвечают все {len(sws)} коммутатора узла", {}))
            a = list(h[agg])[-10:]
            if len(a) == 10 and all(x["util"] >= 90 for x in a) and a[-1]["latency_ms"] > 20:
                out.append((agg, "congestion", "major", f"Загрузка {a[-1]['util']:.0f}% 10+ мин, задержка {a[-1]['latency_ms']:.0f} мс, потери {a[-1]['loss']:.1f}%", {}))
        for sw in PARENT:
            if sw in down and not set(AGGS[PARENT[sw]]) <= down:
                out.append((sw, "switch_down", "critical", f"Коммутатор {sw} не отвечает 2+ мин, {PORTS} абонентских портов без связи", {}))
            rx = [x["rx_dbm"] for x in list(h[sw])[-30:] if x["rx_dbm"] is not None]
            if len(rx) >= 20 and sw not in down:
                k = slope(rx)
                eta = (RX_FAIL - rx[-1]) / k if k < -0.01 else None
                if rx[-1] <= -20 or (eta is not None and eta <= 180):
                    ev = f"RX {rx[-1]:.1f} дБм, тренд {k * 60:+.1f} дБм/ч" + (f", отказ через ~{eta:.0f} мин" if eta else "")
                    out.append((sw, "optic_degradation", "major" if eta and eta < 60 else "warning", ev,
                                {"eta_failure_min": round(eta) if eta else None}))
        for p in all_ports():
            w = [x for x in list(h[p])[-15:] if x]
            if not w or not list(h[p])[-1]:
                continue
            last, crc = w[-1], [x["crc"] for x in w]
            m5, prev = mean(crc[-5:]), mean(crc[-10:-5]) if len(crc) >= 10 else 0
            drops = sum(1 for a, b in zip(w, w[1:]) if a["link"] and not b["link"])
            prior = self.open.get(p, {}).get("kind") in CABLE
            if not last["link"] and (prior or sum(crc) > 150):
                out.append((p, "cable_fault", "critical", f"Линк упал, CRC за 15 мин: {sum(crc)}", {}))
            elif drops >= 3:
                out.append((p, "flapping", "major", f"{drops} падений линка за 15 мин, CRC: {sum(crc)}", {}))
            elif m5 >= 15 or (m5 >= 8 and m5 > 3 * prev + 1):
                out.append((p, "cable_degradation", "warning", f"CRC {m5:.0f}/мин (было {prev:.0f}/мин), растут", {}))
            tariff = self.tariff_of(p)
            if last["link"] and all(x["link"] and x["speed"] < tariff for x in w[-3:]) and len(w) >= 3:
                out.append((p, "speed_mismatch", "warning", f"Порт {last['speed']} Мбит/с при тарифе {tariff} Мбит/с", {}))
        return out

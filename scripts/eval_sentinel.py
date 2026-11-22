"""Оценка Сентинела на симуляции: случайные неисправности + шум, несколько сидов.

Метрики: recall по типам, precision (ложные инциденты), выигрыш во времени относительно первой жалобы клиента
и (для оптики) относительно момента отказа. Запуск: python -m scripts.eval_sentinel --seeds 20
"""
import argparse
import json
import random
from datetime import timedelta
from statistics import median

from app.network import AGGS, PARENT, START, Network, all_ports
from app.sentinel import Sentinel

COMPLAINT_DELAY = {"cable_degradation": (5, 25), "speed_downgrade": (30, 300), "optic_degradation": (3, 10), "switch_power": (3, 10), "congestion": (10, 40)}
MATCH = {"cable_degradation": {"cable_degradation", "flapping", "cable_fault"}, "speed_downgrade": {"speed_mismatch"},
         "optic_degradation": {"optic_degradation"}, "switch_power": {"switch_down", "agg_down"}, "congestion": {"congestion"}}


def run(seed, minutes=720, n_faults=10):
    rng = random.Random(1000 + seed)
    net = Network(seed=seed)
    sen = Sentinel(net)
    used, plan = set(), []
    for _ in range(n_faults):
        kind = rng.choice(list(MATCH))
        target = {"cable_degradation": all_ports, "speed_downgrade": all_ports}.get(kind, lambda: list(PARENT) if kind != "congestion" else list(AGGS))()
        target = rng.choice(target)
        sw = target.split("/")[0]
        if target in used or sw in used or (kind in ("optic_degradation", "switch_power") and any(u.startswith(sw) for u in used)):
            continue
        used.add(target)
        plan.append((rng.randint(10, minutes - 240), kind, target))
    plan.sort()
    for m in range(minutes):
        for at, kind, target in plan:
            if at == m:
                f = net.inject(kind, target)
                lo, hi = COMPLAINT_DELAY[kind]
                f["complaint_at"] = f["impact_at"] + timedelta(minutes=rng.randint(lo, hi))
        net.step(1)
        sen.scan()
    rows, matched = [], set()
    for f in net.faults:
        inc = next((i for i in sen.incidents if (i["target"] == f["target"] or (f["kind"] == "switch_power" and i["target"] == PARENT.get(f["target"])))
                    and i["first_kind"] in MATCH[f["kind"]] | {"switch_down"} and i["detected_at"] >= f["start"]), None)
        if inc:
            matched.add(inc["id"])
        rows.append({"seed": seed, "kind": f["kind"], "target": f["target"], "detected": inc is not None,
                     "lead_vs_complaint_min": (f["complaint_at"] - inc["detected_at"]).total_seconds() / 60 if inc else None,
                     "lead_vs_failure_min": (f["impact_at"] - inc["detected_at"]).total_seconds() / 60 if inc else None})
    def explained(i):
        return any(f["start"] <= i["detected_at"] and (f["target"] == i["target"] or PARENT.get(f["target"].split("/")[0]) == i["target"]
                                                       or f["target"].startswith(i["target"] + "/")) for f in net.faults)
    fp = [i for i in sen.incidents if i["id"] not in matched and not explained(i)]
    return rows, fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--minutes", type=int, default=720)
    a = ap.parse_args()
    rows, fps = [], []
    for s in range(a.seeds):
        r, fp = run(s, a.minutes)
        rows += r
        fps += [{"seed": s, "target": i["target"], "kind": i["kind"], "evidence": i["evidence"]} for i in fp]
    by = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    report = {"seeds": a.seeds, "hours_per_seed": a.minutes / 60, "faults": len(rows), "false_incidents": len(fps),
              "false_per_day": round(len(fps) / (a.seeds * a.minutes / 1440), 2), "by_kind": {}}
    print(f"{'тип неисправности':22} {'n':>4} {'recall':>7} {'выигрыш до жалобы, мин (медиана)':>34} {'до отказа':>10}")
    for k, rs in sorted(by.items()):
        det = [r for r in rs if r["detected"]]
        lc = median(r["lead_vs_complaint_min"] for r in det) if det else None
        lf = median(r["lead_vs_failure_min"] for r in det) if det else None
        report["by_kind"][k] = {"n": len(rs), "recall": round(len(det) / len(rs), 3), "median_lead_vs_complaint_min": lc, "median_lead_vs_failure_min": lf,
                                "detected_before_first_complaint": round(sum(r["lead_vs_complaint_min"] > 0 for r in det) / max(len(det), 1), 3)}
        print(f"{k:22} {len(rs):>4} {len(det) / len(rs):>7.0%} {lc if lc is not None else '-':>34} {lf if lf is not None else '-':>10}")
    det = [r for r in rows if r["detected"]]
    report["overall"] = {"recall": round(len(det) / len(rows), 3), "before_first_complaint": round(sum(r["lead_vs_complaint_min"] > 0 for r in det) / max(len(det), 1), 3)}
    print(f"\nВсего: recall {report['overall']['recall']:.0%}, обнаружено до первой жалобы: {report['overall']['before_first_complaint']:.0%}, "
          f"ложных инцидентов: {len(fps)} ({report['false_per_day']}/сутки на 144 порта)")
    open("reports/sentinel_eval.json", "w").write(json.dumps({**report, "false_examples": fps[:20]}, ensure_ascii=False, indent=1, default=str))
    print("→ reports/sentinel_eval.json")


if __name__ == "__main__":
    main()

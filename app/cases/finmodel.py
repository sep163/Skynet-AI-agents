"""Финмодель по циклу тестирования гипотез: 1 исследование (источники) → 2 гипотеза → 3 предпосылки → 4 модель → 5 самые/наименее доходные функции.
Все предпосылки в data/finmodel/assumptions.json с пометкой источника; любую можно переопределить и пересчитать."""
import copy
import json

from fastapi import APIRouter
from pydantic import BaseModel

from .common import load

router = APIRouter(prefix="/api/finmodel", tags=["finmodel"])
YEAR_RAMP = 78  # сумма 1..12: накопление ежемесячных когорт за первый год


def _v(x):
    return x["value"] if isinstance(x, dict) and "value" in x else x


def _set(d, path, val):
    *head, last = path.split(".")
    for k in head:
        d = d[k]
    if isinstance(d.get(last), dict) and "value" in d[last]:
        d[last] = {**d[last], "value": val, "kind": "override"}
    else:
        d[last] = val


def compute(model: str = "claude-haiku-5-5", stt: str = "local_whisper_rub_min", overrides: dict | None = None) -> dict:
    A = copy.deepcopy(load("finmodel/assumptions.json"))
    sal = load("finmodel/salaries.json")
    for k, v in (overrides or {}).items():
        _set(A, k, v)
    c, L, D, B = A["company"], A["llm"], A["directions"], A["build"]
    loaded = lambda role: sal["roles"][role]["hh"]["median"] * _v(c["payroll_overhead"]) * _v(c["workplace_overhead"])
    prod = _v(c["productive_min_month"])
    subs = _v(c["revenue_rub_year"]) * _v(c["b2c_revenue_share"]) / 12 / _v(c["arpu_rub_month"])
    contacts = subs * _v(c["contacts_per_sub_month"])
    aht = _v(c["aht_min"])
    price = L[model]
    dialog_cost = lambda tin, tout: tin / 1000 * price["in"] + tout / 1000 * price["out"]
    chat_cost = dialog_cost(_v(L["tokens_in_per_dialog"]), _v(L["tokens_out_per_dialog"]))
    dev_loaded = sal["roles"]["python_dev"]["hh"]["p75"] * _v(c["payroll_overhead"]) * _v(c["workplace_overhead"])
    build_cost = _v(B["dev_months"]) * _v(B["dev_fte"]) * dev_loaded
    run_month = _v(B["run_fte"]) * dev_loaded
    out = []

    def row(key, fte, savings, extra_year, llm, other_cost=0.0, risk=0.0, details=None):
        d = D[key]
        net_month = savings + risk - llm - other_cost - run_month
        year = net_month * 12 + extra_year
        inputs = [v for k, v in d.items() if isinstance(v, dict) and "kind" in v]
        confirmed = sum(v["kind"] in ("source", "hh") for v in inputs) + 1
        out.append({"key": key, "title": d["title"], "hypothesis": d["hypothesis"], "fte_freed": round(fte, 1), "savings_month": round(savings),
                    "risk_avoided_month": round(risk), "llm_month": round(llm), "other_cost_month": round(other_cost), "run_month": round(run_month),
                    "net_month": round(net_month), "extra_revenue_year": round(extra_year), "effect_year": round(year), "build_cost": round(build_cost),
                    "roi_year": round((year - build_cost) / build_cost, 2), "payback_months": round(build_cost / net_month, 1) if net_month > 0 else None,
                    "evidence_share": round(confirmed / (len(inputs) + 1), 2), "role_cost_month": round(loaded(d["role"])), "details": details or {}})

    s = D["support"]
    bot = contacts * _v(s["chat_share"])
    auto = bot * _v(s["automation_rate"])
    defl = contacts * _v(s["outage_contact_share"]) * _v(s["proactive_deflection"])
    fte = (auto + defl) * aht / prod
    row("support", fte, fte * loaded(s["role"]), 0, bot * chat_cost, details={"contacts_month": round(contacts), "bot_dialogs": round(bot), "auto_closed": round(auto), "deflected_by_sentinel": round(defl)})

    o = D["onboarding"]
    conn = subs * _v(o["gross_adds_rate_month"])
    fte = conn * _v(o["automation_rate"]) * _v(o["manager_min_per_connection"]) / prod
    extra = conn * _v(o["conversion_uplift"])
    row("onboarding", fte, fte * loaded(o["role"]), extra * _v(c["arpu_rub_month"]) * _v(o["contribution_margin"]) * YEAR_RAMP,
        conn * _v(o["dialogs_per_connection"]) * chat_cost, details={"connections_month": round(conn), "extra_connections_month": round(extra, 1)})

    b = D["b2b"]
    leads = _v(b["leads_month"])
    fte = leads * _v(b["manager_min_saved_per_lead"]) / prod
    extra = leads * _v(b["conversion_uplift"])
    row("b2b", fte, fte * loaded(b["role"]), extra * _v(b["b2b_arpu_month"]) * _v(b["contribution_margin"]) * YEAR_RAMP, leads * chat_cost,
        details={"leads_month": leads, "extra_clients_month": round(extra, 1)})

    q = D["quality"]
    manual = contacts * _v(q["manual_review_share"]) * _v(q["manual_review_min"])
    fte_qa = manual * (1 - _v(q["manual_time_kept"])) / prod
    fte_rep = contacts * _v(q["repeat_contact_reduction"]) * aht / prod
    stt_cost = contacts * _v(q["phone_share"]) * _v(q["call_min_per_contact"]) * _v(A["stt"][stt])
    row("quality", fte_qa + fte_rep, fte_qa * loaded(q["role"]) + fte_rep * loaded(D["support"]["role"]), 0,
        contacts * dialog_cost(_v(q["qa_tokens_in"]), _v(q["qa_tokens_out"])), other_cost=stt_cost,
        details={"dialogs_checked_month": round(contacts), "coverage_before": f"{_v(q['manual_review_share']):.0%}", "coverage_after": "100%", "stt_month": round(stt_cost)})

    m = D["comms"]
    fte = _v(m["messages_month"]) * _v(m["copy_hours_saved_per_message"]) * 60 / prod
    row("comms", fte, fte * loaded(m["role"]), 0, _v(m["messages_month"]) * 3 * dialog_cost(6000, 1500),
        risk=_v(m["fine_rub"]) * _v(m["fine_prob_year"]) / 12, details={"messages_month": _v(m["messages_month"])})

    ranked = sorted(out, key=lambda r: r["effect_year"], reverse=True)
    return {"model": model, "stt": stt, "subscribers_est": round(subs), "contacts_month": round(contacts), "chat_dialog_cost_rub": round(chat_cost, 3),
            "build_cost": round(build_cost), "run_month": round(run_month), "directions": out, "ranking": [r["key"] for r in ranked],
            "best": ranked[0]["key"], "worst": ranked[-1]["key"], "llm_options": [k for k in L if not k.startswith("_") and isinstance(L[k], dict) and "in" in L[k]],
            "assumptions": A, "salaries": sal}


SCENARIOS = {
    "pessimistic": {"company.contacts_per_sub_month": 0.12, "directions.support.automation_rate": 0.2, "directions.support.proactive_deflection": 0.2,
                    "directions.onboarding.automation_rate": 0.2, "directions.onboarding.conversion_uplift": 0.02, "directions.b2b.conversion_uplift": 0.01,
                    "directions.quality.repeat_contact_reduction": 0.02, "llm.tokens_in_per_dialog": 40000, "build.dev_months": 5},
    "base": {},
    "optimistic": {"company.contacts_per_sub_month": 0.3, "directions.support.automation_rate": 0.45, "directions.support.proactive_deflection": 0.5,
                   "directions.onboarding.automation_rate": 0.5, "directions.onboarding.conversion_uplift": 0.08, "directions.b2b.conversion_uplift": 0.05,
                   "directions.quality.repeat_contact_reduction": 0.08},
}


def scenarios(model="claude-haiku-5-5", stt="local_whisper_rub_min", overrides=None):
    return {name: {d["key"]: d["effect_year"] for d in compute(model, stt, {**ov, **(overrides or {})})["directions"]} for name, ov in SCENARIOS.items()}


class In(BaseModel):
    model: str = "claude-haiku-5-5"
    stt: str = "local_whisper_rub_min"
    overrides: dict = {}


@router.get("")
def get(model: str = "claude-haiku-5-5", stt: str = "local_whisper_rub_min"):
    return compute(model, stt) | {"scenarios": scenarios(model, stt)}


@router.post("")
def post(x: In):
    return compute(x.model, x.stt, x.overrides) | {"scenarios": scenarios(x.model, x.stt, x.overrides)}


if __name__ == "__main__":
    for mdl in ("claude-haiku-5-5", "gigachat-lite", "gigachat-pro", "yandexgpt-pro"):
        r = compute(mdl)
        print(f"\n== {mdl}: абонентов ~{r['subscribers_est']:,}, обращений/мес ~{r['contacts_month']:,}, диалог {r['chat_dialog_cost_rub']} ₽, внедрение {r['build_cost']:,} ₽")
        for d in sorted(r["directions"], key=lambda d: -d["effect_year"]):
            print(f"{d['title']:28} FTE {d['fte_freed']:5} экономия {d['savings_month']:>10,} LLM {d['llm_month']:>8,} нетто/мес {d['net_month']:>10,} "
                  f"выручка/год {d['extra_revenue_year']:>11,} эффект/год {d['effect_year']:>12,} ROI {d['roi_year']:>6} окуп. {d['payback_months']} мес, подтв. {d['evidence_share']:.0%}")
    print(json.dumps(scenarios(), ensure_ascii=False, indent=1))

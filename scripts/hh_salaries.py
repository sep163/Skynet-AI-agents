"""Зарплаты по ролям в Санкт-Петербурге из открытых источников:
1) публичная выдача hh.ru (spb.hh.ru/search/vacancy, только с зарплатой) — API hh.ru без авторизации отдаёт 403;
2) открытый API «Работа России» (opendata.trudvsem.ru) — для сверки.
Запуск: python -m scripts.hh_salaries → data/finmodel/salaries.json"""
import html
import json
import re
import statistics
import time
from datetime import date

import httpx

ROLES = {
    "support_l1": "специалист технической поддержки интернет провайдер",
    "contact_center": "оператор контакт-центра",
    "installer": "монтажник интернет",
    "dispatcher": "диспетчер",
    "b2b_manager": "менеджер по работе с корпоративными клиентами",
    "sales_consultant": "менеджер по продажам интернет провайдер",
    "qa_specialist": "специалист по контролю качества колл-центра",
    "copywriter": "копирайтер",
    "noc_engineer": "сетевой инженер",
    "python_dev": "python разработчик",
    "ml_engineer": "ML инженер LLM",
}
UA = {"User-Agent": "Mozilla/5.0 (salary research)"}
NUM = r"(\d[\d ]{2,})"


def _n(x):
    return int(x.replace(" ", ""))


def parse_hh(page: str) -> list[float]:
    t = re.sub(r"[   ]", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    t = re.sub(r"\s+", " ", t)
    out = []
    for m in re.finditer(rf"(?:от {NUM} ?)?(?:(?:до|–|-) ?{NUM} ?)?₽ за месяц, (на руки|до вычета налогов)", t):
        lo, hi, kind = m.group(1), m.group(2), m.group(3)
        if not lo and not hi:
            continue
        v = (_n(lo) + _n(hi)) / 2 if lo and hi else _n(lo or hi)
        out.append(v / 0.87 if kind == "на руки" else v)
    return out


def hh(text: str) -> dict:
    vals = []
    for page in range(2):
        r = httpx.get("https://spb.hh.ru/search/vacancy", params={"text": text, "area": 2, "only_with_salary": "true", "items_on_page": 50, "page": page},
                      headers=UA, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            break
        got = parse_hh(r.text)
        vals += got
        if len(got) < 20:
            break
        time.sleep(1.5)
    return _stats(vals)


def trudvsem(text: str) -> dict:
    r = httpx.get("https://opendata.trudvsem.ru/api/v1/vacancies/region/7800000000000", params={"text": text, "limit": 100}, headers=UA, timeout=30)
    vs = (r.json().get("results") or {}).get("vacancies") or []
    vals = []
    for v in vs:
        v = v["vacancy"]
        lo, hi = v.get("salary_min") or 0, v.get("salary_max") or 0
        if lo or hi:
            vals.append((lo + hi) / 2 if lo and hi else lo or hi)
    return _stats(vals)


def _stats(vals):
    vals = [v for v in vals if 20000 < v < 600000]
    if len(vals) < 4:
        return {"n": len(vals), "median": round(statistics.median(vals)) if vals else None}
    q = statistics.quantiles(vals, n=4)
    return {"n": len(vals), "median": round(statistics.median(vals)), "p25": round(q[0]), "p75": round(q[2])}


if __name__ == "__main__":
    out = {"date": date.today().isoformat(), "area": "Санкт-Петербург", "unit": "₽/мес до вычета НДФЛ (gross)",
           "method": "середина вилки; «на руки» пересчитано в gross делением на 0.87; выбросы < 20 тыс. и > 600 тыс. отброшены",
           "sources": {"hh": "spb.hh.ru/search/vacancy?only_with_salary=true", "trudvsem": "opendata.trudvsem.ru/api/v1/vacancies/region/7800000000000"}, "roles": {}}
    for k, q in ROLES.items():
        out["roles"][k] = {"query": q, "hh": hh(q), "trudvsem": trudvsem(q)}
        print(k, out["roles"][k])
        time.sleep(1.5)
    open("data/finmodel/salaries.json", "w").write(json.dumps(out, ensure_ascii=False, indent=1))

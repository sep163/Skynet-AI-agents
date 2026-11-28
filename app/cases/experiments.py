"""A/B-тесты подходов менеджеров по типам клиентов.

Тестируем не скрипт целиком (его выполняют частично и по-своему), а один подход для одного типа клиента:
фразу или предложение в конкретной ситуации. Тест заводит и запускает только супервайзер или РОП:
менеджер по продажам сам скрипт не меняет. Но если сильный менеджер придумал фразу, которая поднимает его продажи,
система находит её по разборам его звонков, супервайзер проверяет её тестом, а победа засчитывается автору в рейтинг.

- Деление: каждый менеджер работает в обоих вариантах, блоками по 2–3 дня (A → B или B → A, порядок случайный,
  половина отдела начинает с A). Так сильные и слабые менеджеры не перекашивают результат.
- Сбор автоматический: каждый разобранный звонок попадает в каждый идущий тест, если совпали менеджер, даты
  и тип клиента. По разбору определяется, была ли ситуация, применена ли фраза, каков ближайший итог.
- Воронка: звонков этого типа → ситуация возникла → фраза применена → ближайший итог → итог в CRM.
- Решение по главной оценке (все, кому назначен вариант, с разбивкой по менеджеру и циклу A+B), рядом —
  эффект самой фразы (главная оценка / разница в применении) и «только где применили» (справочно, завышена).
  Защитная метрика: клиент стал враждебен или менеджер давил. Смотрим один раз при наборе объёма."""
import json
import math
from statistics import median
import random
import re
from datetime import date, datetime, timedelta
from statistics import NormalDist

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, emotion

router = APIRouter(prefix="/api/experiments", tags=["experiments"])
PATH = config.DATA / "runtime" / "experiments.json"
LIB = json.loads((config.DATA / "sales" / "approaches.json").read_text(encoding="utf-8"))
SEG = LIB["segments"]
ND = NormalDist()
TECH = (r"(не работает|пропал|пропада\w*|тормоз\w*|низк\w* скорост\w*|скорость (упала|низк)\w*|обрыв\w*|рв[её]тся|рвутся|падал\w*|падает|"
        r"нет интернета|нет связи|роутер\w* (не |глюч|сломал|завис)\w*|висит|лага\w*|мастер\w*[^.?!]{0,25}(не приш|никого|обещ)|обещаете мастера)")
DEMO_MANAGERS = ["Анна Котова", "Денис Мельников", "Ирина Соловьёва", "Максим Гусев", "Павел Орлов", "Светлана Белова", "Тимур Ахметов",
                 "Юлия Зайцева", "Олег Волков", "Мария Лебедева", "Артём Никитин", "Елена Фролова"]


def _load():
    d = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}
    for k in ("experiments", "events", "standards", "credits"):
        d.setdefault(k, [])
    return d


X = _load()


def _save():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(X, ensure_ascii=False, indent=1), encoding="utf-8")


def reset(data=None):
    global X
    X = data or {"experiments": [], "events": [], "standards": [], "credits": []}


# ---------- статистика ----------

def need_per_arm(p1: float, p2: float, alpha=0.05, power=0.8) -> int:
    p2 = min(max(p2, 0.001), 0.999)
    if abs(p2 - p1) < 1e-9:
        return 10 ** 9
    za, zb = ND.inv_cdf(1 - alpha / 2), ND.inv_cdf(power)
    pb = (p1 + p2) / 2
    return math.ceil((za * math.sqrt(2 * pb * (1 - pb)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p1 - p2) ** 2)


def stratified(strata: list) -> dict:
    """Разница долей B − A по Мантелю — Хензелю и тест Кохрана — Мантеля — Хензеля.
    strata: [(успехов B, n B, успехов A, n A)]; страты с одним вариантом не участвуют."""
    s = [(b, nb, a, na) for b, nb, a, na in strata if nb and na]
    if not s:
        return {"diff": None, "p": None, "strata": 0}
    num = sum((b * na - a * nb) / (nb + na) for b, nb, a, na in s)
    den = sum(nb * na / (nb + na) for b, nb, a, na in s)
    o = e = v = 0.0
    for b, nb, a, na in s:
        n, m = nb + na, b + a
        o += b
        e += nb * m / n
        v += nb * na * m * (n - m) / (n * n * (n - 1)) if n > 1 else 0
    chi = (abs(o - e) - 0.5) ** 2 / v if v > 0 else 0.0
    # дисперсия взвешенной разницы: веса страт w = nB·nA / (nB + nA)
    var = sum((nb * na / (nb + na)) ** 2 * ((b / nb) * (1 - b / nb) / nb + (a / na) * (1 - a / na) / na) for b, nb, a, na in s) / den ** 2
    se = math.sqrt(max(var, 1e-12))
    d = num / den
    return {"diff": round(d, 4), "ci": [round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)], "p": round(math.erfc(math.sqrt(chi / 2)), 4), "strata": len(s)}


# ---------- разбор звонка → тип клиента, ситуация, фраза, итог ----------

def segment(call: dict, r: dict | None, script: dict | None = None) -> str:
    client = " ".join(t for ro, t in call["dialog"] if ro == "client").lower()
    sc = call.get("script", "")
    direction = ((script or {}).get("scripts", {}).get(sc) or {}).get("direction", "B2C")
    if sc == "winback":
        return "winback"
    if re.search(emotion.CHURN, client):
        return "leaving"
    if direction in ("B2B", "B2G"):
        return "business"
    if re.search(TECH, client):
        return "tech_loyal"
    if re.search(r"дорог\w*|дешевле", client) or any(o.get("type") == "price" for o in (r or {}).get("objections", [])):
        return "price"
    return "new"


def _stems(text: str) -> list:
    return [w[:5] for w in re.findall(r"[а-яёa-z0-9]+", text.lower()) if len(w) >= 4]


def phrase_used(call: dict, phrase: str, keywords: list | None = None) -> dict:
    """Применена ли фраза: в одной реплике менеджера есть ≥ 60% значимых слов фразы (по основам) или все ключевые слова."""
    want = keywords and [k[:5].lower() for k in keywords] or sorted(set(_stems(phrase)))
    if not want:
        return {"used": False, "quote": None, "coverage": 0}
    best, quote = 0.0, None
    for ro, t in call["dialog"]:
        if ro != "agent":
            continue
        have = set(_stems(t))
        cov = sum(w in have for w in want) / len(want)
        if cov > best:
            best, quote = cov, t
    used = best >= (1.0 if keywords else 0.6)
    return {"used": used, "quote": quote if used else None, "coverage": round(best, 2)}


def situation(call: dict, r: dict | None, trigger: str) -> bool:
    if trigger in ("any", "", None):
        return True
    return any(o.get("type") == trigger for o in (r or {}).get("objections", []))


def success(call: dict, r: dict | None, metric: str) -> bool:
    close = (r or {}).get("close_type")
    if metric == "deal":
        return call.get("outcome") == "сделка" or close == "deal"
    if metric == "stayed":
        return call.get("outcome") != "отказ" and close not in ("refused", "no_next_step")
    return close in ("deal", "meeting", "callback_dated") if close else call.get("outcome") in ("сделка", "встреча")


def guardrail(r: dict | None) -> bool:
    r = r or {}
    return (r.get("client_hostility") or {}).get("final", 0) >= 2 or (r.get("style") or {}).get("pressure", 0) >= 2


# ---------- расписание ----------

def _d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s


def order(exp: dict) -> dict:
    """Половина менеджеров начинает с A, половина с B; кто именно — случайно, но воспроизводимо."""
    ms = list(exp["managers"])
    random.Random(exp["id"]).shuffle(ms)
    return {m: ("A", "B") if i % 2 == 0 else ("B", "A") for i, m in enumerate(ms)}


def arm_of(exp: dict, manager: str, day) -> dict | None:
    if manager not in exp["managers"] or not exp.get("start"):
        return None
    i = (_d(day) - _d(exp["start"])).days
    if i < 0 or i >= exp["days"]:
        return None
    block = i // exp["block_days"]
    return {"arm": order(exp)[manager][block % 2], "block": block, "cycle": block // 2}


def schedule(exp: dict) -> dict:
    if not exp.get("start"):
        return {"days": [], "rows": []}
    days = [(_d(exp["start"]) + timedelta(i)).isoformat() for i in range(exp["days"])]
    return {"days": days, "rows": [{"manager": m, "arms": [arm_of(exp, m, d)["arm"] for d in days]} for m in exp["managers"]]}


# ---------- сбор ----------

def ingest_call(call: dict, r: dict | None, script: dict | None = None, source: str = "разбор звонка") -> list:
    """Вызывается после каждого разбора: звонок сам попадает во все подходящие идущие тесты."""
    out = []
    for exp in X["experiments"]:
        if exp["status"] != "running":
            continue
        a = arm_of(exp, call["manager"], call.get("date") or date.today().isoformat())
        seg = segment(call, r, script)
        if not a or seg not in exp["segments"]:
            continue
        b = phrase_used(call, exp["variants"]["B"]["phrase"], exp["variants"]["B"].get("keywords"))
        sit = situation(call, r, exp["trigger"])
        ev = {"exp": exp["id"], "call_id": call["id"], "manager": call["manager"], "date": call.get("date"), **a, "segment": seg,
              "situation": sit, "applied": b["used"], "quote": b["quote"], "success": success(call, r, exp["metric"]),
              "guardrail": guardrail(r), "crm": None, "source": source}
        X["events"] = [e for e in X["events"] if not (e["exp"] == exp["id"] and e["call_id"] == call["id"])]
        X["events"].append(ev)
        out.append(ev)
    if out:
        _save()
    return out


def simulate(exp: dict, calls_per_day: int = 100, seed: int = 1, true_effect: float | None = None) -> int:
    """Демо: неделя звонков отдела, сгенерированная прямо на уровне событий (без расшифровок).
    У менеджеров разный базовый уровень и разное применение фразы; эффект фразы задан и показан в карточке."""
    rnd = random.Random(f"{exp['id']}-{seed}")
    eff = exp.get("sim", {}).get("true_effect", 0.10) if true_effect is None else true_effect
    base = exp["baseline"]
    seg_share = sum(SEG[s]["share"] for s in exp["segments"])
    sit_share = 1.0 if exp["trigger"] in ("any", "") else 0.6
    X["events"] = [e for e in X["events"] if e["exp"] != exp["id"]]
    k = 0
    for m in exp["managers"]:
        skill = rnd.uniform(-0.12, 0.12)
        adh = rnd.uniform(0.45, 0.95)
        for i in range(exp["days"]):
            day = (_d(exp["start"]) + timedelta(i)).isoformat()
            a = arm_of(exp, m, day)
            for _ in range(calls_per_day):
                if rnd.random() > seg_share:
                    continue
                k += 1
                sit = rnd.random() < sit_share
                applied = sit and rnd.random() < (adh if a["arm"] == "B" else 0.04)
                p = min(0.95, max(0.02, base + skill + (eff if applied else 0) + rnd.uniform(-0.03, 0.03) * (i % 3 - 1)))
                ok = rnd.random() < p
                X["events"].append({"exp": exp["id"], "call_id": f"SIM-{exp['id']}-{k:05d}", "manager": m, "date": day, **a,
                                    "segment": exp["segments"][0], "situation": sit, "applied": applied, "quote": None, "success": ok,
                                    "guardrail": rnd.random() < 0.04, "crm": (rnd.random() < (0.85 if ok else 0.1)) if ok or rnd.random() < 0.5 else None,
                                    "source": "симуляция"})
    exp["sim"] = {"true_effect": eff, "calls_per_day": calls_per_day, "note": "демо-данные: эффект фразы задан в симуляции"}
    _save()
    return k


# ---------- анализ ----------

def analyze(exp: dict) -> dict:
    ev = [e for e in X["events"] if e["exp"] == exp["id"]]
    arms = {}
    for arm in ("A", "B"):
        xs = [e for e in ev if e["arm"] == arm]
        sit = [e for e in xs if e["situation"]]
        crm = [e for e in sit if e["crm"] is not None]
        arms[arm] = {"calls": len(xs), "situation": len(sit), "applied": sum(e["applied"] for e in sit), "success": sum(e["success"] for e in sit),
                     "guardrail": sum(e["guardrail"] for e in sit), "crm_known": len(crm), "crm_success": sum(e["crm"] for e in crm)}
        a = arms[arm]
        a["rates"] = {k: round(a[k] / a["situation"], 3) if a["situation"] else None for k in ("applied", "success", "guardrail")}
        a["rates"]["crm"] = round(a["crm_success"] / a["crm_known"], 3) if a["crm_known"] else None
    strata = {}
    for e in ev:
        if not e["situation"]:
            continue
        s = strata.setdefault((e["manager"], e["cycle"]), {"A": [0, 0], "B": [0, 0]})
        s[e["arm"]][0] += e["success"]
        s[e["arm"]][1] += 1
    main = stratified([(v["B"][0], v["B"][1], v["A"][0], v["A"][1]) for v in strata.values()])
    gstrata = {}
    for e in ev:
        if e["situation"]:
            s = gstrata.setdefault((e["manager"], e["cycle"]), {"A": [0, 0], "B": [0, 0]})
            s[e["arm"]][0] += e["guardrail"]
            s[e["arm"]][1] += 1
    guard = stratified([(v["B"][0], v["B"][1], v["A"][0], v["A"][1]) for v in gstrata.values()])
    rA, rB = arms["A"]["rates"], arms["B"]["rates"]
    gap = (rB["applied"] or 0) - (rA["applied"] or 0)
    cace = round(main["diff"] / gap, 3) if main["diff"] is not None and gap >= 0.15 else None
    appl = [e for e in ev if e["situation"] and e["arm"] == "B" and e["applied"]]
    notapp = [e for e in ev if e["situation"] and e["arm"] == "A" and not e["applied"]]
    pp = round(sum(e["success"] for e in appl) / len(appl) - sum(e["success"] for e in notapp) / len(notapp), 3) if appl and notapp else None
    need = need_per_arm(exp["baseline"], exp["baseline"] + exp["min_effect"] * exp["expected_adherence"])
    elapsed = len({e["date"] for e in ev}) or 1
    per_day = min(arms["A"]["situation"], arms["B"]["situation"]) / elapsed
    have = min(arms["A"]["situation"], arms["B"]["situation"])
    cycles_done = min((sum(1 for (m, c), v in strata.items() if m == mm and v["A"][1] and v["B"][1]) for mm in exp["managers"]), default=0)
    warn = []
    if arms["B"]["situation"] and gap < 0.3:
        warn.append(f"фразу в варианте B применяют всего на {round(gap * 100)} п.п. чаще, чем в A: тест почти не проверяет фразу — сначала обучение (тренажёр)")
    if cycles_done < 1:
        warn.append("не у всех менеджеров прошёл полный цикл A + B")
    if have < need:
        days_left = math.ceil((need - have) / per_day) if per_day else None
        status, verdict = "collecting", f"идёт сбор: {have} из {need} ситуаций на вариант" + (f", ещё ~{days_left} дн. при текущем потоке" if days_left else "")
    elif main["p"] is not None and main["p"] < 0.05 and main["diff"] > 0 and not (guard["p"] is not None and guard["p"] < 0.05 and guard["diff"] > 0):
        status, verdict = "b_wins", "B лучше: фраза повышает ближайший итог, защитная метрика не хуже"
    elif main["p"] is not None and main["p"] < 0.05 and main["diff"] < 0:
        status, verdict = "a_wins", "A лучше: фраза вредит, оставить как было"
    elif guard["p"] is not None and guard["p"] < 0.05 and guard["diff"] > 0:
        status, verdict = "guardrail", "B поднимает итог, но клиенты чаще раздражаются или менеджер давит — не внедрять"
    else:
        status, verdict = "no_diff", "разницы не видно: эффект меньше заложенного минимума"
    per_mgr = []
    for m in exp["managers"]:
        row = {"manager": m}
        for arm in ("A", "B"):
            xs = [e for e in ev if e["manager"] == m and e["arm"] == arm and e["situation"]]
            row[arm] = {"n": len(xs), "success": round(sum(e["success"] for e in xs) / len(xs), 3) if xs else None,
                        "applied": round(sum(e["applied"] for e in xs) / len(xs), 3) if xs else None}
        per_mgr.append(row)
    return {"arms": arms, "itt": main, "guardrail_test": guard, "applied_gap": round(gap, 3), "phrase_effect": cace, "per_protocol": pp,
            "need_per_arm": need, "have_per_arm": have, "per_day_per_arm": round(per_day, 1), "status": status, "verdict": verdict, "warnings": warn,
            "managers": per_mgr, "sources": sorted({e["source"] for e in ev}),
            "examples": [{"call_id": e["call_id"], "manager": e["manager"], "quote": e["quote"], "success": e["success"]} for e in ev if e["quote"]][:6]}


def plan(managers: int = 12, calls_per_day: int = 100, min_effect: float = 0.10, adherence: float = 0.7, block_days: int = 2) -> list:
    """Сколько дней нужно тесту по каждому типу клиента при таком потоке звонков."""
    out = []
    for k, s in SEG.items():
        per_day_arm = managers * calls_per_day * s["share"] / 2
        need = need_per_arm(s["baseline"], s["baseline"] + min_effect * adherence)
        days = math.ceil(need / per_day_arm)
        days = max(days, 2 * block_days)
        days = math.ceil(days / (2 * block_days)) * 2 * block_days
        out.append({"segment": k, "name": s["name"], "share": s["share"], "per_day": round(per_day_arm * 2), "baseline": s["baseline"],
                    "need_per_arm": need, "days": days, "fits_week": days <= 7})
    return out


# ---------- жизненный цикл ----------

class ExpIn(BaseModel):
    title: str
    segments: list[str]
    trigger: str = "any"
    a_text: str = "как обычно"
    b_phrase: str
    b_keywords: list[str] | None = None
    metric: str | None = None
    min_effect: float = 0.10
    expected_adherence: float = 0.7
    block_days: int = 2
    days: int = 6
    managers: list[str] | None = None
    author: str
    role: str = "supervisor"
    hypothesis: str = ""
    origin_manager: str | None = None
    origin_calls: list[str] | None = None


ROLES = {"supervisor": "супервайзер", "rop": "РОП"}


def _role(role):
    if role not in ROLES:
        raise HTTPException(403, "заводят и запускают тесты супервайзер или РОП; фраза менеджера попадает в тест через «Находки менеджеров»")


def create(x: ExpIn) -> dict:
    _role(x.role)
    bad = [s for s in x.segments if s not in SEG]
    if bad or not x.segments:
        raise HTTPException(400, f"тип клиента: {list(SEG)}")
    if x.block_days not in (1, 2, 3) or x.days < 2 * x.block_days:
        raise HTTPException(400, "блок 1–3 дня, тест не короче одного цикла A + B")
    if len(_stems(x.b_phrase)) < 2 and not x.b_keywords:
        raise HTTPException(400, "фраза слишком короткая, чтобы найти её в звонке: добавьте ключевые слова")
    from . import sales
    seg0 = SEG[x.segments[0]]
    managers = x.managers or sorted({c["manager"] for c in sales.S.calls})
    origin = None
    if x.origin_manager:
        el = eligibility(x.origin_manager)
        if not el["eligible"]:
            raise HTTPException(409, f"{x.origin_manager}: {el['why']}")
        # автор и так говорит свою фразу в обоих вариантах — в тесте он бы размыл разницу, поэтому участвуют остальные
        managers = [m for m in managers if m != x.origin_manager]
        origin = {"manager": x.origin_manager, "calls": x.origin_calls or [], "eligibility": el}
    e = {"id": f"AB-{len(X['experiments']) + 1:03d}", "title": x.title, "hypothesis": x.hypothesis, "segments": x.segments, "trigger": x.trigger,
         "variants": {"A": {"text": x.a_text}, "B": {"phrase": x.b_phrase, "keywords": x.b_keywords}},
         "metric": x.metric or seg0["metric"], "baseline": seg0["baseline"], "min_effect": x.min_effect, "expected_adherence": x.expected_adherence,
         "block_days": x.block_days, "days": x.days, "managers": managers, "origin": origin,
         "author": x.author, "role": x.role, "created": datetime.now().isoformat(timespec="minutes"), "status": "draft", "start": None}
    X["experiments"].append(e)
    _save()
    return e


def start(eid: str, day: str | None = None, author: str = "РОП", role: str = "rop") -> dict:
    _role(role)
    e = _get(eid)
    if e["status"] != "draft":
        raise HTTPException(409, "тест уже запущен или завершён")
    e.update(status="running", start=day or date.today().isoformat(), started_by=author)
    _save()
    return e


def finish(eid: str, author: str = "РОП", role: str = "rop") -> dict:
    _role(role)
    e = _get(eid)
    r = analyze(e)
    e.update(status="finished", result={"status": r["status"], "verdict": r["verdict"], "itt": r["itt"], "phrase_effect": r["phrase_effect"]},
             finished_by=author, finished=datetime.now().isoformat(timespec="minutes"))
    if r["status"] == "b_wins":
        for s in e["segments"]:
            X["standards"].append({"segment": s, "phrase": e["variants"]["B"]["phrase"], "exp": e["id"], "effect": r["itt"]["diff"], "at": e["finished"],
                                   "author": (e.get("origin") or {}).get("manager")})
        if e.get("origin"):
            X["credits"].append({"manager": e["origin"]["manager"], "exp": e["id"], "phrase": e["variants"]["B"]["phrase"], "segments": e["segments"],
                                 "effect": r["itt"]["diff"], "phrase_effect": r["phrase_effect"], "managers_tested": len(e["managers"]), "at": e["finished"],
                                 "text": f"фраза стала стандартом для «{', '.join(SEG[s]['name'] for s in e['segments'])}»: {round(r['itt']['diff'] * 100, 1)} п.п. к итогу у других менеджеров"})
    _save()
    return e


def _get(eid):
    e = next((x for x in X["experiments"] if x["id"] == eid), None)
    if not e:
        raise HTTPException(404, "нет такого теста")
    return e


def eligibility(manager: str) -> dict:
    """Чью фразу проверяем: у менеджера итог выше медианы отдела и достаточно разобранных звонков."""
    from . import sales
    profs = [p for p in sales.team() if p["calls"]]
    me = next((p for p in profs if p["manager"] == manager), None)
    if not me:
        return {"eligible": False, "why": "нет разобранных звонков"}
    med = median(p["conversion"] for p in profs)
    ok = me["calls"] >= 3 and me["conversion"] >= med
    why = (f"конверсия {round(me['conversion'] * 100)}% при медиане отдела {round(med * 100)}%, разобрано звонков: {me['calls']}"
           + ("" if ok else " — нужно не ниже медианы и от 3 звонков"))
    return {"eligible": ok, "why": why, "conversion": me["conversion"], "median": med, "calls": me["calls"]}


STAGE_NAMES = {"needs": "выявление потребностей", "offer": "предложение", "objections": "ответ на возражение", "closing": "закрытие на следующий шаг"}


def innovations(min_words: int = 5, per_manager: int = 3) -> list:
    """Находки менеджеров: фразы, которые разбор отметил как сильные (этап на 2 из 2 или отработанное возражение)
    в успешных звонках менеджера и которых нет у других менеджеров. Это кандидаты: что фраза работает,
    а не просто совпала с удачным звонком, покажет только A/B-тест на других менеджерах."""
    from . import sales
    calls = [c for c in sales.S.calls if c["id"] in sales.S.results]
    out = []
    for m in sorted({c["manager"] for c in calls}):
        el = eligibility(m)
        mine = [c for c in calls if c["manager"] == m]
        others = [c for c in calls if c["manager"] != m]
        found = []
        for c in mine:
            r = sales.S.results[c["id"]]
            if not success(c, r, "next_step") or any(st["id"] == "compliance" and st["score"] == 0 for st in r["stages"]):
                continue  # фраза с неверной ценой или запрещённым обещанием в стандарт не идёт
            agent = [t for ro, t in c["dialog"] if ro == "agent"]
            strong = [(st["evidence"], STAGE_NAMES[st["id"]]) for st in r["stages"] if st["id"] in STAGE_NAMES and st["score"] == 2 and st.get("evidence")]
            for o in r.get("objections", []):
                if o.get("handled"):
                    i = next((k for k, (ro, t) in enumerate(c["dialog"]) if ro == "client" and o["quote"][:20].lower() in t.lower()), None)
                    nxt = next((t for ro, t in c["dialog"][i + 1:] if ro == "agent"), None) if i is not None else None
                    if nxt:
                        strong.append((nxt, f"ответ на «{o['quote'][:40]}»"))
            for ev, why in strong:
                t = next((a for a in agent if phrase_used({"dialog": [["agent", a]]}, ev)["used"] or ev[:30] in a), None)
                if not t or len(set(_stems(t))) < min_words or any(phrase_used({"dialog": [["agent", f["phrase"]]]}, t)["used"] for f in found):
                    continue
                if any(phrase_used(o, t)["used"] for o in others):
                    continue
                uses = [x for x in mine if phrase_used(x, t)["used"]]
                seg = segment(c, r, sales.S.script)
                found.append({"manager": m, "phrase": t, "where": why, "segment": seg, "segment_name": SEG[seg]["name"], "calls": [x["id"] for x in uses],
                              "wins": sum(success(x, sales.S.results[x["id"]], "next_step") for x in uses), "eligible": el["eligible"], "why": el["why"],
                              "in_tests": any((e.get("origin") or {}).get("manager") == m and e["variants"]["B"]["phrase"] == t for e in X["experiments"])})
        out += found[:per_manager]
    return sorted(out, key=lambda x: (not x["eligible"], -x["wins"]))


def credits(manager: str | None = None) -> list:
    return [c for c in X["credits"] if not manager or c["manager"] == manager]


def today(manager: str, day: str | None = None) -> list:
    """Что менеджеру делать сегодня: по каким клиентам какой вариант."""
    day = day or date.today().isoformat()
    out = []
    for e in X["experiments"]:
        a = e["status"] == "running" and arm_of(e, manager, day)
        if a:
            out.append({"exp": e["id"], "title": e["title"], "segments": [SEG[s]["name"] for s in e["segments"]], "arm": a["arm"],
                        "do": f"используй фразу: «{e['variants']['B']['phrase']}»" if a["arm"] == "B" else f"работай как обычно ({e['variants']['A']['text']}), фразу из теста не используй"})
    return out


def seed_demo():
    """На чистой копии: два демо-теста на неделю отдела из 12 менеджеров по 100 звонков."""
    if X["experiments"]:
        return
    for title, seg, phrase, eff, hyp, mde in (
            ("Точный срок и компенсация для клиентов с проблемами связи", "tech_loyal",
             "Я вижу вашу линию: проблема на нашей стороне, мастер будет завтра с 10 до 12, а за дни без связи я начислю компенсацию", 0.12,
             "Клиент, который не хочет уходить, соглашается на решение, если слышит точный срок и видит, что его время ценят", 0.12),
            ("Годовая оплата вместо ухода", "leaving", "Давайте посчитаем годовую оплату: выйдет на 20% дешевле, а тариф останется тот же", 0.08,
             "Часть уходящих уходит из-за цены: годовая оплата снимает причину без смены тарифа", 0.10)):
        e = create(ExpIn(title=title, segments=[seg], b_phrase=phrase, author="Ольга Петрова", role="supervisor", hypothesis=hyp, days=6, block_days=2, min_effect=mde,
                         managers=DEMO_MANAGERS))
        start(e["id"], "2026-11-23", "Азамат")
        simulate(e, 100, true_effect=eff)


def view(e):
    return e | {"segment_names": [SEG[s]["name"] for s in e["segments"]], "metric_name": LIB["metrics"].get(e["metric"], e["metric"])}


class StartIn(BaseModel):
    day: str | None = None
    author: str = "РОП"
    role: str = "rop"


class CrmIn(BaseModel):
    call_id: str
    success: bool


@router.get("/library")
def get_library():
    return {"segments": SEG, "metrics": LIB["metrics"], "standards": X["standards"]}


@router.get("/plan")
def get_plan(managers: int = 12, calls_per_day: int = 100, min_effect: float = 0.10, adherence: float = 0.7, block_days: int = 2):
    return plan(managers, calls_per_day, min_effect, adherence, block_days)


@router.get("")
def list_exps():
    return [view(e) | {"summary": {k: v for k, v in analyze(e).items() if k in ("status", "verdict", "itt", "have_per_arm", "need_per_arm")}}
            for e in X["experiments"][::-1]]


@router.post("")
def post_exp(x: ExpIn):
    return create(x)


@router.get("/innovations")
def get_innovations():
    return innovations()


@router.get("/credits")
def get_credits(manager: str | None = None):
    return credits(manager)


@router.get("/today")
def get_today(manager: str, day: str | None = None):
    return today(manager, day)


@router.get("/{eid}")
def get_exp(eid: str):
    e = _get(eid)
    return view(e) | {"analysis": analyze(e), "schedule": schedule(e)}


@router.post("/{eid}/start")
def post_start(eid: str, x: StartIn):
    return start(eid, x.day, x.author, x.role)


@router.post("/{eid}/finish")
def post_finish(eid: str, x: StartIn):
    return finish(eid, x.author, x.role)


@router.post("/{eid}/simulate")
def post_simulate(eid: str, calls_per_day: int = 100, effect: float = 0.10):
    e = _get(eid)
    if not e.get("start"):
        raise HTTPException(409, "сначала запустите тест")
    return {"events": simulate(e, calls_per_day, true_effect=effect)}


@router.post("/crm")
def post_crm(x: CrmIn):
    """Итог из CRM (подключение, клиент остался) дописывается к уже собранному событию."""
    n = 0
    for e in X["events"]:
        if e["call_id"] == x.call_id:
            e["crm"] = x.success
            n += 1
    _save()
    return {"updated": n}

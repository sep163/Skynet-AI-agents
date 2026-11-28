"""Оценка состояния клиента по независимым осям вместо одной шкалы «агрессии».

- manner — манера речи (нейтральная / простая-резкая / формальная). Сама по себе ни на что не влияет:
  мат как связка слов, просторечие, капс — это стиль, а не агрессия.
- dissatisfaction 0–3 — недовольство ситуацией («опять не работает», «третий раз»).
- hostility 0–3 — враждебность К ЛЮДЯМ, с адресатом: situation / company / employee.
  «Бля, опять ничего не работает» → 0. Тихое «вы там головой думаете?» → 2. Оскорбление или угроза оператору → 3.
- arousal — возбуждение по голосу (громкость, темп, разброс высоты тона) ОТНОСИТЕЛЬНО обычной манеры этого клиента.
  Возбуждение не повышает враждебность: оно только уточняет картину («громко, но не враждебно» / «тихо, но враждебно»).
Это аналитика для контроля качества (РОП): она никого не подключает и не меняет маршрут обращения.
Система размечает манеру речи, а не человека: никаких выводов о происхождении, образовании и т. п.
"""
import re

from . import config, llm, profiles

MAT = r"(бля\w*|ху[йеёи]\w*|пизд\w*|[её]б[аылунт]\w*|[её]б\b|сук[аи]\b|сучк\w*|муда\w*|мудил\w*|пид[оа]р\w*|гандон\w*|залуп\w*|оху[ейё]\w*|нахуй|нахер|похуй|хер\w*|хрен\w*|нихрена)"
ROUGH = r"(блин|фигн\w*|хрен\w*|капец|жесть|ч[её]\b|щас|ваще|нифига|задолбал\w*|достал\w*|фиг\b|офигел\w*|обалдел\w*|ёлки|чёрт|черт\w*)"
INSULT = r"(туп(ой|ая|ые|ица|ицы|ые)|идиот\w*|дебил\w*|дур(ак|а|ы|ра)\b|дурак\w*|кретин\w*|урод\w*|баран\w*|овц[аы]\b|бестолоч\w*|некомпетент\w*|клоун\w*|бездар\w*|придур\w*|лох\w*|мраз\w*|скотин\w*|сволоч\w*|твар[ьи]\b|дармоед\w*|бараны)"
PERSON = r"(ты|тебе|тебя|тобой|девушка|молодой человек|оператор\w*|сотрудник\w*|менеджер\w*|специалист\w*|консультант\w*|бот\w*)"
YOU_PL = r"(вы|вас|вам|вами|ваш\w*|у вас|вы там|вы все)"
COMPANY = r"(компани\w*|скайнет\w*|skynet|провайдер\w*|контор\w*|фирм\w*|шарашк\w*)"
CONTEMPT = r"(головой (вообще )?(думаете|думаешь)|мозг(ов|и)? (нет|есть|включи)|читать (вы )?умеете|вы (вообще )?нормальн|где (вас|тебя) (таких )?(набирают|берут|нашли)|кто вас (учил|набирал)|руки из жопы|рукожоп\w*|понабрали|вы (вообще )?(понимаете|слышите|слушаете),? что (вам|я)|до (вас|тебя) (не )?доходит|(значит|видимо) (вы |ты )?плохо \w+|сами виноваты|не умеете пользоваться|для особо одар\w*)"
THREAT = r"(найду (тебя|вас)|приеду (и )?разбер|я (тебе|вам) (устрою|покажу)|пожалеешь|пожалеете|уволю|уволят тебя|морду|урою)"
GO_AWAY = r"(ид(и|ите) (ты |вы )?(на|в)\s+\S+|пош(ёл|ел|ла|ли)\s+(ты|вы|на)|заткн(ись|итесь)|отвали(те)?)"
CHURN = r"(растор\w*|перейд[уём]\w*|уйду|ухожу|отключ[иа]т[ье]с\w*|отказ[ыа]ва\w*|в суд|суд\b|роспотреб\w*|прокурат\w*|верните деньги|жалоб[ау] (на вас|напишу))"
UNHAPPY = r"(нет (связи|интернета)|опять|снова|постоянно|третий раз|второй раз|сколько можно|невозможно|не работает|когда уже|до сих пор|который день|целый день|неделю)"
STRONG = r"(сколько можно|(втор|трет|четв[её]рт|пят)\w+ (раз|день|дня|сутки|неделю)|\d+ (дн|сут|недел)\w*|который день|неделю|невозможно|до сих пор|когда уже)"
FORMAL = r"(уважаем\w*|будьте добры|не могли бы|пожалуйста|благодарю|заранее спасибо|прошу)"
MANNER = {"neutral": "нейтральная", "rough": "простая, резкая", "formal": "формальная"}
TARGET = {"none": "нет", "situation": "ситуация", "company": "компания", "employee": "сотрудник"}


def _f(p, t):
    return re.findall(p, t, re.I)


def _near(a, b, t, window=4):
    """Есть ли слово из a в пределах window слов от слова из b."""
    w = re.findall(r"[\w-]+", t.lower())
    ia = [i for i, x in enumerate(w) if re.fullmatch(a, x, re.I)]
    ib = [i for i, x in enumerate(w) if re.fullmatch(b, x, re.I)]
    return any(abs(i - j) <= window for i in ia for j in ib)


def rules(text: str) -> dict:
    t = text.strip()
    low = t.lower()
    letters = [c for c in t if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / max(len(letters), 1)
    bangs = t.count("!")
    mat, rough, insult = _f(MAT, low), _f(ROUGH, low), _f(INSULT, low)
    churn = bool(re.search(CHURN, low))
    hostility, target, why = 0, "none", []
    if re.search(THREAT, low) or re.search(GO_AWAY, low):
        hostility, target = 3, "employee"
        why.append("угроза или посыл в адрес собеседника")
    if insult and (_near(INSULT, PERSON, low) or _near(INSULT, YOU_PL, low, 3)):
        h, tg = (3, "employee") if _near(INSULT, PERSON, low) else (2, "company")
        if h > hostility:
            hostility, target = h, tg
        why.append("оскорбление с адресатом")
    elif insult and re.search(COMPANY, low):
        hostility, target = max(hostility, 2), "company" if hostility < 2 else target
        why.append("оскорбление в адрес компании")
    if re.search(CONTEMPT, low):
        if hostility < 2:
            hostility, target = 2, "employee"
        why.append("пренебрежение к собеседнику, даже без мата")
    if mat and hostility < 2:
        if _near(MAT, PERSON, low, 2):
            hostility, target = 3, "employee"
            why.append("мат в адрес сотрудника")
        elif _near(MAT, YOU_PL, low, 2):
            hostility, target = 2, "company"
            why.append("мат в адрес компании")
        else:
            target = "situation" if target == "none" else target
            why.append("мат как связка слов, не в чей-то адрес")
    unhappy = len(_f(UNHAPPY, low))
    diss = 3 if churn else 2 if re.search(STRONG, low) or (unhappy and (bangs >= 2 or caps > 0.5)) else 1 if unhappy or mat or rough or bangs else 0
    manner = "rough" if mat or rough or (caps > 0.5 and len(letters) > 10) or bangs >= 3 else "formal" if re.search(FORMAL, low) else "neutral"
    return {"manner": manner, "dissatisfaction": diss, "hostility": hostility, "target": target, "churn_risk": churn, "why": why,
            "text_arousal": "high" if caps > 0.5 and len(letters) > 10 or bangs >= 3 else "normal",
            "signals": {"mat": len(mat), "rough": len(rough), "insult": len(insult), "caps": round(caps, 2), "bangs": bangs}}


SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "manner": {"type": "string", "enum": list(MANNER)}, "dissatisfaction": {"type": "integer", "enum": [0, 1, 2, 3]},
    "hostility": {"type": "integer", "enum": [0, 1, 2, 3]}, "target": {"type": "string", "enum": list(TARGET)},
    "churn_risk": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["manner", "dissatisfaction", "hostility", "target", "churn_risk", "reason"]}
SYSTEM = """Оцени реплику клиента интернет-провайдера по НЕЗАВИСИМЫМ осям.
manner — манера речи: rough (мат как связка, просторечие, резкие короткие фразы), formal или neutral. Манера — это стиль, не агрессия.
dissatisfaction 0–3 — насколько клиент недоволен СИТУАЦИЕЙ.
hostility 0–3 — враждебность К ЛЮДЯМ: 0 — нет (даже если есть мат про ситуацию); 1 — колкость; 2 — пренебрежение или оскорбление компании/сотрудника; 3 — прямое оскорбление или угроза сотруднику.
target — на кого направлена враждебность: none, situation, company, employee.
Пример: «бля, опять нихрена не работает, когда почините» — manner rough, dissatisfaction 2, hostility 0, target situation.
Пример: «девушка, вы вообще головой думаете?» — manner neutral, hostility 2, target employee.
Не делай выводов о происхождении или образовании человека. reason — одна короткая фраза по-русски."""


def arousal(voice: dict | None, client_id: str | None, text_level: str) -> dict:
    """Возбуждение по голосу относительно нормы этого клиента; без нормы — по общим порогам, с пометкой."""
    if not voice:
        return {"level": text_level, "source": "текст (капс, восклицания)" if text_level == "high" else "нет голоса"}
    base = profiles.baseline(client_id) if client_id else None
    feats = {k: voice.get(k) for k in ("loudness_db", "words_per_sec", "pitch_std_st") if voice.get(k) is not None}
    if base and base["n"] >= 2:
        z = {k: (v - base[k]["mean"]) / max(base[k]["std"], {"loudness_db": 2.0, "words_per_sec": 0.3, "pitch_std_st": 0.8}[k])
             for k, v in feats.items() if k in base}
        score = sum(max(0.0, v) for v in z.values())
        lvl = "high" if score >= 3 else "low" if all(v < -1 for v in z.values()) else "normal"
        return {"level": lvl, "source": f"голос относительно обычной манеры клиента ({base['n']} записей)", "z": {k: round(v, 1) for k, v in z.items()}}
    hi = int(feats.get("loudness_db", -99) > -16) + int(feats.get("words_per_sec", 0) > 3.4) + int(feats.get("pitch_std_st", 0) > 4.0)
    return {"level": "high" if hi >= 2 else "normal", "source": "голос по общим порогам (личной нормы ещё нет)"}


def summary(h, diss, manner, ar, voiced=False):
    if h >= 2:
        if ar == "high":
            return "открытая враждебность"
        return "холодная враждебность: голос спокойный, но слова в адрес собеседника" if voiced else "враждебность в адрес собеседника"
    if manner == "rough":
        return "резкая манера речи, враждебности нет" + (" (на эмоциях)" if ar == "high" else "")
    if ar == "high" or diss >= 2:
        return "расстроен ситуацией, не враждебен"
    return "недоволен" if diss == 1 else "спокоен"


def tone_hint(h, diss, manner):
    if h >= 3:
        return "Клиент враждебен к собеседнику. Не спорь и не оправдывайся: коротко, что видишь и что уже сделано. Если клиент просит человека — передай оператору."
    if h == 2:
        return "Клиент резко высказывается о компании или сотруднике. Без обид и шаблонных извинений: признай проблему одной фразой и сразу конкретика."
    tips = []
    if manner == "rough":
        tips.append("Клиент говорит просто и резко — это его манера, а не агрессия. Отвечай так же просто, коротко, без канцелярита и без замечаний о манере речи.")
    if diss >= 2:
        tips.append("Клиент сильно устал от проблемы — сразу к делу, назови срок.")
    elif diss == 1:
        tips.append("Признай неудобство одной фразой.")
    return " ".join(tips)


def analyze(text: str, voice: dict | None = None, use_llm: bool | None = None, client_id: str | None = None) -> dict:
    r = rules(text)
    res = {k: r[k] for k in ("manner", "dissatisfaction", "hostility", "target", "churn_risk")}
    res["reason"] = "; ".join(r["why"]) or "по словарям"
    res["rules"] = r["signals"]
    if config.EMOTION_LLM if use_llm is None else use_llm:
        try:
            j = llm.complete_json(SYSTEM, text, SCHEMA)
            res["llm"] = j
            res["dissatisfaction"] = max(res["dissatisfaction"], j["dissatisfaction"])
            if j["target"] in ("company", "employee"):
                res["hostility"] = max(res["hostility"], j["hostility"])
                res["target"] = j["target"] if j["hostility"] >= res["hostility"] else res["target"]
            res["churn_risk"] = res["churn_risk"] or j["churn_risk"]
            res["reason"] = j["reason"] if not r["why"] else res["reason"]
        except Exception as e:
            res["llm_error"] = str(e)[:200]
    ar = arousal(voice, client_id, r["text_arousal"])
    if voice and client_id:
        profiles.update(client_id, voice, res["manner"], res["hostility"])
    h, d = res["hostility"], res["dissatisfaction"]
    res.update(arousal=ar, manner_label=MANNER[res["manner"]], target_label=TARGET[res["target"]],
               summary=summary(h, d, res["manner"], ar["level"], bool(voice)), tone_hint=tone_hint(h, d, res["manner"]),
               level=h)
    res["label"] = res["summary"]
    return res

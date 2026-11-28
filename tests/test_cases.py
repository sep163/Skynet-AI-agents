import json

from app import config
from app.cases import b2b, comms, finmodel, onboarding
from app.cases.common import money_from, price_check, prices_in, split_next, violations


def test_comms_checker_labeled_texts():
    for b in json.loads((config.DATA / "comms/bad_texts.json").read_text(encoding="utf-8")):
        got = {i["type"] for i in comms.check(b["text"], b["channel"], b["segment"])["issues"]}
        assert set(b["expect"]) <= got and (b["expect"] or not got), b["id"]


def test_price_check_and_next_block():
    assert price_check("Тариф 690 ₽ и роутер 99 руб", {690.0, 99.0})["ok"]
    assert price_check("Всего 490 ₽", {690.0})["unknown"] == [490.0]
    assert prices_in("Подключение 1 500 ₽, роутер 4990 ₽, итого 12 000,50 руб") == [1500.0, 4990.0, 12000.5]
    assert prices_in("за 3 1000 ₽") == [1000.0]
    s = split_next("Готово.\n\nЧто дальше: мастер позвонит за час")
    assert s["body"] == "Готово." and s["next"].startswith("мастер")


def test_say_equals_did_guard():
    assert violations("onboarding", "Подтверждаю запись на завтра", [])
    assert not violations("onboarding", "Подтверждаю запись на завтра", [{"tool": "book_installation", "result": {"id": "Z1"}}])
    assert violations("b2b", "Я подготовил КП для вас", [{"tool": "save_brief", "result": {"id": "L1"}}])


def test_address_matching_requires_house_number():
    onboarding.reset()
    assert onboarding.find_address("Савушкина 12 кв 5")["address"] == "ул. Савушкина, 12"
    assert onboarding.find_address("ул. Савушкина, д. 14")["address"] == "ул. Савушкина, 14"
    assert onboarding.find_address("Савушкина 99") is None


def test_onboarding_calc_and_booking():
    onboarding.reset()
    c = onboarding.calc("Дом 500", "rent", False, False)
    assert c["monthly_total_rub"] == 789 and c["first_month_total_rub"] == 100
    assert "error" in onboarding.book("", "", "Савушкина 12", "home500", "rent", False, False, 0)
    a = onboarding.book("Анна", "+7 911 123-45-67", "Савушкина 12", "home500", "rent", False, False, 0)
    assert a["id"].startswith("ZAYAVKA") and len(onboarding.S.applications) == 1


def test_b2b_brief_needs_confirmation_and_draft_is_internal():
    b2b.reset()
    assert "error" in b2b.save_brief("ООО Зерно", "Ирина", "+7900", "кофейни", 3, 12, "", [], [], [], "срочно", False)
    r = b2b.save_brief("ООО Зерно", "Ирина", "+7900", "сеть кофеен", 3, 12, "домашний 100", ["касса и эквайринг"], ["интернет падал"], ["Wi-Fi для гостей", "2 камеры на точку"], "срочно", True)
    assert "draft" not in r and "monthly_total_rub" not in str(r)
    ids = {l["service_id"]: l["qty"] for l in b2b.S.leads[0]["draft"]["lines"]}
    assert ids["backup_lte"] == 3 and ids["cctv"] == 6


def test_finmodel_ranks_and_scenarios():
    r = finmodel.compute()
    assert r["best"] == "support" and len(r["directions"]) == 5
    sc = finmodel.scenarios()
    assert sc["pessimistic"]["support"] < sc["base"]["support"] < sc["optimistic"]["support"]
    lower = finmodel.compute(overrides={"directions.support.automation_rate": 0.1})
    assert next(d for d in lower["directions"] if d["key"] == "support")["effect_year"] < next(d for d in r["directions"] if d["key"] == "support")["effect_year"]


def test_price_check_ignores_speed_and_channels():
    onboarding.reset()
    allowed = money_from(onboarding.S.price)
    assert 690.0 in allowed and 4990.0 in allowed
    assert not price_check("Тариф «Дом 500» стоит 500 ₽ в месяц", allowed)["ok"]  # 500 — скорость, а не цена
    assert not price_check("200 каналов за 200 ₽", allowed)["ok"]
    calc = onboarding.calc("home500", "rent", False, False)
    assert price_check("Каждый месяц 789 ₽, первый месяц 100 ₽", allowed | money_from(calc))["ok"]


def test_comms_does_not_merge_numbers():
    issues = comms.check("Для 3 1000 ₽ скидка. «Дом 1000» — 890 ₽ в месяц.", "post", "семьи")["issues"]
    assert [i["msg"] for i in issues if i["type"] == "unknown_price"] == ["цена 1000 ₽ не найдена в прайсе"]


def test_tone_eval_old_vs_new():
    import io
    from contextlib import redirect_stdout
    from scripts import eval_tone
    with redirect_stdout(io.StringIO()):
        eval_tone.main()
    r = json.loads(open("reports/tone_eval.json").read())
    assert r["new"]["agg"] == r["n"] and r["new"]["esc"] == r["n"] and r["old"]["agg"] < r["n"]


def test_voice_baseline_is_personal(tmp_path, monkeypatch):
    from app import emotion, profiles
    monkeypatch.setattr(profiles, "PATH", tmp_path / "p.json")
    profiles.reset()
    loud = {"loudness_db": -10, "words_per_sec": 3.8, "pitch_std_st": 4.5}
    for d in (0, 0.5, -0.5):
        emotion.analyze("Здравствуйте, вопрос по тарифу", {k: v + d for k, v in loud.items()}, use_llm=False, client_id="c9")
    e = emotion.analyze("Здравствуйте, вопрос по тарифу", loud, use_llm=False, client_id="c9")
    assert e["arousal"]["level"] == "normal" and "обычной манеры" in e["arousal"]["source"]

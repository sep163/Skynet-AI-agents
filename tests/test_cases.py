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
    assert r["new"]["agg"] == r["n"] and r["old"]["agg"] < r["n"]


def test_conduct_operator_rudeness_and_reactions():
    from app.cases import quality
    quality.S = quality.State()
    d = {x["id"]: x for x in quality.S.dialogs}
    c = lambda i: quality.conduct(d[i]["dialog"], None, d[i]["agent"])
    assert c("D11")["client"]["hostility"] == 0 and not c("D11")["issues"]
    assert c("D12")["client"]["hostility"] == 2 and not c("D12")["issues"]
    d13 = c("D13")
    assert d13["operator"]["hostility"] >= 2 and any(r["reaction"] == "ответил резкостью" for r in d13["reactions"])


def test_sales_compliance_and_rop_dashboard(monkeypatch, tmp_path):
    from app.cases import sales
    monkeypatch.setattr(sales, "CACHE", tmp_path / "c.json")
    monkeypatch.setattr(sales, "FEEDBACK", tmp_path / "f.json")
    sales.S = sales.State()
    calls = {c["id"]: c for c in sales.S.calls}
    assert "490" in sales.compliance(calls["S03"]["dialog"])["evidence"]
    assert "лучший" in sales.compliance(calls["S05"]["dialog"])["evidence"]
    assert sales.compliance(calls["S10"]["dialog"])["score"] == 2

    def fake(cid, needs):
        st = [{"id": i, "score": needs if i == "needs" else 2, "evidence": "q", "mistake": "" if needs == 2 else "не выяснил", "better": "b"}
              for i in sales.stage_ids(calls[cid]["script"])]
        return {"stages": st, "objections": [{"type": "price", "quote": "дорого", "handled": needs == 2}], "missed_signals": [], "outcome_reason": "",
                "feedback": {"good": "g", "fix": ["f"], "phrase": "p"}, "risk_zones": [] if needs else ["needs"], "total": 80, "analyzed_at": "2026-11-28T10:00"}
    for cid in calls:
        sales.S.results[cid] = fake(cid, 0 if calls[cid]["manager"] == "Денис Мельников" else 2)
    d = sales.dashboard()
    assert d["by_manager"]["Денис Мельников"]["stages"]["needs"] == 0
    assert d["weak_links"][0]["who"] == "Денис Мельников" and d["weak_links"][0]["stage_id"] == "needs"
    assert d["objections"]["дорого"]["count"] == len(calls)
    f = sales.add_feedback(sales.FeedbackIn(manager="Денис Мельников", text="Сначала вопросы, потом прайс", stage_id="needs"))
    assert f["author"] == "Супервайзер" and f["risk_zones"] == ["Потребности"]


def _fake_sales(monkeypatch, tmp_path):
    from app.cases import sales
    monkeypatch.setattr(sales, "CACHE", tmp_path / "c.json")
    monkeypatch.setattr(sales, "FEEDBACK", tmp_path / "f.json")
    sales.S = sales.State()
    for c in sales.S.calls:
        weak = c["manager"] == "Денис Мельников"
        sales.S.results[c["id"]] = {
            "stages": [{"id": i, "score": 0 if weak and i == "needs" else 2, "evidence": "q", "mistake": "m" if weak else "", "better": "b"} for i in sales.stage_ids(c["script"])],
            "objections": [], "missed_signals": [], "outcome_reason": "", "feedback": {"good": "g", "fix": ["f"], "phrase": "p"},
            "goal_result": "failed" if weak else "achieved", "close_type": "no_next_step" if weak else "deal",
            "style": {"pressure": 2 if weak else 0, "passivity": 0, "composure": 0 if weak else 2}, "style_verdict": "calmer" if weak else "balanced",
            "wrong_phrases": [{"quote": "ну дешевле нет", "why": "w", "better": "b"}] if weak else [], "client_segment": "calm",
            "risk_zones": ["needs"] if weak else [], "total": 40 if weak else 90, "analyzed_at": "2026-11-28T10:00"}
    return sales


def test_three_levels_supervisor_and_rop(monkeypatch, tmp_path):
    sales = _fake_sales(monkeypatch, tmp_path)
    d = sales.department("Ольга Петрова")
    assert {m["manager"] for it in d["items"] for m in it["managers"]} <= {"Анна Котова", "Денис Мельников"}
    p = sales.profile("Денис Мельников")
    assert p["verdict"] == "calmer" and p["plan"][0]["key"] == "calmer" and any(x["key"] == "needs" for x in p["plan"])
    assert p["wrong_phrases"] and p["talk"]["manager_talk_share"] > 0
    r = sales.rop_summary()
    assert [t["supervisor"] for t in r["teams"]] == ["Ольга Петрова", "Сергей Ким"]
    assert any(a["manager"] == "Денис Мельников" for a in r["teams"][0]["attention"])
    a = sales.assign(sales.AssignIn(manager="Денис Мельников", key="calmer", author="Супервайзер Ольга Петрова"))
    assert a["kind"] == "обучение" and sales.practice_list("Денис Мельников")["scenarios"][3]["assigned"]


def test_practice_simulator_with_fake_llm(monkeypatch, tmp_path):
    sales = _fake_sales(monkeypatch, tmp_path)
    from app import llm
    monkeypatch.setattr(llm, "complete", lambda s, u: "Клиент: Ну ладно, давайте 中文 завтра.")
    monkeypatch.setattr(sales, "analyze", lambda call: sales.S.results["S01"] | {"analyzed_at": "x"})
    st = sales.practice_start("Анна Котова", "hostile")
    r = sales.practice_say(st["session"], "Понимаю, мастер будет сегодня с 18 до 20")
    assert r["client"] == "Ну ладно, давайте  завтра."
    f = sales.practice_finish(st["session"])
    assert f["result"]["total"] == 90 and any(x["kind"] == "тренировка" for x in sales.S.feedback if "kind" in x)


def _review(monkeypatch, tmp_path):
    sales = _fake_sales(monkeypatch, tmp_path)
    from app.cases import review
    monkeypatch.setattr(review, "PATH", tmp_path / "r.json")
    review.reset()
    sales.S.raw = dict(sales.S.results)
    sales.rebuild()
    return sales, review


def test_dispute_flow_supervisor_rop_changes_analytics(monkeypatch, tmp_path):
    sales, review = _review(monkeypatch, tmp_path)
    assert sales.profile("Денис Мельников")["stages"]["needs"] == 0
    cid = next(c["id"] for c in sales.S.calls if c["manager"] == "Денис Мельников")
    d = review.dispute(review.DisputeIn(call_id=cid, field="stage:needs", proposed=2, comment="я задал два вопроса", author="Денис Мельников"))
    assert d["status"] == "supervisor" and d["supervisor"] == "Ольга Петрова" and d["model_value"] == 0
    d = review.supervisor_decide(d["id"], review.SupervisorIn(decision="reject", author="Ольга Петрова", comment="вопросов не было"))
    assert d["status"] == "rejected_sup" and any(f.get("kind") == "спор" for f in sales.S.feedback)
    d = review.appeal(d["id"], "прослушайте 0:40")
    assert d["status"] == "rop"
    with __import__("pytest").raises(Exception):
        review.appeal(d["id"])
    r = review.rop_decide(d["id"], review.RopIn(decision="set", value=1, comment="один вопрос был", rule="один открытый вопрос — это 1, не 0"))
    assert r["dispute"]["status"] == "accepted" and sales.S.results[cid]["stages"][[s["id"] for s in sales.S.results[cid]["stages"]].index("needs")]["score"] == 1
    assert sales.S.raw[cid]["stages"][0]["score"] in (0, 2)  # исходный разбор ИИ не тронут
    assert "один открытый вопрос" in review.precedents_prompt(sales.S.calls[0]["script"])
    q = review.model_quality()
    assert q["disputes"]["accepted"] == 1 and q["agreement"] == 0.0


def test_rop_manual_verdict_and_wrong_phrase(monkeypatch, tmp_path):
    sales, review = _review(monkeypatch, tmp_path)
    cid = next(c["id"] for c in sales.S.calls if c["manager"] == "Денис Мельников")
    review.decide_verdict(review.VerdictIn(call_id=cid, field="wrong_phrase:0", value="not_error", comment="уместно"))
    review.decide_verdict(review.VerdictIn(call_id=cid, field="close_type", value="no_next_step"))
    assert sales.S.results[cid]["wrong_phrases"] == []
    assert review.model_quality()["by_field"][1]["agreement"] == 1.0
    import pytest
    with pytest.raises(Exception):
        review.decide_verdict(review.VerdictIn(call_id=cid, field="stage:needs", value=7))


def test_aggression_calibration_follows_people(monkeypatch, tmp_path):
    sales, review = _review(monkeypatch, tmp_path)
    c = review.calibration()
    assert c["fitted"] and c["n_calls"] >= 18
    s13 = next(x for x in c["calls"] if x["call_id"] == "S13")
    assert s13["rules"] == 0 and s13["human"] >= 2 and s13["calibrated"] >= 1.5  # «Вы там совсем охренели?» — люди видят враждебность
    assert sales.S.results["S13"]["client_segment"] == "hostile"
    assert next(r for r in c["raters"] if r["rater"] == "Денис Мельников")["reading"] == "строже среднего"
    review.decide_verdict(review.VerdictIn(call_id="S13", field="client_hostility", value=1, comment="грубо, но не к менеджеру"))
    assert sales.S.results["S13"]["client_hostility"] == {**sales.S.results["S13"]["client_hostility"], "final": 1, "source": "решение РОП"}
    assert review.targets()["S13"]["weight"] == 3.0


def test_acoustics_overlaps_and_raised_voice():
    from app.cases import sales
    u = [{"role": "client", "start": 0, "end": 4, "loudness_db": -30}, {"role": "operator", "start": 2.5, "end": 6, "loudness_db": -28},
         {"role": "client", "start": 10, "end": 12, "loudness_db": -18}, {"role": "operator", "start": 12.5, "end": 14, "loudness_db": -29},
         {"role": "client", "start": 14.5, "end": 16, "loudness_db": -31}]
    a = sales.acoustics(u)
    assert a["interruptions"] == {"manager": 1, "client": 0} and a["overlap_s"] == 1.5 and a["long_pauses"] == 1
    assert a["raised_voice"]["client"] == 1 and a["manager_response_s"] == 0.5


def test_precedents_reach_llm_prompt(monkeypatch, tmp_path):
    sales, review = _review(monkeypatch, tmp_path)
    cid = sales.S.calls[0]["id"]
    review.decide_verdict(review.VerdictIn(call_id=cid, field="goal_result", value="partly", rule="запись без даты — цель достигнута частично"))
    seen = {}
    from app import llm
    monkeypatch.setattr(llm, "complete_json", lambda sys, user, sch: seen.setdefault("sys", sys) and (_ for _ in ()).throw(RuntimeError("stop")))
    with __import__("pytest").raises(RuntimeError):
        sales.analyze(sales.S.calls[0])
    assert "запись без даты" in seen["sys"]


def _exps(monkeypatch, tmp_path):
    from app.cases import experiments as E
    monkeypatch.setattr(E, "PATH", tmp_path / "e.json")
    E.reset()
    return E


def test_ab_schedule_crossover_and_today(monkeypatch, tmp_path):
    E = _exps(monkeypatch, tmp_path)
    e = E.create(E.ExpIn(title="t", segments=["tech_loyal"], b_phrase="мастер будет завтра с 10 до 12", author="a", managers=E.DEMO_MANAGERS))
    assert e["status"] == "draft"
    assert E.create(E.ExpIn(title="m", segments=["price"], b_phrase="посчитаем годовую оплату", author="Денис", role="manager"))["status"] == "proposed"
    E.start(e["id"], "2026-11-23")
    sc = E.schedule(e)
    starts = [r["arms"][0] for r in sc["rows"]]
    assert starts.count("A") == starts.count("B") == 6
    assert all(r["arms"] == [r["arms"][0]] * 2 + [{"A": "B", "B": "A"}[r["arms"][0]]] * 2 + [r["arms"][0]] * 2 for r in sc["rows"])
    assert E.arm_of(e, "Анна Котова", "2026-11-29") is None and E.today("Анна Котова", "2026-11-23")[0]["arm"] in "AB"


def test_ab_phrase_detection_and_segments(monkeypatch, tmp_path):
    E = _exps(monkeypatch, tmp_path)
    call = {"dialog": [["client", "Опять пропал интернет, третий день"], ["agent", "Вижу линию: мастер приедет завтра с 10 до 12, начислю компенсацию за эти дни"]],
            "script": "inbound_b2c"}
    assert E.phrase_used(call, "Мастер будет завтра с 10 до 12, за дни без связи начислю компенсацию")["used"]
    assert not E.phrase_used(call, "Давайте посчитаем годовую оплату со скидкой")["used"]
    assert E.phrase_used(call, "что угодно", keywords=["компенсац", "завтра"])["used"]
    assert E.segment(call, None) == "tech_loyal"
    assert E.segment({"dialog": [["client", "Хочу расторгнуть договор, ухожу к другому"]], "script": "inbound_b2c"}, None) == "leaving"
    assert E.segment({"dialog": [["client", "А роутер нужен свой?"]], "script": "inbound_b2c"}, None) == "new"


def test_ab_analysis_stratified_effect_and_adherence(monkeypatch, tmp_path):
    E = _exps(monkeypatch, tmp_path)
    e = E.create(E.ExpIn(title="t", segments=["tech_loyal"], b_phrase="мастер будет завтра с 10 до 12", author="a", managers=E.DEMO_MANAGERS, min_effect=0.12))
    E.start(e["id"], "2026-11-23")
    E.simulate(e, 100, true_effect=0.12)
    a = E.analyze(e)
    assert a["status"] == "b_wins" and a["itt"]["strata"] == 12 and 0.05 < a["itt"]["diff"] < 0.17
    assert a["applied_gap"] > 0.5 and abs(a["phrase_effect"] - 0.12) < 0.06
    E.simulate(e, 100, true_effect=0.0)
    assert E.analyze(e)["status"] in ("no_diff", "collecting")
    f = E.finish(e["id"])
    assert f["status"] == "finished" and not E.X["standards"]
    # без применения фразы тест предупреждает, что проверяет не фразу
    e2 = E.create(E.ExpIn(title="t2", segments=["new"], b_phrase="два времени монтажа на выбор", author="a", managers=E.DEMO_MANAGERS))
    E.start(e2["id"], "2026-11-23")
    E.X["events"] = [ev | {"applied": False} if ev["exp"] == e["id"] else ev for ev in E.X["events"]]
    E.simulate(e2, 30, true_effect=0.1)
    E.X["events"] = [ev | {"applied": False} for ev in E.X["events"] if ev["exp"] == e2["id"]]
    assert any("почти не проверяет" in w for w in E.analyze(e2)["warnings"])


def test_ab_stratified_math():
    from app.cases import experiments as E
    r = E.stratified([(30, 100, 20, 100), (30, 100, 20, 100)])
    assert r["diff"] == 0.1 and r["p"] < 0.05 and r["ci"][0] > 0
    assert E.stratified([(5, 10, 0, 0)])["strata"] == 0
    assert 540 < E.need_per_arm(0.30, 0.38) < 560


def test_ab_ingest_from_analysis(monkeypatch, tmp_path):
    sales = _fake_sales(monkeypatch, tmp_path)
    E = _exps(monkeypatch, tmp_path)
    e = E.create(E.ExpIn(title="t", segments=["tech_loyal", "new"], b_phrase="Записать вас на монтаж? Есть завтра с 10 до 12", author="a",
                         managers=["Анна Котова"], days=6))
    E.start(e["id"], "2026-11-23")
    c = next(x for x in sales.S.calls if x["id"] == "S01")
    ev = E.ingest_call(c, sales.S.results["S01"], sales.S.script)
    assert ev and ev[0]["applied"] and ev[0]["success"] and ev[0]["segment"] == "new" and ev[0]["source"] == "разбор звонка"
    assert E.ingest_call(c, sales.S.results["S01"]) and len([x for x in E.X["events"] if x["call_id"] == "S01"]) == 1

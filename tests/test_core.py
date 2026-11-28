from fastapi.testclient import TestClient

from app import emotion, llm, ops as ops_mod
from app.kb import KB
from app.ops import Ops


def test_kb_finds_wan_article():
    assert "100" in KB().search("порт 100 мбит роутер wan")[0]["title"]


def test_clean_day_no_incidents():
    o = Ops(seed=3)
    o.tick(600)
    assert o.sentinel.incidents == []


def test_cable_detected_before_impact_and_visit_proposed():
    o = Ops(seed=1)
    f = o.net.inject("cable_degradation", "sw-01/5")
    o.tick(15)
    inc = o.sentinel.open["sw-01/5"]
    assert inc["detected_at"] < f["impact_at"]
    act = next(a for a in o.actions if a["type"] == "dispatch_technician")
    assert act["status"] == "pending"
    assert any(a["type"] == "notify_clients" and a["status"] == "done" for a in o.actions)
    o.decide(act["id"], True)
    job = o._job(act["details"]["job_id"])
    assert job["status"] == "confirmed"
    o.complete_job(job["id"])
    o.tick(20)
    assert "sw-01/5" not in o.sentinel.open


def test_optic_predicted_before_failure():
    o = Ops(seed=2)
    f = o.net.inject("optic_degradation", "sw-04")
    o.tick(40)
    inc = o.sentinel.open["sw-04"]
    assert inc["kind"] == "optic_degradation" and inc["eta_failure_min"] > 30
    assert inc["detected_at"] < f["impact_at"]


def test_switch_down_is_one_incident_not_24():
    o = Ops(seed=4)
    o.net.inject("switch_power", "sw-03")
    o.tick(10)
    assert [i["target"] for i in o.sentinel.incidents] == ["sw-03"]


def test_policy_auto_vs_approval():
    o = Ops(seed=5)
    assert o.promise_payment("c001")["status"] == "done"
    assert o.clients["c001"]["status"] == "active"
    assert o.request_visit("c003", "нет линка")["status"] == "ждёт подтверждения диспетчера"


def test_emotion_axes_manner_is_not_aggression():
    a = lambda t, v=None: emotion.analyze(t, v, use_llm=False)
    assert a("Подскажите тариф")["hostility"] == 0
    e = a("Бля, опять нихрена не работает", {"loudness_db": -10, "words_per_sec": 4, "pitch_std_st": 5})
    assert e["manner"] == "rough" and e["hostility"] == 0 and e["arousal"]["level"] == "high"
    q = a("Девушка, вы вообще головой думаете?", {"loudness_db": -28, "words_per_sec": 2, "pitch_std_st": 1})
    assert q["hostility"] == 2 and q["target"] == "employee" and "холодная" in q["summary"]
    assert a("Иди ты на хрен со своими советами")["hostility"] == 3
    c = a("Я расторгну договор")
    assert c["churn_risk"] and c["hostility"] == 0


def test_api_chat_with_mocked_llm(monkeypatch):
    from app.main import app
    ops_mod.reset()
    from tests.test_graph import Scripted
    monkeypatch.setattr(llm, "adapter", lambda: Scripted([llm.Step({"role": "assistant"}, [("1", "get_client_profile", {})], "", "tool_use"),
                                                          llm.Step({"role": "assistant", "content": "ok"}, [], "ok", "end")]))
    monkeypatch.setattr(emotion.config, "EMOTION_LLM", False)
    c = TestClient(app)
    r = c.post("/api/chat", json={"client_id": "c002", "messages": [{"role": "user", "content": "привет"}]}).json()
    assert r["answer"] == "ok" and r["trace"][0]["result"]["id"] == "c002"
    c.post("/api/sim/inject", json={"kind": "switch_power", "target": "sw-02"})
    s = c.post("/api/sim/step", json={"minutes": 5}).json()
    assert any(i["target"] == "sw-02" for i in s["incidents"])

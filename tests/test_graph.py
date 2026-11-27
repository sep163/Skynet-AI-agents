"""Граф LangGraph без LLM: модель заменена сценарием шагов."""
from app import graph, llm
from app.cases import common, onboarding


class Scripted:
    """Поддельная модель: отдаёт заранее заданные шаги и запоминает, что ей прислали."""
    name = "scripted"

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def start(self, history):
        return [dict(m) for m in history]

    def defs(self, tools):
        return [t.name for t in tools.values()]

    def step(self, system, msgs, defs):
        self.seen.append(msgs)
        return self.steps.pop(0)

    def results(self, outs):
        return [{"role": "tool", "tool_name": n, "content": out} for _, n, out, _ in outs]


def say(text):
    return llm.Step({"role": "assistant", "content": text}, [], text, "end")


def call(name, args):
    return llm.Step({"role": "assistant", "content": ""}, [("1", name, args)], "", "tool_use")


BOOK = {"name": "Анна", "phone": "+7 911 123-45-67", "address": "Савушкина 12", "tariff_id": "home500", "router": "rent",
        "tv_box": False, "static_ip": False, "slot_index": 0}


def run(steps, case="onboarding"):
    onboarding.reset()
    ad = Scripted(steps)
    text, trace, g = graph.run(case, "sys", [{"role": "user", "content": "привет"}], onboarding.tools(),
                               common.money_from(onboarding.S.price), "calc_first_payment", adapter=ad)
    return text, trace, g, ad


def test_tool_call_then_answer():
    text, trace, g, _ = run([call("check_address", {"address": "Савушкина 12"}), say("Дом подключён.")])
    assert text == "Дом подключён." and trace[0]["result"]["status"] == "connected" and g == {"violations": [], "retried": False, "ok": True}


def test_guard_makes_model_do_what_it_said():
    text, trace, g, ad = run([say("Записал вас на монтаж!"), call("book_installation", BOOK), say("Записал вас на монтаж!")])
    assert g["retried"] and g["ok"] and not g["left"]
    assert [t["tool"] for t in trace] == ["book_installation"] and len(onboarding.S.applications) == 1
    assert "Автопроверка" in ad.seen[1][-1]["content"]  # замечание пришло в ту же историю


def test_guard_marks_unfixed_answer():
    text, _, g, _ = run([say("Тариф стоит 500 ₽."), say("Тариф стоит 500 ₽.")])
    assert g["retried"] and not g["ok"] and g["left"] and text.endswith(graph.NOTE)


def test_no_guard_mode_sends_answer_as_is(monkeypatch):
    monkeypatch.setattr(common, "GUARD", False)
    text, _, g, _ = run([say("Записал вас на монтаж!")])
    assert text == "Записал вас на монтаж!" and g.get("disabled")


def test_step_limit():
    text, _, g, _ = run([call("get_tariffs", {})] * graph.MAX_STEPS, case="b2b")
    assert text == llm.STEP_LIMIT


def test_graph_shape():
    m = graph.mermaid()
    assert all(n in m for n in ("agent", "tools", "guard"))

"""Агентный граф LangGraph — общий для поддержки, подключения и B2B.

    START ─▶ agent ──вызовы инструментов──▶ tools ─▶ agent
               │ ◀──pause_turn──┘
               └──ответ готов──▶ guard ──расхождение, 1-я попытка──▶ agent
                                   └──ok / уже исправляли──▶ END

agent — шаг модели (Claude или Ollama, app/llm.py).
tools — выполняет инструменты; они реально меняют состояние (заявки, выезды, обещанный платёж).
guard — автопроверки «сказал = сделал» и «каждая сумма из прайса или расчёта» (app/cases/common.py).
  При расхождении модель получает замечание и одну попытку исправиться: вызвать инструмент или переписать ответ.

Инструменты и настройки проверки передаются через config["configurable"]: они привязаны к клиенту
и не хранятся в состоянии. Трассировка Langfuse — app/tracing.py.
"""
from functools import lru_cache
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import llm, tracing

MAX_STEPS = 10
NOTE = "\n\n(Уточнение: часть ответа не подтверждена системой — менеджер проверит и свяжется с вами.)"
FEEDBACK = ("[Автопроверка системы — это не клиент] {v} Если в диалоге уже есть все данные для действия — выполни его инструментом сейчас; "
            "если чего-то не хватает — спроси только недостающее, не переспрашивая сказанное. Не упоминай клиенту эту проверку. Дай клиенту полный ответ заново.")


class AgentState(TypedDict, total=False):
    case: str
    system: str
    messages: list   # история в формате провайдера
    pending: list    # вызовы инструментов, ждущие выполнения
    trace: list      # [{tool, args, result}] — для UI, автопроверок и eval
    text: str
    stop: str
    steps: int
    attempt: int     # 0 — первый ответ, 1 — после замечания автопроверки
    guard: dict


def _cfg(config) -> dict:
    return config["configurable"]


def agent(state: AgentState, config) -> dict:
    c = _cfg(config)
    if state.get("steps", 0) >= MAX_STEPS:
        return {"text": llm.STEP_LIMIT, "stop": "limit", "pending": []}
    st = c["adapter"].step(state["system"], state["messages"], c["defs"])
    upd = {"steps": state.get("steps", 0) + 1, "stop": st.stop, "pending": st.calls, "text": st.text}
    if st.message is not None:
        upd["messages"] = state["messages"] + [st.message]
    return upd


def tools(state: AgentState, config) -> dict:
    c = _cfg(config)
    trace, outs = list(state.get("trace", [])), []
    for cid, name, args in state["pending"]:
        res, js, err = llm.call_tool(c["tools"], name, args)
        trace.append({"tool": name, "args": args, "result": res})
        outs.append((cid, name, js, err))
    return {"trace": trace, "pending": [], "messages": state["messages"] + c["adapter"].results(outs)}


def guard(state: AgentState, config) -> dict:
    from .cases import common
    c = _cfg(config)
    text, trace, attempt = state["text"], state.get("trace", []), state.get("attempt", 0)
    if not common.GUARD or state.get("stop") == "refusal":
        return {"guard": {"violations": [], "retried": False, "ok": True, **({"disabled": True} if not common.GUARD else {})}}
    v = common.check_all(state["case"], text, trace, c.get("allowed_base"), c.get("calc_hint", "инструмент расчёта"))
    if attempt == 0:
        g = {"violations": v, "retried": bool(v), "ok": not v}
        if not v:
            return {"guard": g}
        # замечание идёт в ту же историю: модель видит свои вызовы инструментов и может довести действие до конца
        msgs = state["messages"] + ([{"role": "assistant", "content": text}] if state.get("stop") == "limit" else [])
        return {"guard": g, "attempt": 1, "steps": 0, "messages": msgs + [{"role": "user", "content": FEEDBACK.format(v=" ".join(v))}]}
    g = state["guard"] | {"ok": not v, "left": v}
    return {"guard": g, "text": text + NOTE if v else text}


def after_agent(state: AgentState) -> str:
    return {"tool_use": "tools", "pause": "agent"}.get(state.get("stop"), "guard")


def after_guard(state: AgentState) -> str:
    g = state["guard"]
    return "agent" if g.get("retried") and "left" not in g else END


@lru_cache
def build():
    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("guard", guard)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, {"tools": "tools", "agent": "agent", "guard": "guard"})
    g.add_edge("tools", "agent")
    g.add_conditional_edges("guard", after_guard, {"agent": "agent", END: END})
    return g.compile()


def run(case: str, system: str, history: list, tool_list: list, allowed_base: set | None = None,
        calc_hint: str = "инструмент расчёта", adapter: Any = None) -> tuple[str, list, dict]:
    ad = adapter or llm.adapter()
    reg = {t.name: t for t in tool_list}
    cfg = tracing.graph_config(f"skynet-{case}") | {
        "recursion_limit": 4 * MAX_STEPS + 10,
        "configurable": {"adapter": ad, "tools": reg, "defs": ad.defs(reg), "allowed_base": allowed_base, "calc_hint": calc_hint}}
    out = build().invoke({"case": case, "system": system, "messages": ad.start(history), "trace": [], "steps": 0, "attempt": 0}, cfg)
    return out["text"], out.get("trace", []), out["guard"]


def mermaid() -> str:
    """Схема графа для документации и защиты."""
    return build().get_graph().draw_mermaid()

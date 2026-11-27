"""Слой LLM: Claude (основной) и локальная модель через Ollama (запасной/офлайн).

Агентный цикл — граф LangGraph в app/graph.py. Здесь только адаптеры моделей с одинаковым интерфейсом
(шаг модели → вызовы инструментов → результаты) и разовые запросы complete / complete_json.
Каждый запрос к модели — generation в Langfuse, если заданы ключи (app/tracing.py).
"""
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import config, tracing

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}
REFUSAL = "Не могу помочь с этим запросом в чате — передаю диалог оператору."
STEP_LIMIT = "Мне нужно больше времени на диагностику — подключаю оператора."


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    fn: Callable[..., Any]

    def schema(self):
        return {"type": "object", "properties": self.params, "required": list(self.params), "additionalProperties": False}


@dataclass
class Step:
    """Ответ модели за один шаг: сообщение для истории, вызовы инструментов [(id, имя, аргументы)], текст."""
    message: Any
    calls: list
    text: str = ""
    stop: str = "end"  # end | tool_use | refusal | pause


_claude = None


def _client():
    global _claude
    if _claude is None:
        import anthropic
        _claude = anthropic.Anthropic()
    return _claude


def _usage(r):
    u = getattr(r, "usage", None)
    return {"input": u.input_tokens, "output": u.output_tokens} if u else None


def _claude_create(name: str = "claude", **kw):
    oc = {"effort": config.CLAUDE_EFFORT, **kw.pop("output_config", {})}
    with tracing.generation(name, config.CLAUDE_MODEL, {"system": kw.get("system"), "messages": kw.get("messages")}) as g:
        if config.CLAUDE_MODEL in FALLBACK_MODELS:
            r = _client().beta.messages.create(model=config.CLAUDE_MODEL, betas=[FALLBACK_BETA], fallbacks="default", output_config=oc, **kw)
        else:
            r = _client().messages.create(model=config.CLAUDE_MODEL, output_config=oc, **kw)
        g.update(output=[b.model_dump() for b in r.content], usage_details=_usage(r), metadata={"stop_reason": r.stop_reason})
    return r


def _text(content):
    return "".join(b.text for b in content if b.type == "text").strip()


def _ollama(payload, name: str = "ollama"):
    body = {"model": config.OLLAMA_MODEL, "stream": False, "think": config.OLLAMA_THINK, **payload}
    with tracing.generation(name, config.OLLAMA_MODEL, payload.get("messages")) as g:
        r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
        r.raise_for_status()
        d = r.json()
        g.update(output=d["message"], usage_details={"input": d.get("prompt_eval_count", 0), "output": d.get("eval_count", 0)})
    return d["message"]


class ClaudeAdapter:
    name = "claude"

    def start(self, history):
        return [dict(m) for m in history]

    def defs(self, tools):
        return [{"name": t.name, "description": t.description, "input_schema": t.schema(), "strict": True} for t in tools.values()]

    def step(self, system, msgs, defs) -> Step:
        r = _claude_create("agent_step", max_tokens=16000, system=system, tools=defs, messages=msgs)
        if r.stop_reason == "refusal":
            return Step(None, [], REFUSAL, "refusal")
        msg = {"role": "assistant", "content": r.content}
        if r.stop_reason == "pause_turn":
            return Step(msg, [], "", "pause")
        calls = [(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        if r.stop_reason != "tool_use" or not calls:
            return Step(msg, [], _text(r.content), "end")
        return Step(msg, calls, "", "tool_use")

    def results(self, outs):
        """outs: [(id, имя, json-строка, ошибка?)] → сообщения для истории."""
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": out, "is_error": err} for i, _, out, err in outs]}]


class OllamaAdapter:
    name = "ollama"

    def start(self, history):
        return [dict(m) for m in history]

    def defs(self, tools):
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema()}} for t in tools.values()]

    def step(self, system, msgs, defs) -> Step:
        m = _ollama({"messages": [{"role": "system", "content": system}, *msgs], "tools": defs}, "agent_step")
        calls = []
        for n, c in enumerate(m.get("tool_calls") or []):
            f = c["function"]
            args = f.get("arguments") or {}
            calls.append((str(n), f["name"], json.loads(args) if isinstance(args, str) else args))
        return Step(m, calls, "" if calls else (m.get("content") or "").strip(), "tool_use" if calls else "end")

    def results(self, outs):
        return [{"role": "tool", "tool_name": name, "content": out} for _, name, out, _ in outs]


def adapter():
    return ClaudeAdapter() if config.PROVIDER == "claude" else OllamaAdapter()


def call_tool(tools: dict, name: str, args: dict):
    """Выполняет инструмент. Возвращает (результат, json для модели, ошибка?)."""
    t = tools.get(name)
    with tracing.span(name, "tool", args) as sp:
        try:
            out, err = (t.fn(**args), False) if t else ({"error": f"нет инструмента {name}"}, True)
        except Exception as e:
            out, err = {"error": str(e)}, True
        sp.update(output=out, level="ERROR" if err else "DEFAULT")
    return out, json.dumps(out, ensure_ascii=False, default=str), err


def complete(system: str, user: str) -> str:
    if config.PROVIDER == "claude":
        r = _claude_create("complete", max_tokens=4000, system=system, messages=[{"role": "user", "content": user}])
        return REFUSAL if r.stop_reason == "refusal" else _text(r.content)
    return (_ollama({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, "complete").get("content") or "").strip()


def complete_json(system: str, user: str, schema: dict) -> dict:
    if config.PROVIDER == "claude":
        r = _claude_create("complete_json", max_tokens=4000, system=system, messages=[{"role": "user", "content": user}],
                           output_config={"format": {"type": "json_schema", "schema": schema}})
        if r.stop_reason == "refusal":
            raise RuntimeError("refusal")
        return json.loads(_text(r.content))
    m = _ollama({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "format": schema}, "complete_json")
    return json.loads(m.get("content") or "{}")

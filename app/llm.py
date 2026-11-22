"""Единый слой LLM: Claude (основной) и локальная модель через Ollama (запасной/офлайн).

Агентный цикл написан вручную, чтобы оба бэкенда делили один реестр инструментов и одинаковый trace.
"""
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import config

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


_claude = None


def _client():
    global _claude
    if _claude is None:
        import anthropic
        _claude = anthropic.Anthropic()
    return _claude


def _claude_create(**kw):
    oc = {"effort": config.CLAUDE_EFFORT, **kw.pop("output_config", {})}
    if config.CLAUDE_MODEL in FALLBACK_MODELS:
        return _client().beta.messages.create(model=config.CLAUDE_MODEL, betas=[FALLBACK_BETA], fallbacks="default", output_config=oc, **kw)
    return _client().messages.create(model=config.CLAUDE_MODEL, output_config=oc, **kw)


def _text(content):
    return "".join(b.text for b in content if b.type == "text").strip()


def _call(tools, name, args, trace):
    t = tools.get(name)
    try:
        out, err = (t.fn(**args), False) if t else ({"error": f"нет инструмента {name}"}, True)
    except Exception as e:
        out, err = {"error": str(e)}, True
    trace.append({"tool": name, "args": args, "result": out})
    return json.dumps(out, ensure_ascii=False, default=str), err


def run_agent(system: str, history: list[dict], tools: list[Tool], max_steps: int = 10):
    reg = {t.name: t for t in tools}
    return (_claude_agent if config.PROVIDER == "claude" else _ollama_agent)(system, history, reg, max_steps)


def _claude_agent(system, history, tools, max_steps):
    msgs, trace = [dict(m) for m in history], []
    defs = [{"name": t.name, "description": t.description, "input_schema": t.schema(), "strict": True} for t in tools.values()]
    for _ in range(max_steps):
        r = _claude_create(max_tokens=16000, system=system, tools=defs, messages=msgs)
        if r.stop_reason == "refusal":
            return REFUSAL, trace
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason == "pause_turn":
            continue
        uses = [b for b in r.content if b.type == "tool_use"]
        if r.stop_reason != "tool_use" or not uses:
            return _text(r.content), trace
        results = []
        for u in uses:
            out, err = _call(tools, u.name, u.input, trace)
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out, "is_error": err})
        msgs.append({"role": "user", "content": results})
    return STEP_LIMIT, trace


def _ollama(payload):
    body = {"model": config.OLLAMA_MODEL, "stream": False, "think": config.OLLAMA_THINK, **payload}
    r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
    r.raise_for_status()
    return r.json()["message"]


def _ollama_agent(system, history, tools, max_steps):
    msgs, trace = [{"role": "system", "content": system}, *history], []
    defs = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema()}} for t in tools.values()]
    for _ in range(max_steps):
        m = _ollama({"messages": msgs, "tools": defs})
        msgs.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            return (m.get("content") or "").strip(), trace
        for c in calls:
            f = c["function"]
            args = f.get("arguments") or {}
            out, _ = _call(tools, f["name"], json.loads(args) if isinstance(args, str) else args, trace)
            msgs.append({"role": "tool", "tool_name": f["name"], "content": out})
    return STEP_LIMIT, trace


def complete(system: str, user: str) -> str:
    if config.PROVIDER == "claude":
        r = _claude_create(max_tokens=4000, system=system, messages=[{"role": "user", "content": user}])
        return REFUSAL if r.stop_reason == "refusal" else _text(r.content)
    return (_ollama({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).get("content") or "").strip()


def complete_json(system: str, user: str, schema: dict) -> dict:
    if config.PROVIDER == "claude":
        r = _claude_create(max_tokens=4000, system=system, messages=[{"role": "user", "content": user}],
                           output_config={"format": {"type": "json_schema", "schema": schema}})
        if r.stop_reason == "refusal":
            raise RuntimeError("refusal")
        return json.loads(_text(r.content))
    m = _ollama({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "format": schema})
    return json.loads(m.get("content") or "{}")

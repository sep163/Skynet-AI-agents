"""Трассировка в Langfuse. Включается ключами LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY и LANGFUSE_HOST в .env.

Что попадает в трассу одного ответа клиенту:
- узлы графа LangGraph (agent → tools → guard) — через CallbackHandler;
- каждый запрос к модели — generation с токенами;
- каждый вызов инструмента — tool с аргументами и результатом;
- session_id диалога и user_id клиента — чтобы в Langfuse открыть весь разговор.
Без ключей все функции — пустые заглушки, поведение не меняется.
"""
import os
from contextlib import contextmanager, nullcontext


class _Noop:
    def update(self, **kw):
        return self


def enabled() -> bool:
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return False
    try:
        import langfuse  # noqa: F401
        return True
    except ImportError:
        return False


@contextmanager
def _observation(name, as_type, **kw):
    if not enabled():
        yield _Noop()
        return
    from langfuse import get_client
    with get_client().start_as_current_observation(name=name, as_type=as_type, **kw) as o:
        yield o


def generation(name: str, model: str, input):
    return _observation(name, "generation", model=model, input=input)


def span(name: str, as_type: str = "span", input=None):
    return _observation(name, as_type, input=input)


def attributes(session_id: str | None = None, user_id: str | None = None, tags: list[str] | None = None):
    """Привязка всех наблюдений внутри блока к диалогу и клиенту."""
    if not enabled():
        return nullcontext()
    from langfuse import propagate_attributes
    return propagate_attributes(session_id=session_id, user_id=user_id, tags=tags)


def graph_config(run_name: str) -> dict:
    """Конфиг запуска графа: колбэк Langfuse, если включён."""
    cfg = {"run_name": run_name}
    if enabled():
        from langfuse.langchain import CallbackHandler
        cfg["callbacks"] = [CallbackHandler()]
    return cfg


def flush():
    if enabled():
        from langfuse import get_client
        get_client().flush()

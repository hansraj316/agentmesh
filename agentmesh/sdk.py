"""AgentMesh Python SDK: ``@mesh.agent`` decorator, ``mesh.run`` grouping,
and ``mesh.record_usage`` token/cost accounting."""

import contextvars
import functools
import inspect
import time
import uuid
import warnings
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar

from agentmesh.events import Event
from agentmesh.store import EventStore

F = TypeVar("F", bound=Callable[..., Any])

_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agentmesh_run_id", default=None
)

_current_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agentmesh_span_id", default=None
)

_current_usage: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "agentmesh_usage", default=None
)


def _new_span_fields() -> Dict[str, str]:
    """Span payload fields for a fresh invocation: its own span_id plus the
    caller's span as ``parent_span_id``, if there is one (AMP v0.2)."""
    fields = {"span_id": str(uuid.uuid4())}
    parent = _current_span_id.get()
    if parent is not None:
        fields["parent_span_id"] = parent
    return fields


class Mesh:
    """Entry point for instrumenting agents.

    If no store is provided, one is resolved per emit so the ``AGENTMESH_DB``
    environment variable is honored even when set after import.
    """

    def __init__(self, store: Optional[EventStore] = None) -> None:
        self._store = store

    def _resolve_store(self) -> EventStore:
        if self._store is not None:
            return self._store
        return EventStore()

    def _emit(self, run_id: str, agent: str, type: str, payload: Dict[str, Any]) -> None:
        self._resolve_store().append(Event.new(run_id, agent, type, payload))

    @contextmanager
    def run(self, run_id: Optional[str] = None) -> Iterator[str]:
        """Group all decorated calls inside this block under one run_id."""
        rid = run_id if run_id is not None else str(uuid.uuid4())
        token = _current_run_id.set(rid)
        try:
            yield rid
        finally:
            _current_run_id.reset(token)

    def current_run_id(self) -> Optional[str]:
        return _current_run_id.get()

    def record_usage(
        self,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        model: Optional[str] = None,
    ) -> None:
        """Accumulate token/cost usage onto the current ``@mesh.agent`` span.

        The accumulated fields are merged into the span's ``agent_end``
        payload (AMP v0.3). Multiple calls within one span sum the token and
        cost numbers; for ``model`` the last non-None value wins. Outside any
        span this is a no-op that emits a warning.
        """
        usage = _current_usage.get()
        if usage is None:
            warnings.warn(
                "mesh.record_usage() called outside any @mesh.agent span; usage dropped",
                stacklevel=2,
            )
            return
        if input_tokens is not None:
            usage["input_tokens"] = usage.get("input_tokens", 0) + input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = usage.get("output_tokens", 0) + output_tokens
        if cost_usd is not None:
            usage["cost_usd"] = usage.get("cost_usd", 0.0) + cost_usd
        if model is not None:
            usage["model"] = model

    def agent(self, name: str) -> Callable[[F], F]:
        """Instrument a sync or async callable as a named agent.

        Emits ``agent_start`` before the call, ``agent_end`` (with
        ``duration_ms``) on success, and ``agent_error`` (with exception
        info) on raise — the exception is re-raised unchanged.

        Each call gets a fresh ``span_id``; nested decorated calls record
        the enclosing call's span as ``parent_span_id`` (AMP v0.2), so a
        trace tree can be rebuilt with ``agentmesh trace RUN_ID``. Usage
        recorded via ``mesh.record_usage`` inside the call is merged into
        the ``agent_end`` payload (AMP v0.3).
        """

        def decorator(fn: F) -> F:
            function_name = getattr(fn, "__qualname__", getattr(fn, "__name__", name))

            def _start(run_id: str, span: Dict[str, str]) -> float:
                payload: Dict[str, Any] = {"function": function_name}
                payload.update(span)
                self._emit(run_id, name, "agent_start", payload)
                return time.perf_counter()

            def _end(
                run_id: str, started: float, span: Dict[str, str], usage: Dict[str, Any]
            ) -> None:
                payload: Dict[str, Any] = {
                    "function": function_name,
                    "duration_ms": (time.perf_counter() - started) * 1000.0,
                }
                payload.update(usage)
                payload.update(span)
                self._emit(run_id, name, "agent_end", payload)

            def _error(
                run_id: str, started: float, exc: BaseException, span: Dict[str, str]
            ) -> None:
                payload: Dict[str, Any] = {
                    "function": function_name,
                    "duration_ms": (time.perf_counter() - started) * 1000.0,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                payload.update(span)
                self._emit(run_id, name, "agent_error", payload)

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    run_id = _current_run_id.get() or str(uuid.uuid4())
                    span = _new_span_fields()
                    usage: Dict[str, Any] = {}
                    token = _current_span_id.set(span["span_id"])
                    usage_token = _current_usage.set(usage)
                    try:
                        started = _start(run_id, span)
                        try:
                            result = await fn(*args, **kwargs)
                        except BaseException as exc:
                            _error(run_id, started, exc, span)
                            raise
                        _end(run_id, started, span, usage)
                        return result
                    finally:
                        _current_usage.reset(usage_token)
                        _current_span_id.reset(token)

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                run_id = _current_run_id.get() or str(uuid.uuid4())
                span = _new_span_fields()
                usage: Dict[str, Any] = {}
                token = _current_span_id.set(span["span_id"])
                usage_token = _current_usage.set(usage)
                try:
                    started = _start(run_id, span)
                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as exc:
                        _error(run_id, started, exc, span)
                        raise
                    _end(run_id, started, span, usage)
                    return result
                finally:
                    _current_usage.reset(usage_token)
                    _current_span_id.reset(token)

            return sync_wrapper  # type: ignore[return-value]

        return decorator


mesh = Mesh()

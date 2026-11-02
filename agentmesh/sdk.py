"""AgentMesh Python SDK: ``@mesh.agent`` decorator, ``mesh.run`` grouping,
``mesh.record_usage`` token/cost accounting, and ``mesh.handoff`` task
handoffs between agents (AMP v0.4)."""

import contextvars
import functools
import inspect
import time
import uuid
import warnings
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Type, TypeVar

from agentmesh.events import Event
from agentmesh.handoffs import HANDOFF_KIND
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


# (agent name, span fields) of the innermost running @mesh.agent call. Lets
# mesh.handoff default its sender and link itself to the enclosing span.
_current_agent: contextvars.ContextVar[Optional[Tuple[str, Dict[str, str]]]] = (
    contextvars.ContextVar("agentmesh_agent", default=None)
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

    def handoff(
        self,
        task: str,
        to: str,
        task_id: Optional[str] = None,
        from_: Optional[str] = None,
    ) -> "Handoff":
        """Record one agent handing ``task`` to agent ``to`` (AMP v0.4).

        Use as a sync or async context manager; it yields the ``task_id``::

            with mesh.handoff("Add retry to GitHub sync", to="coder") as task_id:
                coder(task)

        Emits a ``message`` event with a ``handoff`` payload and status
        ``running`` on enter, then ``done`` (with ``duration_ms``) on clean
        exit or ``failed`` (with ``duration_ms``, the first line of the error
        as ``error``, and ``error_type``) if the block raises; the exception
        is re-raised unchanged. Both events share the ``task_id`` and are
        emitted by the sender: ``from_`` if given, else the innermost running
        ``@mesh.agent`` (ValueError on enter if there is neither).

        ``task_id`` defaults to a fresh ``t-`` id. The events join the
        current ``mesh.run`` (or a fresh run_id). Inside a decorated agent
        they also carry that agent's ``span_id`` and ``parent_span_id``,
        like ``tool_call`` events; a handoff does not open a span itself.
        """
        return Handoff(self, task=task, to=to, task_id=task_id, from_=from_)

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
                    agent_token = _current_agent.set((name, span))
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
                        _current_agent.reset(agent_token)
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
                agent_token = _current_agent.set((name, span))
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
                    _current_agent.reset(agent_token)
                    _current_usage.reset(usage_token)
                    _current_span_id.reset(token)

            return sync_wrapper  # type: ignore[return-value]

        return decorator


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


class Handoff:
    """Context manager returned by :meth:`Mesh.handoff`; one use per instance."""

    def __init__(
        self,
        mesh: Mesh,
        task: str,
        to: str,
        task_id: Optional[str] = None,
        from_: Optional[str] = None,
    ) -> None:
        for label, value in (("task", task), ("to", to)):
            if not isinstance(value, str) or not value:
                raise ValueError("%s must be a non-empty string, got %r" % (label, value))
        for label, value in (("task_id", task_id), ("from_", from_)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    "%s must be a non-empty string when given, got %r" % (label, value)
                )
        self._mesh = mesh
        self.task = task
        self.to = to
        self.task_id = task_id if task_id is not None else "t-" + uuid.uuid4().hex[:12]
        self._from = from_
        self._run_id: Optional[str] = None
        self._started: Optional[float] = None
        self._payload: Dict[str, Any] = {}

    @property
    def from_(self) -> Optional[str]:
        """The sender, once resolved (on enter) or as given."""
        return self._from

    @property
    def run_id(self) -> Optional[str]:
        """The run the handoff events belong to, once entered."""
        return self._run_id

    def _open(self) -> str:
        if self._started is not None:
            raise RuntimeError("a mesh.handoff(...) context can only be entered once")
        current = _current_agent.get()
        sender = self._from if self._from is not None else (current[0] if current else None)
        if sender is None:
            raise ValueError(
                "mesh.handoff() needs from_=... when used outside any @mesh.agent call"
            )
        self._from = sender
        self._run_id = _current_run_id.get() or str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "kind": HANDOFF_KIND,
            "task_id": self.task_id,
            "task": self.task,
            "from": sender,
            "to": self.to,
        }
        if current is not None:
            payload.update(current[1])
        self._payload = payload
        self._emit({"status": "running"})
        self._started = time.perf_counter()
        return self.task_id

    def _close(self, exc: Optional[BaseException]) -> None:
        started = self._started if self._started is not None else time.perf_counter()
        extra: Dict[str, Any] = {"duration_ms": (time.perf_counter() - started) * 1000.0}
        if exc is None:
            extra["status"] = "done"
        else:
            extra["status"] = "failed"
            extra["error"] = _first_line(str(exc)) or type(exc).__name__
            extra["error_type"] = type(exc).__name__
        self._emit(extra)

    def _emit(self, extra: Dict[str, Any]) -> None:
        payload = dict(self._payload)
        payload.update(extra)
        self._mesh._emit(str(self._run_id), str(self._from), "message", payload)

    def __enter__(self) -> str:
        return self._open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        self._close(exc)
        return False

    async def __aenter__(self) -> str:
        return self._open()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        self._close(exc)
        return False


mesh = Mesh()

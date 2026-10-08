r"""Per-call record of what the LLM was actually asked, and what came back.

Three things this gets right that a hook on ``bind_tools`` does not:

* Node identity comes from langgraph's own ``langgraph_node`` metadata, not
  from pattern-matching the system prompt.
* The bound-tool list lives in a context variable set for the duration of one
  binding's invocation, not in shared module state. Shared state let a
  concurrent analyst's tools be attributed to another call, and made the
  second of two consecutive calls on one binding look tool-less.
* A structured attempt is identified by wrapping ``with_structured_output``,
  which is what the framework actually calls. Its outcome is then known
  directly: an instance came back, or it did not and the caller fell back.

Each call is one JSON line. Keys, counts and lengths only -- never the prompt,
the reply text, or the key.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import pathlib
import threading

from langchain_core.runnables import Runnable

# Set while one binding's invoke is in flight, so nested or concurrent calls
# each see their own tool list.
_CTX = contextvars.ContextVar("probe_call_ctx", default=None)

STATE = {
    "log": None,
    "seq": 0,
    "node_counts": {},
    "lock": threading.Lock(),
    "run_meta": {},
}

BODY_HEAD_CHARS = 80


# ---------------------------------------------------------------- logging


def set_log(path: pathlib.Path, meta: dict | None = None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    with STATE["lock"]:
        STATE["log"] = path
        STATE["seq"] = 0
        STATE["node_counts"] = {}
        STATE["run_meta"] = dict(meta or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / (path.stem + ".meta.json")).write_text(
        json.dumps(
            dict(meta or {}, started=_dt.datetime.now().isoformat(timespec="seconds")),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def note_report(path) -> None:
    """Bind this log to the report the run produced."""
    with STATE["lock"]:
        STATE["run_meta"]["report_dir"] = str(path)
        if STATE["log"] is not None:
            (STATE["log"].parent / (STATE["log"].stem + ".meta.json")).write_text(
                json.dumps(STATE["run_meta"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def _write(record: dict) -> None:
    with STATE["lock"]:
        log = STATE["log"]
        if log is None:
            return
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- context


def _tool_names(tools) -> list[str]:
    names = []
    for tool in tools or []:
        if isinstance(tool, dict):
            fn = tool.get("function")
            names.append(fn.get("name") if isinstance(fn, dict) else tool.get("name"))
        else:
            names.append(getattr(tool, "name", None) or getattr(tool, "__name__", None))
    return [n for n in names if n]


def _choice_text(choice) -> str:
    if choice is None:
        return "<unset>"
    if isinstance(choice, str):
        return choice
    try:
        if isinstance(choice, dict):
            fn = choice.get("function") or {}
            return "function:%s" % (fn.get("name") if isinstance(fn, dict) else choice.get("name"))
    except Exception:
        pass
    return str(choice)[:40]


class _Bound(Runnable):
    """Delegates to a binding while holding its tool list for the duration.

    Two constraints shape this. langchain returns a pydantic model from
    bind_tools, and pydantic refuses attribute assignment. And langchain
    composes structured output with ``llm | parser``, so whatever comes back
    has to be a real Runnable or coercion fails. Subclassing Runnable satisfies
    both without touching the binding.
    """

    def __init__(self, inner, info):
        super().__init__()
        self._inner = inner
        self._info = info

    def invoke(self, input, config=None, **kwargs):
        token = _CTX.set({**(_CTX.get() or {}), **self._info})
        try:
            return self._inner.invoke(input, config, **kwargs)
        finally:
            _CTX.reset(token)

    async def ainvoke(self, input, config=None, **kwargs):
        token = _CTX.set({**(_CTX.get() or {}), **self._info})
        try:
            return await self._inner.ainvoke(input, config, **kwargs)
        finally:
            _CTX.reset(token)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


class _Structured(Runnable):
    """Same for a structured-output runnable, plus the outcome is recorded."""

    def __init__(self, inner, schema_name):
        super().__init__()
        self._inner = inner
        self._schema = schema_name

    def _record(self, token, call):
        try:
            result = call()
        except Exception as exc:
            from tradingagents.agents.structured import reply_text

            _write(
                {
                    "event": "structured_result",
                    "schema": self._schema,
                    "ok": False,
                    "raw_text_retained": bool(reply_text(getattr(exc, "response", None)).strip()),
                    "error": type(exc).__name__,
                }
            )
            raise
        finally:
            _CTX.reset(token)
        self._record_result(result)
        return result

    def _record_result(self, result):
        from tradingagents.agents.structured import reply_text

        envelope = isinstance(result, dict) and {"raw", "parsed", "parsing_error"} <= result.keys()
        parsed = result.get("parsed") if envelope else result
        raw = result.get("raw") if envelope else None
        _write(
            {
                "event": "structured_result",
                "schema": self._schema,
                "ok": parsed is not None,
                "raw_text_retained": parsed is None and bool(reply_text(raw).strip()),
            }
        )

    def invoke(self, input, config=None, **kwargs):
        token = _CTX.set(
            {"tools": [], "tool_choice": "<schema>", "structured_schema": self._schema}
        )
        return self._record(token, lambda: self._inner.invoke(input, config, **kwargs))

    async def ainvoke(self, input, config=None, **kwargs):
        token = _CTX.set(
            {"tools": [], "tool_choice": "<schema>", "structured_schema": self._schema}
        )

        try:
            result = await self._inner.ainvoke(input, config, **kwargs)
        except Exception as exc:
            from tradingagents.agents.structured import reply_text

            _write(
                {
                    "event": "structured_result",
                    "schema": self._schema,
                    "ok": False,
                    "raw_text_retained": bool(reply_text(getattr(exc, "response", None)).strip()),
                    "error": type(exc).__name__,
                }
            )
            raise
        finally:
            _CTX.reset(token)
        self._record_result(result)
        return result

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


# ---------------------------------------------------------------- patching


def install(client_cls=None) -> bool:
    """Patch a chat class. Defaults to the real client; tests pass a stand-in
    so the same code path runs without a network call."""
    if client_cls is None:
        from tradingagents.llm_clients.openai_client import LocalCompatibleChatOpenAI as C
    else:
        C = client_cls

    if getattr(C, "_probe_installed", False):
        return False

    # getattr, not attribute access: the stand-in used by the offline tests
    # defines the pieces it cares about and nothing else.
    orig_generate = getattr(C, "_generate", None)
    orig_bind_tools = getattr(C, "bind_tools", None)
    orig_structured = getattr(C, "with_structured_output", None)
    if orig_generate is None:
        raise AttributeError(f"{C.__name__} has no _generate to wrap")

    # ---- bind_tools: remember what this binding carries, for its own lifetime
    # ---- bind_tools: wrap so the tool list lives for this binding's own lifetime
    def bind_tools(self, tools, **kwargs):
        binding = orig_bind_tools(self, tools, **kwargs)
        return _Bound(
            binding,
            {"tools": _tool_names(tools), "tool_choice": _choice_text(kwargs.get("tool_choice"))},
        )

    # ---- with_structured_output: the authoritative structured marker
    def with_structured_output(self, schema, **kwargs):
        runnable = orig_structured(self, schema, **kwargs)
        if runnable is None:
            return None
        name = getattr(schema, "__name__", None) or type(schema).__name__
        return _Structured(runnable, name)

    # ---- the call itself
    def generate(self, *args, **kwargs):
        ctx = _CTX.get() or {}
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        run_manager = kwargs.get("run_manager")
        meta = dict(getattr(run_manager, "metadata", None) or {})
        node = meta.get("langgraph_node")
        step = meta.get("langgraph_step")
        if node is None:
            # The run manager does not always carry the graph metadata: an agent
            # that calls its llm directly does not forward the config. The runtime
            # config is the authoritative place to ask.
            try:
                from langgraph.config import get_config as _lg_config

                cfg = _lg_config() or {}
                cmeta = dict(cfg.get("metadata") or {})
                node = cmeta.get("langgraph_node") or node
                step = cmeta.get("langgraph_step") or step
                if node is None:
                    tags = cfg.get("tags") or []
                    if tags:
                        node = "tag:" + ",".join(str(x) for x in tags)
            except Exception:
                pass

        shape = {}
        try:
            payload = self._get_request_payload(messages or [])
            shape = {
                "payload_keys": sorted(k for k in payload if k != "messages"),
                "max_tokens": payload.get("max_tokens") or payload.get("max_completion_tokens"),
                "reasoning_effort": payload.get("reasoning_effort"),
                "reasoning_split": (payload.get("extra_body") or {}).get("reasoning_split"),
                "response_format": bool(payload.get("response_format")),
            }
        except Exception as exc:
            shape = {"payload_error": type(exc).__name__}

        result = orig_generate(self, *args, **kwargs)

        with STATE["lock"]:
            STATE["seq"] += 1
            seq = STATE["seq"]
            key = node or "<no-node>"
            STATE["node_counts"][key] = STATE["node_counts"].get(key, 0) + 1
            occurrence = STATE["node_counts"][key]

        record = {
            "seq": seq,
            "node": node,
            "langgraph_step": step,
            "call_index_for_node": occurrence,
            "model": getattr(self, "model_name", None) or getattr(self, "model", None),
            "req_tools": ctx.get("tools", []),
            "req_tool_count": len(ctx.get("tools", [])),
            "req_tool_choice": ctx.get("tool_choice", "<unset>"),
            "structured_schema": ctx.get("structured_schema"),
        }
        record.update(shape)

        try:
            gens = getattr(result, "generations", None) or []
            gen = gens[0] if gens else None
            if isinstance(gen, (list, tuple)):
                gen = gen[0] if gen else None
            message = getattr(gen, "message", None) if gen is not None else None
            body = getattr(message, "content", "") if message is not None else ""
            if not isinstance(body, str):
                body = str(body)
            calls = list(getattr(message, "tool_calls", None) or []) if message is not None else []
            info = dict(getattr(gen, "generation_info", None) or {})
            rmeta = dict(getattr(message, "response_metadata", None) or {})
            record["finish_reason"] = info.get("finish_reason") or rmeta.get("finish_reason")
            record["body_chars"] = len(body)
            record["body_head"] = body[:BODY_HEAD_CHARS]
            record["tool_calls_returned"] = len(calls)
            usage = (
                rmeta.get("token_usage")
                or (getattr(result, "llm_output", None) or {}).get("token_usage")
                or {}
            )
            if usage:
                det = usage.get("completion_tokens_details") or {}
                record["usage"] = {
                    "prompt": usage.get("prompt_tokens"),
                    "completion": usage.get("completion_tokens"),
                    "reasoning": det.get("reasoning_tokens"),
                }
            blank = len(body.strip()) == 0 and not calls
            if record.get("structured_schema"):
                record["outcome"] = "empty_reply" if blank else "structured_attempt"
            else:
                record["outcome"] = (
                    "empty_reply" if blank else ("tool_call" if calls else "free_text")
                )
        except Exception as exc:
            record["probe_error"] = f"{type(exc).__name__}: {exc}"
        _write(record)
        return result

    C.bind_tools = bind_tools
    C.with_structured_output = with_structured_output
    C._generate = generate
    C._probe_installed = True
    return True


# ---------------------------------------------------------------- reading


def records(log: pathlib.Path | None = None) -> list[dict]:
    log = log or STATE["log"]
    if log is None or not pathlib.Path(log).exists():
        return []
    out = []
    for line in pathlib.Path(log).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def classify(log: pathlib.Path | None = None) -> dict:
    """The three outcomes the caller cares about, counted apart."""
    calls = [r for r in records(log) if r.get("event") != "structured_result"]
    events = [r for r in records(log) if r.get("event") == "structured_result"]
    struct_calls = [r for r in calls if r.get("structured_schema")]
    empty = [r for r in calls if r.get("outcome") == "empty_reply"]
    ok = [e for e in events if e.get("ok")]
    failed = [e for e in events if not e.get("ok")]
    return {
        "calls": calls,
        "events": events,
        "structured_call_records": len(struct_calls),
        "structured_attempts": len(events),
        "structured_ok": len(ok),
        "structured_failed": len(failed),
        "structured_raw_retained": sum(1 for event in failed if event.get("raw_text_retained")),
        "empty_replies": empty,
        "tool_calls": sum(1 for r in calls if r.get("outcome") == "tool_call"),
        "free_text": sum(1 for r in calls if r.get("outcome") == "free_text"),
    }


def struct_ok_or_fail(events, ok: bool) -> list:
    return [e for e in events if bool(e.get("ok")) is ok]

r"""Record why an agent response comes back empty.

Runs the CLI unchanged, but wraps the chat model so every LLM call is
written to data/probe/llm-calls.jsonl as one JSON object: what the request
asked for (max_tokens, reasoning_split), what came back (finish_reason, the
length of message.content, how many tool_calls), and the token usage split
into the visible and the reasoning share. Only shapes and counts are
recorded -- never the prompt, the reply text beyond a 120-char head, or the key.

A response counts as empty when content is blank AND no tool call was made.
Those are the calls worth reading: the graph then writes a zero-length report,
the rating is lost, and the memory log records nothing.

Usage from the repository root (a UTF-8 console is required on Windows):

    set PYTHONUTF8=1
    set PYTHONIOENCODING=utf-8
    .venv\Scripts\python.exe tools_probe\probe_empty_response.py \\
        --ticker AAPL --date 2026-10-05 --analysts market --save --no-show
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "probe" / "llm-calls.jsonl"


def _text_of(message) -> str:
    """content is a str here, but a list of blocks on some providers."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    return str(content or "")


def _first_generation(result):
    """_generate returns ChatResult with a FLAT list of generations."""
    gens = getattr(result, "generations", None) or []
    if not gens:
        return None
    first = gens[0]
    if isinstance(first, (list, tuple)):
        first = first[0] if first else None
    return first


def _request_shape(client, args, kwargs):
    """What the request asked for. The configured cap first, then the built
    payload; either may be absent, and neither is allowed to raise."""
    shape = {
        "req_max_tokens": getattr(client, "max_tokens", None),
        "req_reasoning_split": None,
    }
    try:
        builder = getattr(client, "_get_request_payload", None)
        if builder is not None:
            messages = kwargs.get("messages")
            if messages is None and args:
                messages = args[0]
            payload = builder(messages or [])
            shape["req_max_tokens"] = payload.get("max_tokens") or shape["req_max_tokens"]
            shape["req_reasoning_split"] = (payload.get("extra_body") or {}).get("reasoning_split")
    except Exception:
        pass
    return shape


def _usage_of(result, message):
    """Token usage, preferring the message's own metadata."""
    meta = dict(getattr(message, "response_metadata", None) or {})
    usage = meta.get("token_usage")
    if not usage:
        usage = (getattr(result, "llm_output", None) or {}).get("token_usage")
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "total": usage.get("total_tokens"),
        "reasoning": details.get("reasoning_tokens"),
    }


def install_patch():
    from tradingagents.llm_clients.openai_client import LocalCompatibleChatOpenAI

    original_generate = LocalCompatibleChatOpenAI._generate
    counter = {"n": 0}

    def patched_generate(self, *args, **kwargs):
        shape = _request_shape(self, args, kwargs)
        result = original_generate(self, *args, **kwargs)
        counter["n"] += 1

        record = {
            "seq": counter["n"],
            "model": getattr(self, "model_name", None) or getattr(self, "model", None),
        }
        record.update(shape)

        try:
            gen = _first_generation(result)
            message = getattr(gen, "message", None) if gen is not None else None
            body = _text_of(message) if message is not None else ""
            tool_calls = (
                list(getattr(message, "tool_calls", None) or []) if message is not None else []
            )
            info = dict(getattr(gen, "generation_info", None) or {})
            meta = dict(getattr(message, "response_metadata", None) or {})

            record["finish_reason"] = info.get("finish_reason") or meta.get("finish_reason")
            record["body_chars"] = len(body)
            record["tool_calls"] = len(tool_calls)
            record["empty"] = len(body.strip()) == 0 and not tool_calls
            record["body_head"] = body[:120] if body.strip() else ""
            usage = _usage_of(result, message)
            if usage:
                record["usage"] = usage
        except Exception as exc:
            record["probe_error"] = f"{type(exc).__name__}: {exc}"

        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    LocalCompatibleChatOpenAI._generate = patched_generate


def summarize():
    if not OUT.exists():
        print("probe: no records written")
        return
    records = [
        json.loads(line) for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    empty = [r for r in records if r.get("empty")]
    print()
    print("=" * 74)
    print(f"probe: {len(records):d} calls recorded, {len(empty):d} empty")
    caps = sorted({r.get("req_max_tokens") for r in records if r.get("req_max_tokens")})
    print("max_tokens seen on the wire: %s" % (caps or "not captured"))
    if empty:
        print("-" * 74)
        for r in empty:
            usage = r.get("usage") or {}
            print(
                "  seq={!s:<4} max_tokens={!s:<7} finish={!s:<9} tool_calls={!s} completion={!s} reasoning={!s}".format(
                    r.get("seq"),
                    r.get("req_max_tokens"),
                    r.get("finish_reason"),
                    r.get("tool_calls"),
                    usage.get("completion"),
                    usage.get("reasoning"),
                )
            )
        print("-" * 74)
        print("finish=length -> reasoning consumed the whole budget; raise")
        print("                 TRADINGAGENTS_MAX_TOKENS and compare.")
        print("finish=stop   -> the budget was not the cause; the response path")
        print("                 itself needs looking at.")
    print("=" * 74)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("", encoding="utf-8")

    install_patch()

    from cli.main import app

    code = 0
    try:
        app(sys.argv[1:])
    except SystemExit as exc:
        code = exc.code or 0
    except Exception as exc:
        print(f"run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    finally:
        summarize()
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())

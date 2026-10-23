"""OpenAI tool-calling bridge for text-only upstream models.

Ported from 9router's ``open-sse/translator/concerns/toolBridge.js`` and
``open-sse/executors/webCookieToolBridge.js``. The upstream chat model has no
native function-calling schema, so:

1. the request's ``tools`` are rendered into the prompt,
2. assistant tool calls and tool results are rendered into the transcript,
3. tool-call markers the model writes in prose are parsed back into real
   ``tool_calls`` with declared arguments filtered to the client's schema.

Nothing is invented: a call is only emitted when the model wrote one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

MARKER = "<<TOOL_CALL>>"
RESULT_MARKER = "<<TOOL_RESULT>>"
FENCE_OPEN = "```tool_call"
FENCE_CLOSE = "```"

_MARKER_RE = re.compile(r"<<TOOL_CALL>>\s*(\{.*?\})\s*<<TOOL_CALL>>", re.S)
_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S | re.I)
_XML_TOOL_RE = re.compile(
    r"<tool_use\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</tool_use>", re.S | re.I
)
_OPENCODE_TOOL_RE = re.compile(
    r"(?:^|\n)\s*(?:assistant\s+)?to\s*=\s*([A-Za-z_][\w.-]*)\s*(\{.*?\})\s*(?:\n|$)",
    re.S,
)
_FENCED_TOOL_RE = re.compile(
    r"```(?:json|tool_call|tool)?\s*(\{[^{}]*?\"(?:tool_)?(?:name|tool|call)\".*?\})\s*```",
    re.S | re.I,
)
_MISTRAL_TOOL_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*?\])", re.S)
_INVOKE_XML_RE = re.compile(
    r"<invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", re.S | re.I
)
_ANTML_INVOKE_RE = re.compile(
    r"<antml:invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</antml:invoke>",
    re.S | re.I,
)
_INVOKE_PARAM_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", re.S | re.I
)
_FUNCTION_TAG_RE = re.compile(
    r"<function\s*=\s*[\"']?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</function>", re.S | re.I
)
_BARE_OBJECT_RE = re.compile(
    r"(?:^|\n)\s*(\{\s*\"function\"\s*:\s*\"[^\"]+\".*?\})\s*(?:\n|$)", re.S
)
_BARE_ARRAY_RE = re.compile(r"\"tool_calls\"\s*:\s*\[(.*?)\]\s*(?=,\s*\"|\}|\n|$)", re.S)

_id_counter = 0


def parse_tool_choice(raw: Any) -> tuple[str, str]:
    """Return (mode, forced_name); mode is none | auto | required."""
    if raw is None:
        return "auto", ""
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value == "none":
            return "none", ""
        if value in {"required", "any"}:
            return "required", ""
        return "auto", ""
    if isinstance(raw, dict):
        name = raw.get("function", {}).get("name") if isinstance(raw.get("function"), dict) else raw.get("name")
        if isinstance(name, str) and name.strip():
            return "required", name.strip()
    return "auto", ""


def _fn_of(tool: Any) -> dict:
    if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
        return tool["function"]
    return tool if isinstance(tool, dict) else {}


def has_tools(body: dict) -> bool:
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        return True
    functions = body.get("functions")
    return isinstance(functions, list) and bool(functions)


def selectable_tools(body: dict) -> tuple[list[dict], str, str]:
    raw = body.get("tools")
    if not isinstance(raw, list) or not raw:
        raw = body.get("functions") if isinstance(body.get("functions"), list) else []
    mode, forced = parse_tool_choice(body.get("tool_choice", body.get("toolChoice")))
    defs: list[dict] = []
    for tool in raw:
        fn = _fn_of(tool)
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if forced and name != forced:
            continue
        parameters = fn.get("parameters") or fn.get("input_schema")
        defs.append({
            "name": name.strip(),
            "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
            "parameters": parameters if isinstance(parameters, dict)
            else {"type": "object", "properties": {}},
        })
    return defs, mode, forced


def tools_active(body: dict) -> bool:
    if not has_tools(body):
        return False
    _defs, mode, _forced = selectable_tools(body)
    return mode != "none"


def _instruction_line(mode: str, forced: str) -> str:
    if forced:
        return (
            f'For this turn you MUST output a tool-call JSON block for "{forced}". Reply '
            "with exactly one block and nothing else - do not answer the question yourself, "
            "even if you know the answer."
        )
    if mode == "required":
        return (
            "For this turn you MUST output a tool-call JSON block for one of the tools above. "
            "Reply with exactly one block and nothing else - do not answer the question "
            "yourself, even if you know the answer."
        )
    return "Only use tool-call blocks when a tool is actually needed for the task."


def build_tool_instructions(body: dict) -> str:
    defs, mode, forced = selectable_tools(body)
    if not defs:
        return ""
    # Keep the framing short and matter-of-fact. The upstream model honestly
    # refuses elaborate "system tool protocol" prose, but it will write tool
    # invocation blocks when the instruction is a plain data-format request.
    return "\n".join([
        "To ask the calling program to run a tool, the reply is a single JSON block like:",
        "",
        f"    {MARKER} {{\"name\": \"fn_name\", \"arguments\": {{\"arg\": \"value\"}}}} {MARKER}",
        "",
        _instruction_line(mode, forced),
        "",
        "Available tools for the calling program:",
        json.dumps(defs, ensure_ascii=False, indent=2),
        "",
        "Do not pretend to run a tool; just emit the block and stop the turn.",
    ])
def build_tool_reminder() -> str:
    example = f'{MARKER} {{"name": "run_command", "arguments": {{"command": "ls -la"}}}} {MARKER}'
    return "\n".join([
        "<muse2api tool-exchange protocol reminder>",
        "- tool_call blocks (or ```json blocks) are requests, not execution claims. Emit",
        "  the next tool request when you need one; the wrapper program executes it.",
        "- Stay honest: never claim you actually ran a tool; you only wrote the request.",
        "- Once you get a tool result that shows success, do not retry it and return a",
        "  final answer once the task is done.",
        "- Ask for the simplest tool; do not bundle many in one reply beyond what the protocol allows.",
        "",
        f"    {example}",
        "Keep the reply to one block if a tool is needed; never drop the block.",
    ])

def _parse_maybe_json(text: Any) -> Any:
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = [
        text,
        text.replace("None", "null").replace("True", "true").replace("False", "false")
        .replace("'", '"'),
    ]
    for raw in candidates:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _normalise_args(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    parsed = _parse_maybe_json(str(raw))
    return parsed if isinstance(parsed, dict) else {}


def _args_from_invoke(inner: str) -> dict:
    return {
        match.group(1).strip(): match.group(2).strip()
        for match in _INVOKE_PARAM_RE.finditer(inner)
    }


def _name_from(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    direct = payload.get("name") or payload.get("tool") or payload.get("tool_name") or payload.get("function")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, dict) and isinstance(direct.get("name"), str):
        return direct["name"].strip()
    nested = payload.get("function_call")
    if isinstance(nested, dict) and isinstance(nested.get("name"), str):
        return nested["name"].strip()
    return None


def _args_from(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    for key in ("arguments", "parameters", "input", "args"):
        if payload.get(key) is not None:
            return _normalise_args(payload[key])
    fn = payload.get("function")
    if isinstance(fn, dict):
        return _normalise_args(fn.get("arguments"))
    reserved = {"name", "tool", "tool_name", "type", "id", "call", "function", "function_call"}
    rest = {key: value for key, value in payload.items() if key not in reserved}
    return rest


def extract_tool_calls_from_text(text: str) -> tuple[list[dict], str]:
    """Return (calls, cleaned_text); calls are ``{name, arguments}`` dicts."""
    if not isinstance(text, str) or not text:
        return [], text
    calls: list[dict] = []
    cleaned = text

    def harvest(match: str, name_hint: str | None, args_raw: str) -> None:
        nonlocal cleaned
        payload = _parse_maybe_json(args_raw) or {}
        name = _name_from(payload) or name_hint
        if not name:
            return
        calls.append({"name": name, "arguments": _args_from(payload)})
        cleaned = cleaned.replace(match, "")

    for match in list(_MARKER_RE.finditer(text)):
        harvest(match.group(0), None, match.group(1))
    if not calls:
        for match in _TOOL_TAG_RE.finditer(text):
            harvest(match.group(0), None, match.group(1))
    if not calls:
        for match in _XML_TOOL_RE.finditer(text):
            harvest(match.group(0), match.group(1), match.group(2))
    if not calls:
        for match in _OPENCODE_TOOL_RE.finditer(text):
            harvest(match.group(0), match.group(1), match.group(2))
    if not calls:
        for match in _FENCED_TOOL_RE.finditer(text):
            harvest(match.group(0), None, match.group(1))
    if not calls:
        for match in _MISTRAL_TOOL_RE.finditer(text):
            items = _parse_maybe_json(match.group(1))
            if not isinstance(items, list):
                continue
            took = 0
            for item in items:
                name = _name_from(item)
                if not name:
                    continue
                calls.append({"name": name, "arguments": _args_from(item)})
                took += 1
            if took:
                cleaned = cleaned.replace(match.group(0), "")
    if not calls:
        for regex in (_INVOKE_XML_RE, _ANTML_INVOKE_RE):
            for match in regex.finditer(text):
                name = match.group(1).strip()
                args = _args_from_invoke(match.group(2))
                if name and args:
                    calls.append({"name": name, "arguments": args})
                    cleaned = cleaned.replace(match.group(0), "")
            if calls:
                break
    if not calls:
        for match in _FUNCTION_TAG_RE.finditer(text):
            payload = _parse_maybe_json(match.group(2)) or {}
            calls.append({"name": match.group(1).strip(), "arguments": _args_from(payload)})
            cleaned = cleaned.replace(match.group(0), "")
    if not calls:
        for match in _BARE_OBJECT_RE.finditer(text):
            payload = _parse_maybe_json(match.group(1))
            name = _name_from(payload)
            if not name:
                continue
            calls.append({"name": name, "arguments": _args_from(payload)})
            cleaned = cleaned.replace(match.group(0), "")
    if not calls:
        for match in list(_FENCED_TOOL_RE.finditer(text)):
            payload=_parse_maybe_json(match.group(1))
            if isinstance(payload,list):
                for entry in payload:
                    name=_name_from(entry)
                    if not name: continue
                    calls.append({"name":name,"arguments":_args_from(entry)})
                cleaned = cleaned.replace(match.group(0), "")
                break
            else:
                payload_dict=_parse_maybe_json(match.group(1))
                name=_name_from(payload_dict)
                if name:
                    calls.append({"name":name,"arguments":_args_from(payload_dict)})
                    cleaned = cleaned.replace(match.group(0), "")
                    break
    if not calls:
        # Bare JSON object/array in prose (tokenizer pattern from the reference
        # implementation): ```tool_name``` blocks where the contents is a JSON
        # object with "name" / "tool" keys.
        for match in list(re.finditer(r"(?:^|\\n)\\s*([\\{\\[][^\\n]*(?:\\n[^\\n]*)*)", text)):
            start=match.start(1)
            # Try to balance from start until valid JSON of the same bracket kind.
            open_ch=text[start]; close_ch='}' if open_ch=='{' else ']'
            depth=0
            end=None
            for idx in range(start, min(start+5000, len(text))):
                ch=text[idx]
                if ch==open_ch: depth+=1
                elif ch==close_ch: depth-=1
                if depth==0:
                    end=idx+1; break
            snippet=text[start:end]
            if snippet and snippet.lstrip()[0] in "{[":
                parsed=_parse_maybe_json(snippet)
                entries=[parsed] if isinstance(parsed,dict) else parsed if isinstance(parsed,list) else []
                took=0
                for entry in entries:
                    name=_name_from(entry)
                    if not name: continue
                    calls.append({"name":name,"arguments":_args_from(entry)})
                    took+=1
                if took:
                    cleaned = cleaned.replace(text[max(0,start-4):end], "")
                    break
    if not calls:
        for match in _BARE_ARRAY_RE.finditer(text):
            items = _parse_maybe_json("[" + match.group(1) + "]")
            if not isinstance(items, list):
                continue
            took = 0
            for item in items:
                name = _name_from(item)
                if not name:
                    continue
                calls.append({"name": name, "arguments": _args_from(item)})
                took += 1
            if took:
                cleaned = cleaned.replace(match.group(0), "")

    return calls, cleaned.strip()


def _declared_arg_names(schema: Any, depth: int = 0) -> set[str]:
    names: set[str] = set()
    if not isinstance(schema, dict) or depth > 6:
        return names
    properties = schema.get("properties")
    if isinstance(properties, dict):
        names.update(str(key) for key in properties)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        child = schema.get(key)
        if isinstance(child, list):
            for entry in child:
                names.update(_declared_arg_names(entry, depth + 1))
        else:
            names.update(_declared_arg_names(child, depth + 1))
    return names


def _unique_id(seed: str) -> str:
    global _id_counter
    _id_counter += 1
    return f"call_{seed or 'otb'}{_id_counter:x}"


def apply_tool_bridge(content: str, body: dict) -> dict:
    """Repair a text reply into OpenAI ``tool_calls``; never invents calls."""
    diagnostics = {"applied": False, "reason": None}
    if not isinstance(content, str):
        return {**diagnostics, "reason": "no-content", "content": content, "tool_calls": []}
    if not has_tools(body):
        return {**diagnostics, "reason": "no-tools-in-request", "content": content, "tool_calls": []}
    if not tools_active(body):
        return {**diagnostics, "reason": "tool-choice-none", "content": content, "tool_calls": []}
    catalogue = {
        item["name"]: item.get("parameters") or {}
        for item in selectable_tools(body)[0]
    }
    calls, cleaned = extract_tool_calls_from_text(content)
    if not calls:
        return {**diagnostics, "reason": "no-tool-marker-in-prose",
                "content": content, "tool_calls": []}

    def norm(value: str) -> str:
        return re.sub(r"[-_]+", "_", value.lower())

    def resolve(name: str) -> str | None:
        if not catalogue:
            return name
        if name in catalogue:
            return name
        target = norm(name)
        for declared in catalogue:
            if norm(declared) == target:
                return declared
        return None

    accepted: list[tuple[str, dict]] = []
    for call in calls:
        declared_name = resolve(call["name"])
        if declared_name:
            accepted.append((declared_name, call["arguments"]))
    if not accepted:
        return {**diagnostics, "reason": "no-called-tool-was-offered",
                "content": content, "tool_calls": []}

    tool_calls = []
    for name, arguments in accepted:
        declared = _declared_arg_names(catalogue.get(name) or {})
        args = arguments
        if declared:
            filtered = {key: value for key, value in args.items() if key in declared}
            if filtered:
                args = filtered
        tool_calls.append({
            "id": _unique_id(name),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return {
        "applied": True,
        "reason": None,
        "content": cleaned,
        "tool_calls": tool_calls,
    }


def render_tool_call(name: str, arguments: Any) -> str:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return f'{MARKER} {{"name": {json.dumps(name)}, "arguments": {arguments}}} {MARKER}'


def render_tool_transcript(messages: Iterable[dict]) -> list[str]:
    """Render assistant tool calls and tool results as upstream-readable lines."""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            text = _content_text(message.get("content"))
            calls = []
            for call in message["tool_calls"]:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    calls.append(render_tool_call(fn["name"], fn.get("arguments")))
            if calls:
                prefix = f"{text}\n" if text else ""
                lines.append(f"assistant: {prefix}" + "\n".join(calls))
        elif role == "tool":
            text = _content_text(message.get("content"))
            if text:
                name = message.get("name") or message.get("tool_call_id") or "tool"
                lines.append(f"tool: {RESULT_MARKER} {name}: {text} {RESULT_MARKER}")
    return lines


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""

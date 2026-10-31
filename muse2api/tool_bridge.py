"""OpenAI tool-calling bridge for text-only upstream models.

Ported from and aligned with OmniRoute's webTools architecture
(https://github.com/diegosouzapw/OmniRoute - open-sse/translator/webTools.ts & deepseekWebTools.ts).
The upstream muse.ai web model has no native function-calling schema, so:

1. the request's ``tools`` are serialized into a prompt contract (<tool>{json}</tool> & ```json),
2. multi-turn conversation trajectories (assistant tool calls and role: tool results) are replayed
   to prevent model amnesia and loops,
3. tool-call tags/markers emitted by the model in prose are tokenized, paired with a stack,
   filtered to leaf blocks, name-resolved with Levenshtein fuzzy matching, and parsed back
   into valid OpenAI ``tool_calls`` with declared arguments filtered to the client's schema.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MARKER = "<<TOOL_CALL>>"
RESULT_MARKER = "<<TOOL_RESULT>>"
FENCE_OPEN = "```tool_call"
FENCE_CLOSE = "```"

# Legacy & fallback markers
_MARKER_RE = re.compile(r"<<TOOL_CALL>>\s*(\{.*?\})\s*<<TOOL_CALL>>", re.DOTALL)
_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_XML_TOOL_RE = re.compile(
    r"<tool_use\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</tool_use>", re.DOTALL | re.IGNORECASE
)
_OPENCODE_TOOL_RE = re.compile(
    r"(?:^|\n)\s*(?:assistant\s+)?to\s*=\s*([A-Za-z_][\w.-]*)\s*(\{.*?\})\s*(?:\n|$)",
    re.DOTALL,
)
_FENCED_TOOL_RE = re.compile(
    r"```(?:json|tool_call|tool)?\s*(\{[^{}]*?\"(?:tool_)?(?:name|tool|call)\".*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_MISTRAL_TOOL_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*?\])", re.DOTALL)
_INVOKE_XML_RE = re.compile(
    r"<invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</invoke>", re.DOTALL | re.IGNORECASE
)
_ANTML_INVOKE_RE = re.compile(
    r"<antml:invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</antml:invoke>",
    re.DOTALL | re.IGNORECASE,
)
_INVOKE_PARAM_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", re.DOTALL | re.IGNORECASE
)
_FUNCTION_TAG_RE = re.compile(
    r"<function\s*=\s*[\"']?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</function>", re.DOTALL | re.IGNORECASE
)
_BARE_OBJECT_RE = re.compile(
    r"(?:^|\n)\s*(\{\s*\"(?:name|tool|function)\"\s*:\s*\"[^\"]+\".*?\})\s*(?:\n|$)", re.DOTALL
)
_BARE_ARRAY_RE = re.compile(r"\"tool_calls\"\s*:\s*\[(.*?)\]\s*(?=,\s*\"|\}|\n|$)", re.DOTALL)

# OmniRoute Tokenizer Regexes
TAG_TOKEN_RE = re.compile(
    r"<(\/?)(?:tool_call|tool)(:[A-Za-z0-9_.+-]+)?((?:\s[^>]*)?)\/?>",
    re.IGNORECASE,
)
PARAM_TAG_RE = re.compile(
    r"<parameter\b([^>]*?)\/?>(?:((?:(?!<parameter\b)[\s\S])*?)<\/parameter>)?",
    re.IGNORECASE,
)

_id_counter = 0


# ── OmniRoute Fuzzy Tool Name Resolution ─────────────────────────────────────

def levenshtein_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between strings a and b."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        curr[0] = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = list(curr)
    return prev[len(b)]


def normalize_tool_name(name: str) -> str:
    """Normalize a tool name for fuzzy comparison (lowercase alphanumeric)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def score_tool_name(emitted: str, candidate: str) -> float:
    """Score similarity between an emitted tool name and an offered candidate (0.0 to 1.0)."""
    if emitted == candidate:
        return 1.0
    norm_e = normalize_tool_name(emitted)
    norm_c = normalize_tool_name(candidate)
    if not norm_e or not norm_c:
        return 0.0
    if norm_e == norm_c:
        return 0.98
    shorter = min(len(norm_e), len(norm_c))
    longer = max(len(norm_e), len(norm_c))
    if shorter >= 4 and (norm_e in norm_c or norm_c in norm_e):
        return 0.86 - (longer - shorter) / max(longer, 1) / 4.0
    dist = levenshtein_distance(norm_e, norm_c)
    sim = 1.0 - dist / max(longer, 1)
    return sim if sim >= 0.72 else 0.0


def resolve_requested_tool_name(emitted: str, candidates: Iterable[str]) -> str | None:
    """Fuzzy-resolve an emitted tool name to one of the offered candidates using OmniRoute rules."""
    cand_list = list(candidates)
    if not cand_list:
        return emitted
    best_name: str | None = None
    best_score = 0.0
    second_best = 0.0
    for cand in cand_list:
        score = score_tool_name(emitted, cand)
        if score > best_score:
            second_best = best_score
            best_score = score
            best_name = cand
        elif score > second_best:
            second_best = score
    if not best_name or best_score < 0.72:
        return None
    # Avoid ambiguous corrections when two candidates are nearly equally close
    if best_score < 0.98 and (best_score - second_best) < 0.08:
        return None
    return best_name


# ── OmniRoute Loose JSON Parsing & Normalization ──────────────────────────────

def strip_code_fence(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^```(?:json|tool_call|tool|javascript|js|python)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE)
    return text.strip()


def normalize_loose_json(value: str) -> str:
    """Repair loose JSON emitted by models (single quotes, Python booleans, unquoted keys)."""
    s = value
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)', r'\1"\2"\3', s)
    s = re.sub(r',\s*([}\]])', r'\1', s)
    return s


def _parse_maybe_json(text: Any) -> Any:
    """Parse JSON or Python dict literals with multi-tier lenient repair."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = strip_code_fence(text)
    for candidate in [cleaned, normalize_loose_json(cleaned)]:
        try:
            return json.loads(candidate)
        except Exception:
            pass
        try:
            sq_fixed = re.sub(r"(?<!\\)'", '"', candidate)
            return json.loads(sq_fixed)
        except Exception:
            pass
    try:
        val = ast.literal_eval(cleaned)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass
    return None


# ── OmniRoute Tag Tokenizer & Leaf-Block Parser ───────────────────────────────

@dataclass(slots=True)
class TagToken:
    start: int
    end: int
    closing: bool
    suffix: str
    attrs: str


@dataclass(slots=True)
class ToolBlock:
    open_tag: TagToken
    close_tag: TagToken
    inner_start: int
    inner_end: int


def tokenize_tool_tags(text: str) -> list[TagToken]:
    tokens: list[TagToken] = []
    for m in TAG_TOKEN_RE.finditer(text):
        tokens.append(
            TagToken(
                start=m.start(),
                end=m.end(),
                closing=(m.group(1) == "/"),
                suffix=m.group(2)[1:] if m.group(2) else "",
                attrs=m.group(3) or "",
            )
        )
    return tokens


def pair_tool_blocks(tokens: list[TagToken], text_len: int) -> list[ToolBlock]:
    """Pair tags with a stack. Stray opens get a synthetic close at end of text."""
    blocks: list[ToolBlock] = []
    stack: list[TagToken] = []
    for tok in tokens:
        if not tok.closing:
            stack.append(tok)
        else:
            if stack:
                open_tok = stack.pop()
                blocks.append(
                    ToolBlock(
                        open_tag=open_tok,
                        close_tag=tok,
                        inner_start=open_tok.end,
                        inner_end=tok.start,
                    )
                )
    for open_tok in stack:
        syn = TagToken(text_len, text_len, True, "", "")
        blocks.append(
            ToolBlock(
                open_tag=open_tok,
                close_tag=syn,
                inner_start=open_tok.end,
                inner_end=text_len,
            )
        )
    return blocks


def get_attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*(["\'])(.*?)\1', attrs, re.IGNORECASE | re.DOTALL)
    return m.group(2) if m else None


def get_xml_child(inner: str, tag: str) -> str | None:
    m = re.search(rf'<{tag}\b[^>]*>([\s\S]*?)<\/{tag}>', inner, re.IGNORECASE)
    return m.group(1).strip() if m else None


def build_args_from_parameters(inner: str) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    found = False
    for m in PARAM_TAG_RE.finditer(inner):
        attrs = m.group(1) or ""
        body = m.group(2)
        name = get_attr(attrs, "name")
        if not name:
            continue
        val = get_attr(attrs, "content")
        if val is None:
            val = body.strip() if body is not None else ""
        out[name] = val
        found = True
    return out if found else None


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
        return f'Please call the function "{forced}" using a ```json block or <tool> block.'
    if mode == "required":
        return "Please call one of the functions above using a ```json block or <tool> block."
    return "If a function is needed, write a ```json block or <tool> block. If not, answer normally in plain text."


def build_tool_instructions(body: dict) -> str:
    """Build system-prompt instructions for tool calling following OmniRoute's contract."""
    defs, mode, forced = selectable_tools(body)
    if not defs or mode == "none":
        return ""

    tool_lines = []
    for t in defs:
        desc = f": {t['description']}" if t.get("description") else ""
        params = json.dumps(t.get("parameters", {}), ensure_ascii=False)
        tool_lines.append(f"- {t['name']}{desc}\n  parameters: {params}")

    contract_lines = [
        "You can call tools. To call a tool, output ONLY this exact block (no markdown fence):",
        '<tool>{"name": "<tool_name>", "arguments": { ... }}</tool>',
        "Rules:",
        "- Use exactly <tool>...</tool>. Do NOT use <tool:name>, <tool_call>, <name>, <parameter>, id=/name= attributes, or code fences.",
        '- "name" must be one of the tools below; "arguments" must be a JSON object.',
        "- When a tool is needed, emit the <tool> block instead of only describing the plan.",
        "- Emit one <tool> block per call; you may put several blocks back to back.",
        "- If no tool is needed, just answer normally without any <tool> block.",
        "",
        "Available functions:",
        "```json",
        json.dumps(defs, ensure_ascii=False, indent=2),
        "```",
        "",
        "Available tools:",
        *tool_lines,
        "",
        "To call a function, respond with a JSON block:",
        "```json",
        '{"name": "function_name", "arguments": {"param": "value"}}',
        "```",
        "",
        _instruction_line(mode, forced),
    ]
    return "\n".join(contract_lines)


def build_tool_reminder(body: dict | None = None) -> str:
    return (
        "(Note: If you need to call a function, output the ```json block or <tool> block. "
        "If you already have the result or don't need a tool, answer normally.)"
    )


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


def extract_tool_calls_from_text(
    text: str,
    requested_tools: Iterable[str] | None = None,
    schema_map: dict[str, set[str]] | None = None,
) -> tuple[list[dict], str]:
    """Return (calls, cleaned_text); calls are ``{name, arguments}`` dicts.

    Uses OmniRoute's tokenized stack parser for <tool> and <tool_call> tags,
    with leaf block extraction, fuzzy name matching, and nameless schema fallback.
    Falls back to legacy regexes if no tool tags are present.
    """
    if not isinstance(text, str) or not text:
        return [], text

    requested = list(requested_tools) if requested_tools is not None else []
    calls: list[dict] = []
    cleaned = text

    # Step 1: OmniRoute Tag Tokenizer & Leaf-Block Parser
    tokens = tokenize_tool_tags(text)
    if tokens:
        blocks = pair_tool_blocks(tokens, len(text))
        # Filter to leaf blocks (no other block nested inside)
        leaf_blocks = [
            b for b in blocks
            if not any(
                o != b and o.open_tag.start >= b.inner_start and o.close_tag.end <= b.inner_end
                for o in blocks
            )
        ]
        leaf_blocks.sort(key=lambda b: b.open_tag.start)

        accepted_ranges: list[tuple[int, int]] = []
        for block in leaf_blocks:
            tag_name = (
                block.open_tag.suffix
                or get_attr(block.open_tag.attrs, "name")
                or get_attr(block.open_tag.attrs, "id")
                or ""
            )
            inner = text[block.inner_start:block.inner_end].strip()

            name_child = get_xml_child(inner, "name")
            args_child = get_xml_child(inner, "arguments") or get_xml_child(inner, "parameters")
            param_obj = None if args_child else build_args_from_parameters(inner)
            has_xml_children = bool(name_child or args_child or param_obj)

            json_body = None if has_xml_children else _parse_maybe_json(inner)
            json_name = None
            if isinstance(json_body, dict):
                json_name = json_body.get("name") or json_body.get("type")
                if not isinstance(json_name, str):
                    json_name = None

            child_resolved = resolve_requested_tool_name(name_child, requested) if name_child else None
            json_resolved = resolve_requested_tool_name(json_name, requested) if json_name else None
            tag_resolved = resolve_requested_tool_name(tag_name, requested) if tag_name else None

            # Pick best resolved tool name
            name: str | None = None
            name_from_tag = False
            for val, from_tag in [
                (child_resolved, False),
                (json_resolved, False),
                (tag_resolved, True),
                (name_child, False),
                (json_name, False),
                (tag_name, True),
            ]:
                if not name and val:
                    name = val
                    name_from_tag = from_tag

            # Shell style {"command": "..."} fallback
            if not name and not tag_name and isinstance(json_body, dict):
                cmd = json_body.get("command")
                if isinstance(cmd, str):
                    resolved_cmd = resolve_requested_tool_name(cmd, requested)
                    if resolved_cmd:
                        name = resolved_cmd
                        name_from_tag = False

            # Nameless-block schema fallback (#5154)
            if not name and param_obj and schema_map:
                param_keys = set(param_obj.keys())
                if param_keys:
                    candidates = [
                        tool_name for tool_name, schema_keys in schema_map.items()
                        if schema_keys and param_keys.issubset(schema_keys)
                    ]
                    if len(candidates) == 1:
                        name = candidates[0]
                        name_from_tag = False

            if not name:
                continue

            args_value: Any = {}
            if args_child:
                args_value = _parse_maybe_json(args_child) or args_child
            elif param_obj is not None:
                args_value = param_obj
            elif isinstance(json_body, dict):
                if json_body.get("arguments") is not None:
                    args_value = json_body["arguments"]
                elif json_body.get("params") is not None:
                    args_value = json_body["params"]
                elif name_from_tag:
                    args_value = json_body
                else:
                    reserved = {"name", "type", "id", "command", "arguments", "params"}
                    args_value = {k: v for k, v in json_body.items() if k not in reserved}

            args_dict = _normalise_args(args_value)
            calls.append({"name": name, "arguments": args_dict})
            accepted_ranges.append((block.open_tag.start, block.close_tag.end))

        if calls:
            # Strip accepted blocks and any leftover tokens
            mask = list(cleaned)
            for start, end in accepted_ranges:
                for idx in range(start, min(end, len(mask))):
                    mask[idx] = ""
            for tok in tokens:
                if not any(start <= tok.start and tok.end <= end for start, end in accepted_ranges):
                    for idx in range(tok.start, min(tok.end, len(mask))):
                        mask[idx] = ""
            cleaned = "".join(mask).strip()
            return calls, cleaned

    # Step 2: Fallback regex cascade for legacy markers
    def harvest(match: str, name_hint: str | None, args_raw: str) -> None:
        nonlocal cleaned
        payload = _parse_maybe_json(args_raw) or {}
        name = _name_from(payload) or name_hint
        if not name:
            return
        if requested:
            resolved = resolve_requested_tool_name(name, requested)
            if resolved:
                name = resolved
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
                if requested:
                    resolved = resolve_requested_tool_name(name, requested)
                    if resolved:
                        name = resolved
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
                    if requested:
                        resolved = resolve_requested_tool_name(name, requested)
                        if resolved:
                            name = resolved
                    calls.append({"name": name, "arguments": args})
                    cleaned = cleaned.replace(match.group(0), "")
            if calls:
                break
    if not calls:
        for match in _FUNCTION_TAG_RE.finditer(text):
            payload = _parse_maybe_json(match.group(2)) or {}
            name = match.group(1).strip()
            if requested:
                resolved = resolve_requested_tool_name(name, requested)
                if resolved:
                    name = resolved
            calls.append({"name": name, "arguments": _args_from(payload)})
            cleaned = cleaned.replace(match.group(0), "")
    if not calls:
        for match in _BARE_OBJECT_RE.finditer(text):
            payload = _parse_maybe_json(match.group(1))
            name = _name_from(payload)
            if not name:
                continue
            if requested:
                resolved = resolve_requested_tool_name(name, requested)
                if resolved:
                    name = resolved
            calls.append({"name": name, "arguments": _args_from(payload)})
            cleaned = cleaned.replace(match.group(0), "")
    if not calls:
        for match in list(_FENCED_TOOL_RE.finditer(text)):
            payload = _parse_maybe_json(match.group(1))
            if isinstance(payload, list):
                for entry in payload:
                    name = _name_from(entry)
                    if not name:
                        continue
                    if requested:
                        resolved = resolve_requested_tool_name(name, requested)
                        if resolved:
                            name = resolved
                    calls.append({"name": name, "arguments": _args_from(entry)})
                cleaned = cleaned.replace(match.group(0), "")
                break
            name = _name_from(payload)
            if name:
                if requested:
                    resolved = resolve_requested_tool_name(name, requested)
                    if resolved:
                        name = resolved
                calls.append({"name": name, "arguments": _args_from(payload)})
                cleaned = cleaned.replace(match.group(0), "")
                break
    if not calls:
        for match in list(re.finditer(r"(?:^|\n)\s*([\{\[][^\n]*(?:\n[^\n]*)*)", text)):
            start = match.start(1)
            open_ch = text[start]
            close_ch = "}" if open_ch == "{" else "]"
            depth = 0
            end = None
            for idx in range(start, min(start + 5000, len(text))):
                ch = text[idx]
                if ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                if depth == 0:
                    end = idx + 1
                    break
            snippet = text[start:end]
            if snippet and snippet.lstrip()[0] in "{[":
                parsed = _parse_maybe_json(snippet)
                entries = [parsed] if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
                took = 0
                for entry in entries:
                    name = _name_from(entry)
                    if not name:
                        continue
                    if requested:
                        resolved = resolve_requested_tool_name(name, requested)
                        if resolved:
                            name = resolved
                    calls.append({"name": name, "arguments": _args_from(entry)})
                    took += 1
                if took:
                    cleaned = cleaned.replace(text[max(0, start - 4):end], "")
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
                if requested:
                    resolved = resolve_requested_tool_name(name, requested)
                    if resolved:
                        name = resolved
                calls.append({"name": name, "arguments": _args_from(item)})
                took += 1
            if took:
                cleaned = cleaned.replace(match.group(0), "")

    return calls, cleaned.strip()


def _declared_arg_names(schema: Any, depth: int = 0) -> set[str]:
    names: set[str] = set()
    if depth > 4 or not isinstance(schema, dict):
        return names
    props = schema.get("properties")
    if isinstance(props, dict):
        names.update(str(k) for k in props)
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
    schema_map = {name: _declared_arg_names(params) for name, params in catalogue.items()}

    calls, cleaned = extract_tool_calls_from_text(
        content,
        requested_tools=list(catalogue.keys()),
        schema_map=schema_map,
    )
    if not calls:
        return {**diagnostics, "reason": "no-tool-marker-in-prose",
                "content": content, "tool_calls": []}

    accepted: list[tuple[str, dict]] = []
    for call in calls:
        declared_name = resolve_requested_tool_name(call["name"], list(catalogue.keys()))
        if declared_name:
            accepted.append((declared_name, call["arguments"]))
    if not accepted:
        return {**diagnostics, "reason": "no-called-tool-was-offered",
                "content": content, "tool_calls": []}
    if body.get("parallel_tool_calls") is False:
        accepted = accepted[:1]

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
    return f"```json\n{{\"name\": {json.dumps(name)}, \"arguments\": {arguments}}}\n```"


def build_tool_conversation_prompt(messages: Iterable[dict], tool_instructions: str = "") -> str:
    """Build a conversation prompt replaying prior turns and tool results (OmniRoute pattern).

    Prevents model amnesia in autonomous multi-turn loops (Hermes, OpenCode, Cline).
    """
    system_parts: list[str] = []
    if tool_instructions:
        system_parts.append(tool_instructions)

    lines: list[str] = []
    call_name_by_id: dict[str, str] = {}
    saw_tool_activity = False

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = _content_text(m.get("content")).strip()

        if role == "system":
            if content:
                system_parts.append(content)
        elif role == "user":
            if content:
                lines.append(f"User: {content}")
        elif role == "assistant":
            parts: list[str] = []
            if content:
                parts.append(content)
            tool_calls = m.get("tool_calls")
            if not isinstance(tool_calls, list) and isinstance(m.get("function_call"), dict):
                fn = m["function_call"]
                if isinstance(fn.get("name"), str):
                    tool_calls = [{"function": fn}]
            if isinstance(tool_calls, list):
                for c in tool_calls:
                    fn = c.get("function") if isinstance(c, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                        name = fn["name"]
                        raw_args = fn.get("arguments")
                        args = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})
                        call_id = c.get("id")
                        if call_id:
                            call_name_by_id[call_id] = name
                        parts.append(f'<tool>{{"name": {json.dumps(name)}, "arguments": {args}}}</tool>')
                        saw_tool_activity = True
            if parts:
                lines.append(f"Assistant: {' '.join(parts)}")
        elif role in {"tool", "function"}:
            tool_id = m.get("tool_call_id")
            name = call_name_by_id.get(tool_id) if tool_id else (m.get("name") or "tool")
            lines.append(f"Tool result ({name}): {content or '(no output)'}")
            saw_tool_activity = True

    sections: list[str] = []
    if system_parts:
        sections.append("\n\n".join(system_parts))
    if lines:
        sections.append("\n\n".join(lines))
    if saw_tool_activity:
        sections.append(
            "Continue the task using the tool results above. Do NOT repeat tool calls that already "
            "succeeded; perform the next step or give the final answer."
        )
    return "\n\n".join(sections)


def render_tool_transcript(messages: Iterable[dict]) -> list[str]:
    """Render assistant tool calls and tool results as upstream-readable lines."""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) and isinstance(message.get("function_call"), dict):
                fn = message["function_call"]
                if isinstance(fn.get("name"), str):
                    tool_calls = [{"function": fn}]
            if isinstance(tool_calls, list):
                text = _content_text(message.get("content"))
                calls = []
                for call in tool_calls:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                        calls.append(render_tool_call(fn["name"], fn.get("arguments")))
                if calls:
                    prefix = f"{text}\n" if text else ""
                    lines.append(f"assistant: {prefix}" + "\n".join(calls))
        elif role in {"tool", "function"}:
            text = _content_text(message.get("content"))
            if text:
                name = message.get("name") or message.get("tool_call_id") or "tool"
                lines.append(f"tool [Result of {name}]:\n{text}")
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

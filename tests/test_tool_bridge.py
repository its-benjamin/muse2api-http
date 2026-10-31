from __future__ import annotations

import json

from muse2api.tool_bridge import (
    apply_tool_bridge,
    build_tool_conversation_prompt,
    build_tool_instructions,
    build_tool_reminder,
    extract_tool_calls_from_text,
    parse_tool_choice,
    render_tool_transcript,
    resolve_requested_tool_name,
)

BODY = {
    "tools": [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "lines": {"type": "integer"}},
            },
        },
    }]
}


def test_marker_and_xml_shapes_parse():
    calls, cleaned = extract_tool_calls_from_text(
        'I will read it.\n<<TOOL_CALL>> {"name":"read_file","arguments":{"path":"a.py"}} <<TOOL_CALL>>'
    )
    assert calls == [{"name": "read_file", "arguments": {"path": "a.py"}}]
    assert cleaned == "I will read it."

    calls, cleaned = extract_tool_calls_from_text(
        '<tool_use name="read_file">{"path": "b.py"}</tool_use>'
    )
    assert calls == [{"name": "read_file", "arguments": {"path": "b.py"}}]
    assert cleaned == ""


def test_fenced_and_opencode_shapes_parse():
    text = '```tool_call\n{"name":"read_file","arguments":{"path":"c.py"}}\n```'
    assert extract_tool_calls_from_text(text)[0] == [
        {"name": "read_file", "arguments": {"path": "c.py"}}
    ]

    text = 'to=read_file {"path":"d.py"}\n'
    calls, _ = extract_tool_calls_from_text(text)
    assert calls == [{"name": "read_file", "arguments": {"path": "d.py"}}]


def test_lenient_json_python_literals_and_single_quotes():
    text = "<<TOOL_CALL>> {'name': 'read_file', 'arguments': {'path': 'e.py'}} <<TOOL_CALL>>"
    assert extract_tool_calls_from_text(text)[0] == [
        {"name": "read_file", "arguments": {"path": "e.py"}}
    ]


def test_apply_filters_unknown_args_and_resolves_case():
    result = apply_tool_bridge(
        '<<TOOL_CALL>> {"name":"Read-File","arguments":{"path":"a.py","bogus":1}} <<TOOL_CALL>>',
        BODY,
    )
    assert result["applied"] is True
    call = result["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}


def test_apply_drops_unoffered_tool_and_keeps_content():
    result = apply_tool_bridge(
        '<<TOOL_CALL>> {"name":"delete_everything","arguments":{}} <<TOOL_CALL>>',
        BODY,
    )
    assert result["applied"] is False
    assert result["reason"] == "no-called-tool-was-offered"
    assert "delete_everything" in result["content"]


def test_no_bridge_when_no_tools_or_choice_none():
    text = '<<TOOL_CALL>> {"name":"read_file","arguments":{"path":"a.py"}} <<TOOL_CALL>>'
    assert apply_tool_bridge(text, {})["applied"] is False
    assert apply_tool_bridge(text, {**BODY, "tool_choice": "none"})["applied"] is False


def test_instructions_include_schema_and_forced_rule():
    instructions = build_tool_instructions({**BODY, "tool_choice": "required"})
    assert "read_file" in instructions
    assert "Please call one of the functions above using a ```json block" in instructions
    assert "If you need to call a function" in build_tool_reminder()

    forced = build_tool_instructions({
        "tools": BODY["tools"],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    })
    assert 'Please call the function "read_file" using a ```json block' in forced


def test_parse_tool_choice_shapes():
    assert parse_tool_choice(None) == ("auto", "")
    assert parse_tool_choice("none") == ("none", "")
    assert parse_tool_choice("required") == ("required", "")
    assert parse_tool_choice({"function": {"name": "x"}}) == ("required", "x")


def test_transcript_renders_calls_and_results():
    lines = render_tool_transcript([
        {"role": "assistant", "content": "reading",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "hello"},
    ])
    assert len(lines) == 2
    assert "reading" in lines[0] and "read_file" in lines[0]
    assert "hello" in lines[1] and "[Result of read_file]" in lines[1]

    # Supports legacy role: "function" and function_call
    legacy = render_tool_transcript([
        {"role": "assistant", "function_call": {"name": "read_file", "arguments": '{"path":"b.py"}'}},
        {"role": "function", "name": "read_file", "content": "legacy_output"},
    ])
    assert len(legacy) == 2
    assert "read_file" in legacy[0]
    assert "legacy_output" in legacy[1] and "[Result of read_file]" in legacy[1]


def test_parallel_tool_calls_false_limits_to_one():
    text = (
        '```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```\n'
        '```json\n{"name": "read_file", "arguments": {"path": "b.py"}}\n```'
    )
    res_parallel = apply_tool_bridge(text, BODY)
    assert len(res_parallel["tool_calls"]) == 2

    res_single = apply_tool_bridge(text, {**BODY, "parallel_tool_calls": False})
    assert len(res_single["tool_calls"]) == 1
    assert json.loads(res_single["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}

def test_omniroute_canonical_tool_blocks():
    text = '<tool>{"name": "read_file", "arguments": {"path": "main.py"}}</tool>'
    calls, cleaned = extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "main.py"}
    assert cleaned == ""


def test_omniroute_tag_suffix_and_attributes():
    text = '<tool:read_file>{"path": "suffix.py"}</tool>'
    calls, cleaned = extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "suffix.py"}
    assert cleaned == ""

    text_attr = '<tool name="read_file">{"path": "attr.py"}</tool>'
    calls_attr, _ = extract_tool_calls_from_text(text_attr)
    assert len(calls_attr) == 1
    assert calls_attr[0]["name"] == "read_file"
    assert calls_attr[0]["arguments"] == {"path": "attr.py"}


def test_omniroute_doubled_wrapper_leaf_filter():
    text = '<tool><tool>{"name": "read_file", "arguments": {"path": "nested.py"}}</tool></tool>'
    calls, cleaned = extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "nested.py"}
    assert cleaned == ""


def test_omniroute_xml_children_and_parameter_tags():
    text_children = '<tool><name>read_file</name><arguments>{"path": "xml.py"}</arguments></tool>'
    calls1, _ = extract_tool_calls_from_text(text_children)
    assert len(calls1) == 1
    assert calls1[0]["name"] == "read_file"
    assert calls1[0]["arguments"] == {"path": "xml.py"}

    text_params = '<tool name="read_file"><parameter name="path" content="param.py"/></tool>'
    calls2, _ = extract_tool_calls_from_text(text_params)
    assert len(calls2) == 1
    assert calls2[0]["name"] == "read_file"
    assert calls2[0]["arguments"] == {"path": "param.py"}

    text_body = '<tool name="read_file"><parameter name="path">body.py</parameter></tool>'
    calls3, _ = extract_tool_calls_from_text(text_body)
    assert len(calls3) == 1
    assert calls3[0]["name"] == "read_file"
    assert calls3[0]["arguments"] == {"path": "body.py"}


def test_omniroute_fuzzy_name_matching():
    candidates = ["read_file", "write_file", "execute_command"]
    assert resolve_requested_tool_name("readfile", candidates) == "read_file"
    assert resolve_requested_tool_name("read-file", candidates) == "read_file"
    assert resolve_requested_tool_name("readFile", candidates) == "read_file"
    assert resolve_requested_tool_name("unknown_tool", candidates) is None

    # Through apply_tool_bridge
    text = '<tool>{"name": "readfile", "arguments": {"path": "fuzzy.py"}}</tool>'
    res = apply_tool_bridge(text, BODY)
    assert res["applied"] is True
    assert res["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(res["tool_calls"][0]["function"]["arguments"]) == {"path": "fuzzy.py"}


def test_omniroute_nameless_block_schema_fallback():
    text = '<tool><parameter name="path" content="nameless.py"/></tool>'
    res = apply_tool_bridge(text, BODY)
    assert res["applied"] is True
    assert res["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(res["tool_calls"][0]["function"]["arguments"]) == {"path": "nameless.py"}


def test_omniroute_conversation_prompt_builder():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Read file foo.txt"},
        {
            "role": "assistant",
            "content": "Reading file",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "foo.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "File content here"},
    ]
    prompt = build_tool_conversation_prompt(messages, tool_instructions="TOOL INSTRUCTIONS")
    assert "TOOL INSTRUCTIONS" in prompt
    assert "User: Read file foo.txt" in prompt
    assert '<tool>{"name": "read_file", "arguments": {"path": "foo.txt"}}</tool>' in prompt
    assert "Tool result (read_file): File content here" in prompt
    assert "Continue the task using the tool results above" in prompt

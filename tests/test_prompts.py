from __future__ import annotations

from muse2api.chat import ChatSession
from muse2api.server import build_prompt, record_turn


def test_prompt_sends_transcript_then_only_the_new_turn():
    state = ChatSession("session", "node")
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]
    first = build_prompt(messages, state)
    assert "be terse" in first
    assert "user: hello" in first
    assert "user: again" in first

    record_turn(state, messages)
    messages.append({"role": "user", "content": "third"})
    assert build_prompt(messages, state) == "third"


def test_rewritten_transcript_is_resent_in_full():
    state = ChatSession("session", "node")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    record_turn(state, messages)

    # The client dropped history instead of appending: send everything again.
    rewritten = [{"role": "user", "content": "totally new"}]
    resent = build_prompt(rewritten, state)
    assert "user: totally new" in resent
    assert resent != "totally new"

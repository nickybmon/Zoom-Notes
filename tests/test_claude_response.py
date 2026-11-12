"""Claude response parsing: the answer is not always content[0]."""
import pytest

from zoom_notes import _claude_text


def test_plain_text_response():
    body = {"content": [{"type": "text", "text": "# Notes"}]}
    assert _claude_text(body) == "# Notes"


def test_thinking_block_before_text():
    # Regression: Sonnet 5.5 returned a thinking block first, and reading
    # content[0]["text"] failed with "Unexpected LLM response shape: 'text'".
    body = {"content": [
        {"type": "thinking", "thinking": "...", "signature": "x"},
        {"type": "text", "text": "# Notes"},
    ]}
    assert _claude_text(body) == "# Notes"


def test_no_text_block_raises_with_details():
    body = {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": "..."}]}
    with pytest.raises(KeyError, match="max_tokens"):
        _claude_text(body)

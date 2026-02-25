"""Tests for MockToolProvider."""

import asyncio

from shared.schemas import MockToolProvider


class TestMockToolProvider:
    def test_known_tool(self):
        provider = MockToolProvider({"my_tool": '{"result": "ok"}'})
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("my_tool", {})
        )
        assert '"result"' in result

    def test_unknown_tool(self):
        provider = MockToolProvider({})
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("no_such_tool", {})
        )
        assert result == '{"error": "unknown tool"}'

    def test_multiple_tools(self):
        provider = MockToolProvider({
            "tool_a": '{"a": 1}',
            "tool_b": '{"b": 2}',
        })
        a = asyncio.get_event_loop().run_until_complete(provider.call_tool("tool_a", {}))
        b = asyncio.get_event_loop().run_until_complete(provider.call_tool("tool_b", {}))
        assert '"a"' in a
        assert '"b"' in b

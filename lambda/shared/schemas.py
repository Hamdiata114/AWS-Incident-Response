"""Shared schemas: AgentError, TokenUsage, ToolProvider protocol, and provider implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class AgentError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(f"[{category}] {message}")


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@runtime_checkable
class ToolProvider(Protocol):
    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool by name and return the raw JSON string."""
        ...


class McpToolProvider:
    """Production implementation — delegates to an MCP ClientSession."""

    def __init__(self, session):
        self._session = session

    async def call_tool(self, name: str, arguments: dict) -> str:
        mcp_name = f"tool_{name}"
        result = await self._session.call_tool(mcp_name, arguments)
        if not result.content:
            return '{"error": "Tool returned empty response"}'
        return result.content[0].text


class MockToolProvider:
    """Test implementation — returns canned responses."""

    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    async def call_tool(self, name: str, arguments: dict) -> str:
        return self._responses.get(name, '{"error": "unknown tool"}')

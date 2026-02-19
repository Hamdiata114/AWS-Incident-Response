"""Tests for mcp/supervisor/server.py."""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def client():
    """Create a Starlette TestClient for server.app."""
    from starlette.testclient import TestClient

    # Re-import server to pick up env vars
    import importlib
    import server
    importlib.reload(server)

    return TestClient(server.app, raise_server_exceptions=False)


# ── health ───────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_skips_auth(self, client):
        # No auth header, should still return 200
        resp = client.get("/health")
        assert resp.status_code == 200


# ── AuthMiddleware ───────────────────────────────────────────────────

class TestAuthMiddleware:
    def test_valid_bearer_passes(self, client):
        resp = client.get("/sse", headers={"Authorization": "Bearer test-secret-key"})
        # Should not get 401 (may get 200 or other MCP-related status)
        assert resp.status_code != 401

    def test_invalid_returns_401(self, client):
        resp = client.get("/sse", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_missing_returns_401(self, client):
        resp = client.get("/sse")
        assert resp.status_code == 401


# ── Tool wrappers ────────────────────────────────────────────────────

class TestToolWrappers:
    @pytest.mark.asyncio
    async def test_tool_get_recent_logs_returns_json(self):
        import server

        with patch.object(server, "get_recent_logs", new_callable=AsyncMock) as mock_fn:
            mock_fn.return_value = {"log_group": "/aws/lambda/test", "events": []}
            result = await server.tool_get_recent_logs("data-processor", 10)
            parsed = json.loads(result)
            assert parsed["log_group"] == "/aws/lambda/test"

"""Tests for mcp/supervisor/tools/cloudwatch_logs.py."""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def mock_logs_client(monkeypatch):
    """Replace module-level logs_client with a MagicMock."""
    import tools.cloudwatch_logs as mod

    client = MagicMock()
    monkeypatch.setattr(mod, "logs_client", client)
    return client


class TestGetRecentLogs:
    @pytest.mark.asyncio
    async def test_happy_path(self, mock_logs_client):
        from tools.cloudwatch_logs import get_recent_logs

        ts = int(datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        mock_logs_client.filter_log_events.return_value = {
            "events": [{"timestamp": ts, "message": "hello"}],
        }

        result = await get_recent_logs("data-processor", minutes=10)
        assert result["log_group"] == "/aws/lambda/data-processor"
        assert len(result["events"]) == 1
        assert result["events"][0]["message"] == "hello"

    @pytest.mark.asyncio
    async def test_no_events(self, mock_logs_client):
        from tools.cloudwatch_logs import get_recent_logs

        mock_logs_client.filter_log_events.return_value = {"events": []}

        result = await get_recent_logs("data-processor")
        assert result["events"] == []

    @pytest.mark.asyncio
    async def test_log_group_not_found(self, mock_logs_client):
        from tools.cloudwatch_logs import get_recent_logs

        # Simulate ResourceNotFoundException
        exc = type("ResourceNotFoundException", (Exception,), {})()
        mock_logs_client.exceptions.ResourceNotFoundException = type(exc)
        mock_logs_client.filter_log_events.side_effect = type(exc)("not found")

        result = await get_recent_logs("data-processor")
        assert result["error"] == "Log group not found"
        assert result["events"] == []

    @pytest.mark.asyncio
    async def test_respects_minutes_param(self, mock_logs_client):
        from tools.cloudwatch_logs import get_recent_logs

        mock_logs_client.filter_log_events.return_value = {"events": []}

        await get_recent_logs("data-processor", minutes=30)

        call_kwargs = mock_logs_client.filter_log_events.call_args[1]
        # start_time should be ~30 min ago (in ms)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        assert now_ms - call_kwargs["startTime"] >= 29 * 60 * 1000

"""Tests for classify_error."""

import asyncio

import botocore.exceptions

from shared.agent_utils import classify_error


def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "test"}}, "InvokeModel"
    )


class TestClassifyError:
    def test_timeout(self):
        assert classify_error(TimeoutError("t")).category == "mcp_connection"

    def test_async_timeout(self):
        assert classify_error(asyncio.TimeoutError()).category == "mcp_connection"

    def test_connection_error(self):
        assert classify_error(ConnectionError("c")).category == "mcp_connection"

    def test_os_error(self):
        assert classify_error(OSError("o")).category == "mcp_connection"

    def test_access_denied(self):
        assert classify_error(_client_error("AccessDeniedException")).category == "bedrock_auth"

    def test_unauthorized(self):
        assert classify_error(_client_error("UnauthorizedException")).category == "bedrock_auth"

    def test_throttling(self):
        assert classify_error(_client_error("ThrottlingException")).category == "bedrock_transient"

    def test_service_unavailable(self):
        assert classify_error(_client_error("ServiceUnavailableException")).category == "bedrock_transient"

    def test_model_timeout(self):
        assert classify_error(_client_error("ModelTimeoutException")).category == "bedrock_transient"

    def test_unknown_client_error(self):
        assert classify_error(_client_error("SomeOtherError")).category == "unknown"

    def test_unknown_exception(self):
        assert classify_error(RuntimeError("x")).category == "unknown"

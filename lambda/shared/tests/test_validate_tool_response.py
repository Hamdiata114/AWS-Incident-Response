"""Tests for validate_tool_response."""

import json

from pydantic import BaseModel

from shared.agent_utils import validate_tool_response


class DummyResponse(BaseModel):
    status: str
    value: int


SCHEMAS = {"dummy": DummyResponse}


class TestValidateToolResponse:
    def test_valid(self):
        raw = json.dumps({"status": "ok", "value": 42})
        result = validate_tool_response("dummy", raw, SCHEMAS)
        assert isinstance(result, DummyResponse)
        assert result.value == 42

    def test_malformed_json(self):
        result = validate_tool_response("dummy", "not json", SCHEMAS)
        assert isinstance(result, str)
        assert "Invalid JSON" in result

    def test_schema_mismatch(self):
        raw = json.dumps({"status": "ok"})  # missing value
        result = validate_tool_response("dummy", raw, SCHEMAS)
        assert isinstance(result, str)
        assert "validation failed" in result

    def test_unknown_tool_returns_dict(self):
        raw = json.dumps({"foo": "bar"})
        result = validate_tool_response("unknown_tool", raw, SCHEMAS)
        assert result == {"foo": "bar"}

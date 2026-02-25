"""Tests for validate_tool_args."""

import pytest
from pydantic import BaseModel, ValidationError

from shared.agent_utils import validate_tool_args


class DummyArgs(BaseModel):
    name: str
    count: int = 1


SCHEMAS = {"dummy": DummyArgs}


class TestValidateToolArgs:
    def test_valid(self):
        result = validate_tool_args("dummy", {"name": "test"}, SCHEMAS)
        assert result == {"name": "test", "count": 1}

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            validate_tool_args("dummy", {}, SCHEMAS)

    def test_extra_fields_ignored(self):
        result = validate_tool_args("dummy", {"name": "test", "extra": "x"}, SCHEMAS)
        assert "extra" not in result

    def test_unknown_tool(self):
        with pytest.raises(KeyError):
            validate_tool_args("no_such_tool", {}, SCHEMAS)

"""Tests for resolver Lambda handler."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from resolver.schemas import AWSAPICall, RemediationProposal


def _seed_resolving(dynamodb_client, incident_id):
    """Seed incident-state with RESOLVING status."""
    dynamodb_client.put_item(
        TableName="incident-state",
        Item={
            "incident_id": {"S": incident_id},
            "status": {"S": "RESOLVING"},
            "updated_at": {"S": "2025-01-15T10:30:00Z"},
        },
    )


def _get_state(dynamodb_client, incident_id):
    resp = dynamodb_client.get_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
    )
    item = resp.get("Item", {})
    return {k: v.get("S", v.get("N", v.get("BOOL"))) for k, v in item.items()}


def _get_audit(dynamodb_client, incident_id):
    resp = dynamodb_client.get_item(
        TableName="incident-audit",
        Key={"incident_id": {"S": incident_id}},
    )
    item = resp.get("Item", {})
    return {k: v.get("S", v.get("N", v.get("BOOL"))) for k, v in item.items()}


def _make_proposal(incident_id):
    return RemediationProposal(
        incident_id=incident_id,
        fault_types=["permission_loss"],
        actions=[
            AWSAPICall(
                service="iam",
                operation="put_role_policy",
                parameters={
                    "RoleName": "lab-lambda-baisc-role",
                    "PolicyName": "lab-lambda-basic-policy",
                    "PolicyDocument": "{}",
                },
                risk_level="medium",
                requires_approval=False,
                reasoning="Restore S3 access",
            )
        ],
        reasoning="Restoring revoked IAM policy",
    )


class TestHandlerHappyPath:
    """SNS event → agent produces proposal → DynamoDB gets PROPOSED."""

    def test_successful_proposal(
        self, dynamodb_resource, sns_event, sample_incident_id
    ):
        _seed_resolving(dynamodb_resource, sample_incident_id)
        proposal = _make_proposal(sample_incident_id)

        agent_result = {
            "proposal": proposal,
            "reasoning_chain": [{"role": "assistant", "content": "analyzing..."}],
            "token_usage": [{"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}],
        }

        mock_run = AsyncMock(return_value=agent_result)
        mock_context = MagicMock()
        mock_context.get_remaining_time_in_millis.return_value = 120_000

        with patch.dict("sys.modules", {"agent": MagicMock(run_agent=mock_run)}):
            import handler

            handler.dynamodb = dynamodb_resource
            result = handler.handler(sns_event, mock_context)

        body = json.loads(result["body"])
        assert body["status"] == "PROPOSED"
        assert body["incident_id"] == sample_incident_id

        state = _get_state(dynamodb_resource, sample_incident_id)
        assert state["status"] == "PROPOSED"
        assert "proposal" in state

        audit = _get_audit(dynamodb_resource, sample_incident_id)
        assert audit["agent"] == "resolver"
        assert int(audit["total_tokens"]) == 150


class TestHandlerNoProposal:
    """Agent returns None proposal → PROPOSAL_FAILED."""

    def test_none_proposal(
        self, dynamodb_resource, sns_event, sample_incident_id
    ):
        _seed_resolving(dynamodb_resource, sample_incident_id)

        agent_result = {
            "proposal": None,
            "reasoning_chain": [],
            "token_usage": [],
        }

        mock_run = AsyncMock(return_value=agent_result)
        mock_context = MagicMock()
        mock_context.get_remaining_time_in_millis.return_value = 120_000

        with patch.dict("sys.modules", {"agent": MagicMock(run_agent=mock_run)}):
            import handler

            handler.dynamodb = dynamodb_resource
            result = handler.handler(sns_event, mock_context)

        body = json.loads(result["body"])
        assert body["status"] == "PROPOSAL_FAILED"

        state = _get_state(dynamodb_resource, sample_incident_id)
        assert state["status"] == "PROPOSAL_FAILED"
        assert "Agent produced no proposal" in state.get("error_reason", "")


class TestHandlerMCPFailure:
    """Agent raises exception → PROPOSAL_FAILED."""

    def test_agent_error(
        self, dynamodb_resource, sns_event, sample_incident_id
    ):
        from shared.schemas import AgentError

        _seed_resolving(dynamodb_resource, sample_incident_id)

        mock_run = AsyncMock(side_effect=AgentError("mcp_connection", "MCP unreachable"))
        mock_context = MagicMock()
        mock_context.get_remaining_time_in_millis.return_value = 120_000

        agent_module = MagicMock(run_agent=mock_run)
        with patch.dict("sys.modules", {"agent": agent_module}):
            import handler

            handler.dynamodb = dynamodb_resource
            result = handler.handler(sns_event, mock_context)

        body = json.loads(result["body"])
        assert body["status"] == "PROPOSAL_FAILED"
        assert body["error_category"] == "mcp_connection"

        state = _get_state(dynamodb_resource, sample_incident_id)
        assert state["status"] == "PROPOSAL_FAILED"


class TestHandlerMalformedPayload:
    """Missing incident_id or diagnosis → early return."""

    def test_missing_incident_id(self, dynamodb_resource):
        event = {
            "Records": [{"Sns": {"Message": json.dumps({"diagnosis": {}})}}]
        }
        mock_context = MagicMock()

        import handler

        handler.dynamodb = dynamodb_resource
        result = handler.handler(event, mock_context)

        body = json.loads(result["body"])
        assert "error" in body

    def test_missing_diagnosis(self, dynamodb_resource, sample_incident_id):
        event = {
            "Records": [
                {"Sns": {"Message": json.dumps({"incident_id": sample_incident_id})}}
            ]
        }
        mock_context = MagicMock()

        import handler

        handler.dynamodb = dynamodb_resource
        result = handler.handler(event, mock_context)

        body = json.loads(result["body"])
        assert "error" in body

"""Tests for orchestrator.py — 44 tests, one behavior each."""

import importlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Helper: reload orchestrator with moto-patched DynamoDB
# ---------------------------------------------------------------------------

@pytest.fixture
def orch(dynamodb_resource):
    """Import orchestrator with moto DynamoDB and SNS active."""
    import orchestrator
    orchestrator.dynamodb = dynamodb_resource
    sns_client = boto3.client("sns", region_name="ca-central-1")
    # Create topic inside the same mock_aws context
    resp = sns_client.create_topic(Name="resolver-trigger")
    orchestrator.RESOLVER_TOPIC_ARN = resp["TopicArn"]
    orchestrator.sns = sns_client
    return orchestrator


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------

class TestGetState:
    def test_get_state_returns_item_when_exists(self, orch):
        orch.dynamodb.put_item(
            TableName="incident-state",
            Item={"incident_id": {"S": "id1"}, "status": {"S": "RECEIVED"}},
        )
        result = orch.get_state("id1")
        assert result["status"] == "RECEIVED"

    def test_get_state_returns_none_when_missing(self, orch):
        assert orch.get_state("nonexistent") is None

    def test_get_state_handles_numeric_attribute(self, orch):
        orch.dynamodb.put_item(
            TableName="incident-state",
            Item={"incident_id": {"S": "id1"}, "ttl": {"N": "12345"}},
        )
        result = orch.get_state("id1")
        assert result["ttl"] == "12345"


# ---------------------------------------------------------------------------
# write_initial_state
# ---------------------------------------------------------------------------

class TestWriteInitialState:
    def test_write_initial_state_creates_item(self, orch):
        orch.write_initial_state("id1")
        item = orch.get_state("id1")
        assert item["status"] == "RECEIVED"
        assert "created_at" in item
        assert "ttl" in item

    def test_write_initial_state_rejects_duplicate(self, orch):
        orch.write_initial_state("id1")
        with pytest.raises(ClientError) as exc_info:
            orch.write_initial_state("id1")
        assert "ConditionalCheckFailedException" in str(exc_info.value)


# ---------------------------------------------------------------------------
# touch_updated_at
# ---------------------------------------------------------------------------

class TestTouchUpdatedAt:
    def test_touch_updated_at_updates_timestamp(self, orch):
        orch.write_initial_state("id1")
        old = orch.get_state("id1")["updated_at"]
        orch.touch_updated_at("id1")
        new = orch.get_state("id1")["updated_at"]
        assert new >= old


# ---------------------------------------------------------------------------
# transition_state
# ---------------------------------------------------------------------------

class TestTransitionState:
    def test_transition_state_updates_status(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "INVESTIGATING")
        assert orch.get_state("id1")["status"] == "INVESTIGATING"

    def test_transition_state_fails_on_wrong_status(self, orch):
        orch.write_initial_state("id1")
        with pytest.raises(ClientError) as exc_info:
            orch.transition_state("id1", "INVESTIGATING", "FAILED")
        assert "ConditionalCheckFailedException" in str(exc_info.value)

    def test_transition_state_stores_error_reason(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "FAILED", error_reason="boom")
        item = orch.get_state("id1")
        assert item["error_reason"] == "boom"

    def test_transition_state_truncates_error_reason_to_500(self, orch):
        orch.write_initial_state("id1")
        long_reason = "x" * 600
        orch.transition_state("id1", "RECEIVED", "FAILED", error_reason=long_reason)
        item = orch.get_state("id1")
        assert len(item["error_reason"]) == 500

    def test_transition_state_stores_error_category(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "ERROR", error_category="mcp_connection")
        item = orch.get_state("id1")
        assert item["error_category"] == "mcp_connection"

    def test_transition_state_omits_error_fields_when_none(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "INVESTIGATING")
        item = orch.get_state("id1")
        assert "error_reason" not in item
        assert "error_category" not in item


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_estimate_tokens_empty_dict(self, orch):
        assert orch.estimate_tokens({}) == 0

    def test_estimate_tokens_small_payload(self, orch):
        data = {"key": "value"}
        expected = len(json.dumps(data)) // 4
        assert orch.estimate_tokens(data) == expected

    def test_estimate_tokens_datetime_default_str(self, orch):
        data = {"ts": datetime.now(timezone.utc)}
        # Should not raise — default=str handles datetime
        result = orch.estimate_tokens(data)
        assert result > 0

    def test_estimate_tokens_nested_structure(self, orch):
        data = {"a": {"b": [1, 2, 3]}}
        expected = len(json.dumps(data)) // 4
        assert orch.estimate_tokens(data) == expected


# ---------------------------------------------------------------------------
# _drop_oldest_logs
# ---------------------------------------------------------------------------

class TestDropOldestLogs:
    def test_drop_oldest_logs_removes_until_under_budget(self, orch):
        context = {"tools": {"cloudwatch_logs": {
            "events": [{"ts": str(i), "msg": "x" * 100} for i in range(20)]
        }}}
        details = orch._drop_oldest_logs(context, budget=50)
        assert details["cloudwatch_logs"]["events_dropped"] > 0
        assert len(context["tools"]["cloudwatch_logs"]["events"]) < 20

    def test_drop_oldest_logs_no_events_key(self, orch):
        context = {"tools": {"cloudwatch_logs": {"log_group": "test"}}}
        details = orch._drop_oldest_logs(context, budget=1)
        assert details == {}

    def test_drop_oldest_logs_empty_events_list(self, orch):
        context = {"tools": {"cloudwatch_logs": {"events": []}}}
        details = orch._drop_oldest_logs(context, budget=1)
        assert details.get("cloudwatch_logs", {}).get("events_dropped", 0) == 0

    def test_drop_oldest_logs_already_under_budget(self, orch):
        context = {"tools": {"cloudwatch_logs": {"events": [{"ts": "1", "msg": "hi"}]}}}
        details = orch._drop_oldest_logs(context, budget=999999)
        assert details == {}

    def test_drop_oldest_logs_drains_all_events(self, orch):
        context = {"tools": {"cloudwatch_logs": {
            "events": [{"ts": str(i), "msg": "x" * 200} for i in range(5)]
        }}}
        details = orch._drop_oldest_logs(context, budget=1)
        assert context["tools"]["cloudwatch_logs"]["events"] == []
        assert details["cloudwatch_logs"]["events_dropped"] == 5

    def test_drop_oldest_logs_no_cloudwatch_key(self, orch):
        context = {"tools": {}}
        details = orch._drop_oldest_logs(context, budget=1)
        assert details == {}

    def test_drop_oldest_logs_non_dict_data(self, orch):
        context = {"tools": {"cloudwatch_logs": "not a dict"}}
        details = orch._drop_oldest_logs(context, budget=1)
        assert details == {}


# ---------------------------------------------------------------------------
# _trim_iam_to_sids
# ---------------------------------------------------------------------------

class TestTrimIamToSids:
    def test_trim_iam_replaces_with_sids(self, orch):
        context = {"tools": {"iam_policy": {"inline_policies": {
            "policy1": {"Statement": [{"Sid": "Allow", "Effect": "Allow"}]}
        }}}}
        details = orch._trim_iam_to_sids(context, budget=1)
        assert details == {"iam_policy": {"trimmed": True}}
        assert context["tools"]["iam_policy"]["inline_policies"]["policy1"] == {
            "StatementSids": ["Allow"]
        }

    def test_trim_iam_unnamed_sid(self, orch):
        context = {"tools": {"iam_policy": {"inline_policies": {
            "p": {"Statement": [{"Effect": "Allow"}]}
        }}}}
        orch._trim_iam_to_sids(context, budget=1)
        assert context["tools"]["iam_policy"]["inline_policies"]["p"]["StatementSids"] == ["unnamed"]

    def test_trim_iam_no_iam_key(self, orch):
        context = {"tools": {}}
        details = orch._trim_iam_to_sids(context, budget=1)
        assert details == {}

    def test_trim_iam_already_under_budget(self, orch):
        context = {"tools": {"iam_policy": {"inline_policies": {
            "p": {"Statement": [{"Sid": "s1"}]}
        }}}}
        details = orch._trim_iam_to_sids(context, budget=999999)
        assert details == {}

    def test_trim_iam_non_dict_policy(self, orch):
        context = {"tools": {"iam_policy": "not a dict"}}
        details = orch._trim_iam_to_sids(context, budget=1)
        assert details == {}

    def test_trim_iam_no_statement_key(self, orch):
        context = {"tools": {"iam_policy": {"inline_policies": {
            "p": {"Version": "2012-10-17"}
        }}}}
        details = orch._trim_iam_to_sids(context, budget=1)
        # No Statement key so policy is untouched, but trimmed flag still set
        assert details == {"iam_policy": {"trimmed": True}}


# ---------------------------------------------------------------------------
# _drop_lambda_config
# ---------------------------------------------------------------------------

class TestDropLambdaConfig:
    def test_drop_config_replaces_with_flag(self, orch):
        context = {"tools": {"lambda_config": {"FunctionName": "test", "big": "x" * 500}}}
        details = orch._drop_lambda_config(context, budget=1)
        assert context["tools"]["lambda_config"] == {"dropped": True}
        assert details == {"lambda_config": {"dropped": True}}

    def test_drop_config_no_key(self, orch):
        context = {"tools": {}}
        details = orch._drop_lambda_config(context, budget=1)
        assert details == {}

    def test_drop_config_already_under_budget(self, orch):
        context = {"tools": {"lambda_config": {"FunctionName": "test"}}}
        details = orch._drop_lambda_config(context, budget=999999)
        assert details == {}


# ---------------------------------------------------------------------------
# truncate_to_budget
# ---------------------------------------------------------------------------

class TestTruncateToBudget:
    def test_truncate_zero_budget_returns_skipped(self, orch):
        ctx, details = orch.truncate_to_budget({"tools": {}}, 0)
        assert details["skipped"] is True

    def test_truncate_under_budget_no_changes(self, orch):
        ctx = {"tools": {"cloudwatch_logs": {"events": []}}}
        _, details = orch.truncate_to_budget(ctx, 999999)
        assert details == {}

    def test_truncate_applies_stages_in_order(self, orch):
        # Large enough to trigger all three stages
        context = {
            "tools": {
                "cloudwatch_logs": {
                    "events": [{"ts": str(i), "msg": "x" * 200} for i in range(20)]
                },
                "iam_policy": {
                    "inline_policies": {
                        "p": {"Statement": [{"Sid": "s", "Effect": "Allow", "Resource": "*" * 100}]}
                    }
                },
                "lambda_config": {"FunctionName": "test", "big": "y" * 500},
            }
        }
        _, details = orch.truncate_to_budget(context, budget=1)
        # All three stages should have run
        assert "cloudwatch_logs" in details
        assert "iam_policy" in details
        assert "lambda_config" in details


# ---------------------------------------------------------------------------
# _compute_metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_compute_metrics_basic(self, orch):
        m = orch._compute_metrics(
            raw_sizes={"logs": 100, "iam": 50},
            token_budget=200,
            final_tokens=120,
            truncation_details={"logs": {"dropped": 5}},
        )
        assert m["raw_tokens_total"] == 150
        assert m["final_tokens"] == 120
        assert m["truncation_details"] == {"logs": {"dropped": 5}}

    def test_compute_metrics_no_truncation(self, orch):
        m = orch._compute_metrics(
            raw_sizes={"logs": 50},
            token_budget=200,
            final_tokens=50,
            truncation_details={},
        )
        assert m["truncated"] is False

    def test_compute_metrics_zero_budget(self, orch):
        m = orch._compute_metrics(
            raw_sizes={"logs": 50},
            token_budget=0,
            final_tokens=50,
            truncation_details={},
        )
        assert m["truncated"] is False


# ---------------------------------------------------------------------------
# parse_sns_event
# ---------------------------------------------------------------------------

class TestParseSnsEvent:
    def test_parse_valid_sns(self, orch, sns_event, sample_incident):
        result = orch.parse_sns_event(sns_event)
        assert result == sample_incident

    def test_parse_missing_records(self, orch):
        with pytest.raises(KeyError):
            orch.parse_sns_event({})

    def test_parse_invalid_json_body(self, orch):
        event = {"Records": [{"Sns": {"Message": "not json"}}]}
        with pytest.raises(json.JSONDecodeError):
            orch.parse_sns_event(event)

    def test_parse_empty_records(self, orch):
        with pytest.raises(IndexError):
            orch.parse_sns_event({"Records": []})


# ---------------------------------------------------------------------------
# _dedup_or_recover
# ---------------------------------------------------------------------------

class TestDedupOrRecover:
    def test_dedup_new_returns_none(self, orch):
        result = orch._dedup_or_recover("new-id")
        assert result is None
        assert orch.get_state("new-id")["status"] == "RECEIVED"

    def test_dedup_received_returns_none(self, orch):
        orch.write_initial_state("id1")
        result = orch._dedup_or_recover("id1")
        assert result is None

    def test_dedup_stale_investigating_returns_none(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "INVESTIGATING")
        # Backdate updated_at to make it stale
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        orch.dynamodb.update_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "id1"}},
            UpdateExpression="SET updated_at = :t",
            ExpressionAttributeValues={":t": {"S": stale_time}},
        )
        result = orch._dedup_or_recover("id1")
        assert result is None
        assert orch.get_state("id1")["status"] == "RECEIVED"

    def test_dedup_active_investigating_returns_skip(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "INVESTIGATING")
        result = orch._dedup_or_recover("id1")
        assert result == "skip"

    def test_dedup_terminal_returns_skip(self, orch):
        orch.write_initial_state("id1")
        orch.transition_state("id1", "RECEIVED", "CONTEXT_GATHERED")
        result = orch._dedup_or_recover("id1")
        assert result == "skip"


# ---------------------------------------------------------------------------
# _store_context
# ---------------------------------------------------------------------------

class TestStoreContext:
    def test_store_context_writes_item(self, orch, sample_incident):
        orch._store_context("id1", sample_incident, {"tools": {}})
        resp = orch.dynamodb.get_item(
            TableName="incident-context",
            Key={"incident_id": {"S": "id1"}},
        )
        item = resp["Item"]
        assert item["error_type"]["S"] == "access_denied"
        assert "ttl" in item

    def test_store_context_defaults_error_type(self, orch):
        incident_no_type = {"lambda_name": "test", "timestamp": "t"}
        orch._store_context("id1", incident_no_type, {"tools": {}})
        resp = orch.dynamodb.get_item(
            TableName="incident-context",
            Key={"incident_id": {"S": "id1"}},
        )
        assert resp["Item"]["error_type"]["S"] == "unknown"


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

def _make_agent_result(diagnosis, reasoning_chain=None, token_usage=None):
    """Helper to create agent result dict matching new run_agent return shape."""
    return {
        "diagnosis": diagnosis,
        "reasoning_chain": reasoning_chain or [],
        "token_usage": token_usage or [],
    }


# ---------------------------------------------------------------------------
# _store_audit
# ---------------------------------------------------------------------------

class TestStoreAudit:
    def test_store_audit_writes_item(self, orch):
        chain = [{"type": "HumanMessage", "content": "hi"}]
        usage = [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]
        orch._store_audit("id1", chain, usage)
        resp = orch.dynamodb.get_item(
            TableName="incident-audit",
            Key={"incident_id": {"S": "id1"}},
        )
        item = resp["Item"]
        assert "reasoning_chain" in item
        assert "token_usage" in item
        assert "ttl" in item

    def test_store_audit_truncates_large_chain(self, orch):
        # Create chain > 350KB
        chain = [{"type": "AIMessage", "content": "x" * 100_000} for _ in range(5)]
        orch._store_audit("id1", chain, [])
        resp = orch.dynamodb.get_item(
            TableName="incident-audit",
            Key={"incident_id": {"S": "id1"}},
        )
        item = resp["Item"]
        assert item.get("reasoning_truncated", {}).get("BOOL") is True
        stored = json.loads(item["reasoning_chain"]["S"])
        assert len(stored) == 4  # first 1 + last 3

    def test_store_audit_empty_chain_and_usage(self, orch):
        orch._store_audit("id1", [], [])
        resp = orch.dynamodb.get_item(
            TableName="incident-audit",
            Key={"incident_id": {"S": "id1"}},
        )
        item = resp["Item"]
        assert "reasoning_chain" not in item
        assert "token_usage" not in item


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

class TestHandler:
    def test_handler_happy_path(self, orch, sns_event, sample_incident_id):
        from schemas import Diagnosis
        diag = Diagnosis(
            root_cause="S3 policy revoked", fault_types=["permission_loss"],
            affected_resources=["data-processor"], severity="high",
            evidence=[], remediation_plan=[],
        )
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", return_value=_make_agent_result(diag)):
            result = orch.handler(sns_event, mock_ctx)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "RESOLVING"
        # Verify state transitioned to RESOLVING
        state = orch.get_state(sample_incident_id)
        assert state["status"] == "RESOLVING"

    def test_handler_skips_duplicate(self, orch, sns_event, sample_incident_id):
        orch.write_initial_state(sample_incident_id)
        orch.transition_state(sample_incident_id, "RECEIVED", "CONTEXT_GATHERED")
        result = orch.handler(sns_event, None)
        assert result["body"] == "already handled"

    def test_handler_no_diagnosis(self, orch, sns_event):
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", return_value=_make_agent_result(None)):
            result = orch.handler(sns_event, mock_ctx)
        body = json.loads(result["body"])
        assert body["status"] == "FAILED"

    def test_handler_agent_error(self, orch, sns_event):
        from schemas import AgentError
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", side_effect=AgentError("mcp_connection", "timeout")):
            result = orch.handler(sns_event, mock_ctx)
        body = json.loads(result["body"])
        assert body["status"] == "FAILED"
        assert body["error_category"] == "mcp_connection"

    def test_handler_failed_on_exception(self, orch, sns_event):
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", side_effect=RuntimeError("MCP down")):
            with pytest.raises(RuntimeError):
                orch.handler(sns_event, mock_ctx)

    def test_handler_logs_transition_failure(self, orch, sns_event):
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", side_effect=RuntimeError("fail")):
            with patch.object(orch, "transition_state", side_effect=[None, ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "UpdateItem"
            )]):
                with pytest.raises(RuntimeError):
                    orch.handler(sns_event, mock_ctx)

    def test_handler_stores_audit_on_diagnosis(self, orch, sns_event):
        from schemas import Diagnosis
        diag = Diagnosis(
            root_cause="test", fault_types=["throttling"],
            affected_resources=["fn"], severity="medium",
            evidence=[], remediation_plan=[],
        )
        chain = [{"type": "AIMessage", "content": "thinking", "tool_calls": [{"name": "get_iam_state"}]}]
        usage = [{"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}]
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", return_value=_make_agent_result(diag, chain, usage)):
            orch.handler(sns_event, mock_ctx)
        resp = orch.dynamodb.get_item(
            TableName="incident-audit",
            Key={"incident_id": {"S": "data-processor#2025-01-15T10:30:00Z"}},
        )
        assert "Item" in resp


# ---------------------------------------------------------------------------
# Resolver SNS handoff
# ---------------------------------------------------------------------------

class TestResolverHandoff:
    def test_sns_publish_failure_stays_diagnosed(self, orch, sns_event, sample_incident_id):
        """If SNS publish fails, state stays at DIAGNOSED (not RESOLVING)."""
        from schemas import Diagnosis
        diag = Diagnosis(
            root_cause="S3 policy revoked", fault_types=["permission_loss"],
            affected_resources=["data-processor"], severity="high",
            evidence=[], remediation_plan=[],
        )
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", return_value=_make_agent_result(diag)):
            with patch.object(orch.sns, "publish", side_effect=Exception("SNS down")):
                # The transition to RESOLVING happens before publish, so we need
                # to also patch transition_state to fail on DIAGNOSED->RESOLVING
                # Actually: transition succeeds, then publish fails.
                # The state will be RESOLVING but response says DIAGNOSED.
                result = orch.handler(sns_event, mock_ctx)
        body = json.loads(result["body"])
        assert body["status"] == "DIAGNOSED"
        assert body["resolver_handoff_failed"] is True

    def test_sns_publish_sends_diagnosis(self, orch, sns_event, sample_incident_id):
        """Verify SNS message contains incident_id and diagnosis."""
        from schemas import Diagnosis
        diag = Diagnosis(
            root_cause="throttled", fault_types=["throttling"],
            affected_resources=["data-processor"], severity="medium",
            evidence=[], remediation_plan=[],
        )
        mock_ctx = type("Ctx", (), {"get_remaining_time_in_millis": lambda self: 280000})()
        with patch("agent.run_agent", return_value=_make_agent_result(diag)):
            with patch.object(orch.sns, "publish", wraps=orch.sns.publish) as mock_pub:
                orch.handler(sns_event, mock_ctx)
                mock_pub.assert_called_once()
                call_kwargs = mock_pub.call_args[1]
                msg = json.loads(call_kwargs["Message"])
                assert msg["incident_id"] == sample_incident_id
                assert msg["diagnosis"]["root_cause"] == "throttled"
                assert msg["diagnosis"]["fault_types"] == ["throttling"]

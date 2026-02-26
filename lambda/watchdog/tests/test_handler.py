"""Tests for the watchdog handler."""

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("RESOLVER_TOPIC_ARN", "arn:aws:sns:ca-central-1:534321188934:resolver-trigger")


@pytest.fixture
def dynamodb_table():
    """Create mocked incident-state, incident-context tables and SNS topic."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")
        client.create_table(
            TableName="incident-state",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="incident-context",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # SNS topic
        sns_client = boto3.client("sns", region_name="ca-central-1")
        resp = sns_client.create_topic(Name="resolver-trigger")
        yield client, sns_client, resp["TopicArn"]


def _put_incident(client, incident_id, status, updated_at):
    client.put_item(
        TableName="incident-state",
        Item={
            "incident_id": {"S": incident_id},
            "status": {"S": status},
            "updated_at": {"S": updated_at},
        },
    )


# ── scan_stale_incidents ─────────────────────────────────────────────

class TestScanStaleIncidents:
    def test_finds_stale(self, dynamodb_table):
        from handler import scan_stale_incidents
        db, _, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(db, "inc-1", "INVESTIGATING", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(db, cutoff)
        assert len(items) == 1
        assert items[0]["incident_id"]["S"] == "inc-1"

    def test_ignores_fresh(self, dynamodb_table):
        from handler import scan_stale_incidents
        db, _, _ = dynamodb_table

        fresh = datetime.now(timezone.utc).isoformat()
        _put_incident(db, "inc-2", "INVESTIGATING", fresh)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(db, cutoff)
        assert len(items) == 0

    def test_ignores_non_investigating(self, dynamodb_table):
        from handler import scan_stale_incidents
        db, _, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(db, "inc-3", "RESOLVED", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(db, cutoff)
        assert len(items) == 0

    def test_empty_table(self, dynamodb_table):
        from handler import scan_stale_incidents
        db, _, _ = dynamodb_table

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(db, cutoff)
        assert items == []


# ── transition_to_failed ─────────────────────────────────────────────

class TestTransitionToFailed:
    def test_success(self, dynamodb_table):
        from handler import transition_to_failed
        db, _, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(db, "inc-1", "INVESTIGATING", old)

        result = transition_to_failed(db, "inc-1")
        assert result is True

        item = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]
        assert item["status"]["S"] == "FAILED"
        assert item["error_reason"]["S"] == "stale watchdog timeout"

    def test_already_transitioned(self, dynamodb_table):
        from handler import transition_to_failed
        db, _, _ = dynamodb_table

        _put_incident(db, "inc-1", "RESOLVED", "2024-01-01T00:00:00")

        result = transition_to_failed(db, "inc-1")
        assert result is False


# ── scan_failed_proposals ────────────────────────────────────────────

class TestScanFailedProposals:
    def test_finds_failed(self, dynamodb_table):
        from handler import scan_failed_proposals
        db, _, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _put_incident(db, "inc-1", "PROPOSAL_FAILED", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        items = scan_failed_proposals(db, cutoff)
        assert len(items) == 1

    def test_ignores_fresh_failures(self, dynamodb_table):
        from handler import scan_failed_proposals
        db, _, _ = dynamodb_table

        fresh = datetime.now(timezone.utc).isoformat()
        _put_incident(db, "inc-1", "PROPOSAL_FAILED", fresh)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        items = scan_failed_proposals(db, cutoff)
        assert len(items) == 0

    def test_ignores_other_statuses(self, dynamodb_table):
        from handler import scan_failed_proposals
        db, _, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _put_incident(db, "inc-1", "FAILED", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        items = scan_failed_proposals(db, cutoff)
        assert len(items) == 0


# ── retry_proposal ───────────────────────────────────────────────────

class TestRetryProposal:
    def test_retries_first_attempt(self, dynamodb_table, monkeypatch):
        import handler as mod
        from handler import retry_proposal
        db, sns_client, topic_arn = dynamodb_table
        monkeypatch.setattr(mod, "RESOLVER_TOPIC_ARN", topic_arn)

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _put_incident(db, "inc-1", "PROPOSAL_FAILED", old)
        # Store diagnosis in incident-context
        db.put_item(
            TableName="incident-context",
            Item={
                "incident_id": {"S": "inc-1"},
                "enriched_context": {"S": json.dumps({"diagnosis": {"root_cause": "test"}})},
            },
        )

        item = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]

        result = retry_proposal(db, sns_client, item)
        assert result is True

        # Check state transitioned to RESOLVING with retry_count=1
        state = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]
        assert state["status"]["S"] == "RESOLVING"
        assert state["retry_count"]["N"] == "1"

    def test_max_retries_marks_failed(self, dynamodb_table):
        from handler import retry_proposal
        db, sns_client, _ = dynamodb_table

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db.put_item(
            TableName="incident-state",
            Item={
                "incident_id": {"S": "inc-1"},
                "status": {"S": "PROPOSAL_FAILED"},
                "updated_at": {"S": old},
                "retry_count": {"N": "2"},
            },
        )

        item = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]

        result = retry_proposal(db, sns_client, item)
        assert result is False

        state = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]
        assert state["status"]["S"] == "FAILED"
        assert "max retries" in state["error_reason"]["S"]

    def test_second_retry_increments_count(self, dynamodb_table, monkeypatch):
        import handler as mod
        from handler import retry_proposal
        db, sns_client, topic_arn = dynamodb_table
        monkeypatch.setattr(mod, "RESOLVER_TOPIC_ARN", topic_arn)

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db.put_item(
            TableName="incident-state",
            Item={
                "incident_id": {"S": "inc-1"},
                "status": {"S": "PROPOSAL_FAILED"},
                "updated_at": {"S": old},
                "retry_count": {"N": "1"},
            },
        )

        item = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]

        result = retry_proposal(db, sns_client, item)
        assert result is True

        state = db.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]
        assert state["retry_count"]["N"] == "2"


# ── handler (integration) ────────────────────────────────────────────

class TestHandler:
    def test_processes_stale(self, dynamodb_table, monkeypatch):
        import handler as mod
        db, sns_client, topic_arn = dynamodb_table

        monkeypatch.setattr(mod, "dynamodb", db)
        monkeypatch.setattr(mod, "sns", sns_client)
        monkeypatch.setattr(mod, "RESOLVER_TOPIC_ARN", topic_arn)

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(db, "inc-1", "INVESTIGATING", old)

        resp = mod.handler({}, None)
        assert resp["statusCode"] == 200
        assert "1 stale" in resp["body"]

    def test_no_stale(self, dynamodb_table, monkeypatch):
        import handler as mod
        db, sns_client, topic_arn = dynamodb_table

        monkeypatch.setattr(mod, "dynamodb", db)
        monkeypatch.setattr(mod, "sns", sns_client)
        monkeypatch.setattr(mod, "RESOLVER_TOPIC_ARN", topic_arn)

        resp = mod.handler({}, None)
        assert resp["statusCode"] == 200
        assert "0 stale" in resp["body"]

    def test_retries_failed_proposals(self, dynamodb_table, monkeypatch):
        import handler as mod
        db, sns_client, topic_arn = dynamodb_table

        monkeypatch.setattr(mod, "dynamodb", db)
        monkeypatch.setattr(mod, "sns", sns_client)
        monkeypatch.setattr(mod, "RESOLVER_TOPIC_ARN", topic_arn)

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _put_incident(db, "inc-1", "PROPOSAL_FAILED", old)

        resp = mod.handler({}, None)
        assert resp["statusCode"] == 200
        assert "retried 1/1" in resp["body"]

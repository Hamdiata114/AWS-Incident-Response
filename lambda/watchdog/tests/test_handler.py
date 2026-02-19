"""Tests for the watchdog handler."""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def dynamodb_table():
    """Create mocked incident-state table and return client."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")
        client.create_table(
            TableName="incident-state",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


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

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(dynamodb_table, "inc-1", "INVESTIGATING", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(dynamodb_table, cutoff)
        assert len(items) == 1
        assert items[0]["incident_id"]["S"] == "inc-1"

    def test_ignores_fresh(self, dynamodb_table):
        from handler import scan_stale_incidents

        fresh = datetime.now(timezone.utc).isoformat()
        _put_incident(dynamodb_table, "inc-2", "INVESTIGATING", fresh)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(dynamodb_table, cutoff)
        assert len(items) == 0

    def test_ignores_non_investigating(self, dynamodb_table):
        from handler import scan_stale_incidents

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(dynamodb_table, "inc-3", "RESOLVED", old)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(dynamodb_table, cutoff)
        assert len(items) == 0

    def test_empty_table(self, dynamodb_table):
        from handler import scan_stale_incidents

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        items = scan_stale_incidents(dynamodb_table, cutoff)
        assert items == []


# ── transition_to_failed ─────────────────────────────────────────────

class TestTransitionToFailed:
    def test_success(self, dynamodb_table):
        from handler import transition_to_failed

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(dynamodb_table, "inc-1", "INVESTIGATING", old)

        result = transition_to_failed(dynamodb_table, "inc-1")
        assert result is True

        item = dynamodb_table.get_item(
            TableName="incident-state",
            Key={"incident_id": {"S": "inc-1"}},
        )["Item"]
        assert item["status"]["S"] == "FAILED"
        assert item["error_reason"]["S"] == "stale watchdog timeout"

    def test_already_transitioned(self, dynamodb_table):
        from handler import transition_to_failed

        _put_incident(dynamodb_table, "inc-1", "RESOLVED", "2024-01-01T00:00:00")

        result = transition_to_failed(dynamodb_table, "inc-1")
        assert result is False


# ── handler (integration) ────────────────────────────────────────────

class TestHandler:
    def test_processes_stale(self, dynamodb_table, monkeypatch):
        import handler as mod

        monkeypatch.setattr(mod, "dynamodb", dynamodb_table)

        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _put_incident(dynamodb_table, "inc-1", "INVESTIGATING", old)

        resp = mod.handler({}, None)
        assert resp["statusCode"] == 200
        assert "1" in resp["body"]

    def test_no_stale(self, dynamodb_table, monkeypatch):
        import handler as mod

        monkeypatch.setattr(mod, "dynamodb", dynamodb_table)

        resp = mod.handler({}, None)
        assert resp["statusCode"] == 200
        assert "0" in resp["body"]

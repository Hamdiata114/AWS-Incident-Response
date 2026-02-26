"""Shared fixtures for supervisor agent tests."""

import json
import os
import sys
from pathlib import Path

# Add lambda/ to sys.path so 'shared' package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import boto3
import pytest
from moto import mock_aws


# Ensure orchestrator.py env vars are set before import
@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8080/sse")
    monkeypatch.setenv("MCP_API_KEY", "test-key")
    monkeypatch.setenv("TOKEN_BUDGET", "6000")
    monkeypatch.setenv("RESOLVER_TOPIC_ARN", "arn:aws:sns:ca-central-1:534321188934:resolver-trigger")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def dynamodb_resource():
    """Mocked DynamoDB resource with incident-state and incident-context tables."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")

        # incident-state table
        client.create_table(
            TableName="incident-state",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # incident-context table
        client.create_table(
            TableName="incident-context",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # incident-audit table
        client.create_table(
            TableName="incident-audit",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield client


@pytest.fixture
def sample_incident():
    """A minimal incident payload as parsed from SNS."""
    return {
        "lambda_name": "data-processor",
        "timestamp": "2025-01-15T10:30:00Z",
        "error_type": "access_denied",
        "error_message": "AccessDeniedException: User is not authorized",
        "request_id": "abc-123",
    }


@pytest.fixture
def sample_incident_id(sample_incident):
    return f"{sample_incident['lambda_name']}#{sample_incident['timestamp']}"


@pytest.fixture
def sns_event(sample_incident):
    """A complete SNS event wrapping the sample incident."""
    return {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps(sample_incident),
                }
            }
        ]
    }

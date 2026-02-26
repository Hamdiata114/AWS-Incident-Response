"""Conftest for resolver agent tests."""

import json
import sys
from pathlib import Path

# Add lambda/ so 'shared' and 'resolver' packages are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# Add lambda/resolver so bare 'schemas' import works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "http://localhost:8081/sse")
    monkeypatch.setenv("MCP_API_KEY", "test-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def dynamodb_resource():
    """Mocked DynamoDB with incident-state and incident-audit tables."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")

        client.create_table(
            TableName="incident-state",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        client.create_table(
            TableName="incident-audit",
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "incident_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield client


@pytest.fixture
def sample_diagnosis():
    """A sample diagnosis payload as passed by supervisor."""
    return {
        "root_cause": "IAM policy for S3 access was revoked",
        "fault_types": ["permission_loss"],
        "affected_resources": ["data-processor", "lab-lambda-baisc-role"],
        "severity": "high",
        "evidence": [
            {
                "tool": "get_iam_state",
                "field": "inline_policies",
                "value": "missing S3 statement",
                "interpretation": "S3 access was removed from the role policy",
            }
        ],
        "remediation_plan": [
            {
                "action": "Restore S3 IAM policy",
                "details": "Re-attach the S3 access policy statement",
                "evidence_basis": [0],
                "risk_level": "medium",
                "requires_approval": False,
            }
        ],
    }


@pytest.fixture
def sample_incident_id():
    return "data-processor#2025-01-15T10:30:00Z"


@pytest.fixture
def sns_event(sample_incident_id, sample_diagnosis):
    """SNS event wrapping resolver payload."""
    return {
        "Records": [
            {
                "Sns": {
                    "Message": json.dumps({
                        "incident_id": sample_incident_id,
                        "diagnosis": sample_diagnosis,
                    }),
                }
            }
        ]
    }

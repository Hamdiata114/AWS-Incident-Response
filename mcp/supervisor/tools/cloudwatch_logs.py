import boto3
from datetime import datetime, timezone, timedelta

logs_client = boto3.client("logs", region_name="ca-central-1")


async def get_recent_logs(lambda_name: str, minutes: int = 10) -> dict:
    """Fetch recent CloudWatch logs from a Lambda function."""
    # TODO Phase 3: accept optional log_group param, validate with describe_log_groups
    log_group = f"/aws/lambda/{lambda_name}"
    start_time = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)

    try:
        resp = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_time,
            limit=30,
            interleaved=True,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return {"log_group": log_group, "events": [], "error": "Log group not found"}

    events = []
    for e in resp.get("events", []):
        events.append({
            "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
            "message": e["message"][:500],
        })

    return {"log_group": log_group, "events": events}

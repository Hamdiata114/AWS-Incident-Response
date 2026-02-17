# Integration Test Status

## Working
- SNS trigger → supervisor-agent Lambda invocation
- MCP SSE connection to `http://3.99.16.1:8080/sse` (200 OK)
- MCP session initialization (202 Accepted)
- SSM parameter fetch (`/incident-response/mcp-api-key`)
- DynamoDB state machine (RECEIVED → INVESTIGATING → FAILED)
- Error classification + ExceptionGroup unwrapping
- Chaos script revokes S3 permissions correctly
- Lambda memory (512MB) and timeout (300s) are fine
- IAM for Bedrock is now `Resource: *` (broad, works)

## Not Working
- **Bedrock model access**: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` returns:
  ```
  ResourceNotFoundException: Model use case details have not been submitted
  for this account. Fill out the Anthropic use case details form before using
  the model. If you have already filled out the form, try again in 15 minutes.
  ```

## Fix Required
1. Go to **AWS Console → Bedrock → Model access** (ca-central-1)
2. Request access for an Anthropic Claude model
3. Fill out the Anthropic use case form if prompted
4. Wait ~15 min for approval
5. Then re-publish the SNS test message:
   ```bash
   TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
   aws sns publish \
     --topic-arn arn:aws:sns:ca-central-1:534321188934:incident-alerts \
     --message "{
       \"lambda_name\": \"data-processor\",
       \"timestamp\": \"$TIMESTAMP\",
       \"error_type\": \"access_denied\",
       \"error_message\": \"AccessDenied: s3:ListBucket on lab-security-evidence-1\",
       \"request_id\": \"test-integration-manual\"
     }" \
     --region ca-central-1
   ```
6. Check result:
   ```bash
   # Incident state
   aws dynamodb scan --table-name incident-state --region ca-central-1 \
     --query 'Items[].{id:incident_id.S,status:status.S}' --output table

   # Diagnosis (if DIAGNOSED)
   aws dynamodb scan --table-name incident-context --region ca-central-1 --output json

   # Logs
   aws logs get-log-events \
     --log-group-name /aws/lambda/supervisor-agent \
     --log-stream-name "$(aws logs describe-log-streams \
       --log-group-name /aws/lambda/supervisor-agent \
       --region ca-central-1 --order-by LastEventTime \
       --descending --limit 1 \
       --query 'logStreams[0].logStreamName' --output text)" \
     --region ca-central-1 --limit 50 \
     --query 'events[].message' --output text
   ```

## Current Model Config
- File: `agent.py` line 43
- Model: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (inference profile)
- If you want a different model, also available in ca-central-1:
  - `anthropic.claude-3-haiku-20240307-v1:0` (cheapest, may work without form)
  - `anthropic.claude-3-sonnet-20240229-v1:0`

## Full Test Sequence (commands used)

### 1. Revoke S3 permissions (chaos injection)
```bash
python3 chaos/iam_chaos.py revoke --target s3
```

### 2. Invoke data-processor (auto-publishes to SNS on failure, which triggers supervisor)
```bash
aws lambda invoke \
  --function-name data-processor \
  --region ca-central-1 \
  --payload '{}' \
  /tmp/dp-response.json && cat /tmp/dp-response.json
```

### 3. Wait ~2 minutes, then check DynamoDB state
```bash
aws dynamodb scan --table-name incident-state --region ca-central-1 \
  --query 'Items[].{id:incident_id.S,status:status.S,err:error_reason.S}' --output table
```

### 4. Check diagnosis context (if status = DIAGNOSED)
```bash
aws dynamodb scan --table-name incident-context --region ca-central-1 --output json
```

### 5. Check CloudWatch logs (latest invocation)
```bash
aws logs get-log-events \
  --log-group-name /aws/lambda/supervisor-agent \
  --log-stream-name "$(aws logs describe-log-streams \
    --log-group-name /aws/lambda/supervisor-agent \
    --region ca-central-1 --order-by LastEventTime \
    --descending --limit 1 \
    --query 'logStreams[0].logStreamName' --output text)" \
  --region ca-central-1 --limit 50 \
  --query 'events[].message' --output text
```

### 6. Filter logs for errors only
```bash
aws logs get-log-events \
  --log-group-name /aws/lambda/supervisor-agent \
  --log-stream-name "$(aws logs describe-log-streams \
    --log-group-name /aws/lambda/supervisor-agent \
    --region ca-central-1 --order-by LastEventTime \
    --descending --limit 1 \
    --query 'logStreams[0].logStreamName' --output text)" \
  --region ca-central-1 --limit 50 \
  --query 'events[].message' --output text | grep -E "ERROR|Exception|Traceback"
```

### 7. Check IAM status
```bash
python3 chaos/iam_chaos.py status
```

## Don't Forget
- **Restore S3 permissions** after testing:
  ```bash
  python3 chaos/iam_chaos.py restore
  ```

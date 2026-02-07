import boto3

lambda_client = boto3.client("lambda", region_name="ca-central-1")
iam_client = boto3.client("iam")

SUPPORTED_LAMBDAS = {"data-processor"}


async def get_iam_state(lambda_name: str) -> dict:
    """Get current IAM policy state for a Lambda's execution role."""
    if lambda_name not in SUPPORTED_LAMBDAS:
        return {"error": f"Unsupported lambda: {lambda_name}. Supported: {SUPPORTED_LAMBDAS}"}

    # Get role name from Lambda config (not hardcoded)
    fn = lambda_client.get_function(FunctionName=lambda_name)
    role_arn = fn["Configuration"]["Role"]
    role_name = role_arn.split("/")[-1]

    # Attached managed policies
    attached = iam_client.list_attached_role_policies(RoleName=role_name)
    attached_policies = [p["PolicyArn"] for p in attached["AttachedPolicies"]]

    # Inline policies
    inline_names = iam_client.list_role_policies(RoleName=role_name)["PolicyNames"]
    inline_policies = {}
    for name in inline_names:
        doc = iam_client.get_role_policy(RoleName=role_name, PolicyName=name)
        inline_policies[name] = doc["PolicyDocument"]

    return {
        "role_name": role_name,
        "inline_policies": inline_policies,
        "attached_policies": attached_policies,
    }

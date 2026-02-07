import os
import json
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from tools.cloudwatch_logs import get_recent_logs
from tools.iam_policy import get_iam_state
from tools.lambda_config import get_lambda_config

API_KEY = os.environ["MCP_API_KEY"]

mcp = FastMCP("supervisor-tools")


@mcp.tool()
async def tool_get_recent_logs(lambda_name: str, minutes: int = 10) -> str:
    """Fetch recent CloudWatch logs from a Lambda function."""
    result = await get_recent_logs(lambda_name, minutes)
    return json.dumps(result, default=str)


@mcp.tool()
async def tool_get_iam_state(lambda_name: str) -> str:
    """Get current IAM policy state for a Lambda's execution role."""
    result = await get_iam_state(lambda_name)
    return json.dumps(result, default=str)


@mcp.tool()
async def tool_get_lambda_config(lambda_name: str) -> str:
    """Get Lambda function configuration metadata."""
    result = await get_lambda_config(lambda_name)
    return json.dumps(result, default=str)


# Auth middleware — skips /health, checks Bearer token on all other routes
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {API_KEY}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(request: Request):
    return JSONResponse({"status": "ok"})


# Get the SSE app from FastMCP and wrap it with auth + health
mcp_app = mcp.sse_app()

app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/", app=mcp_app),
    ],
    middleware=[Middleware(AuthMiddleware)],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

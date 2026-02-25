import os
import json
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from tools.iam_baseline import get_baseline_iam
from tools.concurrency import get_current_concurrency

API_KEY = os.environ["MCP_API_KEY"]

mcp = FastMCP("resolver-tools")


@mcp.tool()
async def tool_get_baseline_iam(role_name: str) -> str:
    """Compare current IAM inline policy against known-good baseline."""
    result = await get_baseline_iam(role_name)
    return json.dumps(result, default=str)


@mcp.tool()
async def tool_get_current_concurrency(lambda_name: str) -> str:
    """Get Lambda reserved concurrency and flag if throttled."""
    result = await get_current_concurrency(lambda_name)
    return json.dumps(result, default=str)


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
    uvicorn.run(app, host="0.0.0.0", port=8081)

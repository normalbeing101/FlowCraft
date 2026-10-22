"""http.request — SSRF-guarded outbound HTTP.

Contract:
  idempotent=False, supports_idempotency_key=True (key forwarded as
  `Idempotency-Key` header so cooperating downstream APIs can dedupe).
  side_effects depends on method: GET/HEAD = read; others = write.
Failure behavior:
  - Network-policy violations -> CONFIG (never retried).
  - Connection errors/timeouts BEFORE a response was received -> TRANSIENT
    (retry allowed only if step declares an idempotency key or method is safe).
  - 5xx/429 responses: classified but treated like connection errors w.r.t.
    replay safety.
  - 4xx responses: returned as structured output with ok=false (not an error)
    unless `fail_on_status` config says otherwise.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import FailureClass, FlowcraftError
from .base import Tool, ToolContext, ToolSpec, register_tool

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class HttpRequestTool(Tool):
    spec = ToolSpec(
        name="http.request",
        description="Outbound HTTP request guarded by the deployment network policy.",
        input_schema={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum":
                           ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
                "headers": {"type": "object"},
                "json_body": {"type": "object"},
                "query": {"type": "object"},
            },
        },
        output_schema={
            "type": "object",
            "required": ["ok", "status"],
            "properties": {
                "ok": {"type": "boolean"},
                "status": {"type": "integer"},
                "body": {},
                "json": {},
            },
        },
        config_schema={
            "type": "object",
            "properties": {
                "fail_on_status": {"type": "array", "items": {"type": "integer"}},
                "max_response_bytes": {"type": "integer"},
            },
        },
        permissions=["network.outbound"],
        idempotent=False,
        supports_idempotency_key=True,
        side_effects="write",
    )

    async def run(self, inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        url = inputs["url"]
        method = inputs.get("method", "GET").upper()
        headers = dict(inputs.get("headers") or {})
        for k in list(headers):
            if k.lower() in ("host", "transfer-encoding", "content-length"):
                raise FlowcraftError(
                    f"header {k!r} is controlled by the engine and cannot be set by workflows",
                    code="forbidden_header", failure_class=FailureClass.CONFIG)
        # Credential injection point: workflow references an env-var NAME, never a raw key.
        api_key_env = (inputs.get("headers") or {}).pop("api_key_env", None)
        if api_key_env:
            import os
            val = os.environ.get(str(api_key_env), "")
            if not val:
                raise FlowcraftError(f"credential env var {api_key_env!r} is not set",
                                     code="missing_credential",
                                     failure_class=FailureClass.CONFIG)
            headers["Authorization"] = f"Bearer {val}"

        ctx.network_policy.check(url)

        timeout = float(ctx.settings.default_provider_timeout_seconds)
        max_bytes = int((getattr(ctx, "step_config", None) or {}).get(
            "max_response_bytes", ctx.settings.http_max_response_bytes)) \
            if hasattr(ctx, "step_config") else ctx.settings.http_max_response_bytes

        idem_headers = dict(headers)
        if ctx.idempotency_key:
            idem_headers["Idempotency-Key"] = ctx.idempotency_key

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.request(
                    method, url, headers=idem_headers,
                    json=inputs.get("json_body"), params=inputs.get("query"))
        except httpx.TimeoutException as e:
            raise _conn_failure(f"request timed out after {timeout}s", ctx, method,
                                code="http_timeout") from e
        except httpx.HTTPError as e:
            raise _conn_failure(f"transport error: {type(e).__name__}", ctx, method,
                                code="http_transport") from e

        body_bytes = resp.content[:max_bytes]
        body_text = body_bytes.decode(errors="replace")
        try:
            parsed_json = resp.json()
        except Exception:
            parsed_json = None

        from ..security.redaction import redact

        out: dict[str, Any] = {
            "ok": 200 <= resp.status_code < 300,
            "status": resp.status_code,
            "body": redact(body_text),
        }
        if parsed_json is not None:
            out["json"] = redact(parsed_json)

        fail_on = getattr(ctx, "step_config", {}).get("fail_on_status", []) if hasattr(ctx, "step_config") else []
        if resp.status_code in fail_on:
            cls = FailureClass.RATE_LIMITED if resp.status_code == 429 else FailureClass.TRANSIENT
            raise _replay_guard(cls, f"downstream returned {resp.status_code}", ctx, method,
                                 code="http_status_fail")
        return out


def _replay_guard(cls: FailureClass, msg: str, ctx: ToolContext, method: str,
                  code: str) -> FlowcraftError:
    """Central retry-safety decision for unknown-effect failures."""
    safe = method in SAFE_METHODS
    keyed = bool(ctx.idempotency_key)
    if safe or keyed:
        return FlowcraftError(msg, code=code, failure_class=cls,
                              details={"replay_safe_because": "safe_method" if safe else "idempotency_key"})
    return FlowcraftError(
        msg + " — side effects unknown and no Idempotency-Key declared; "
              "engine refuses to retry (at-most-once guarantee)",
        code=code + "_guarded", failure_class=FailureClass.GUARDED,
        details={"how_to_fix": "declare input.headers/idempotency key or use a safe method"},
    )


def _conn_failure(msg: str, ctx: ToolContext, method: str, code: str) -> FlowcraftError:
    return _replay_guard(FailureClass.TRANSIENT, msg, ctx, method, code)


register_tool(HttpRequestTool())

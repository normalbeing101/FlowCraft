"""OpenAI-compatible chat-completions adapter.

Works with OpenAI, Azure-compatible gateways, OpenRouter, and any local
server exposing the /v1/chat/completions contract (Ollama's OpenAI endpoint,
LM Studio, llama.cpp server, vLLM). Endpoint and model are configuration.
Structured output uses response_format json_schema when advertised by the
deployment profile; otherwise callers fall back to client-side validation.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..errors import FailureClass, ProviderError
from .base import (
    GenerationRequest,
    GenerationResult,
    ProviderAdapter,
    ProviderCapabilities,
)


class OpenAICompatProvider(ProviderAdapter):
    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str,
        default_model: str,
        supports_structured_output: bool = False,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            ProviderCapabilities(
                chat=True,
                structured_output=supports_structured_output,
                temperature=True,
                max_output_tokens=True,
            )
        )
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key and self.base_url.startswith("https://api.openai.com"):
            raise ProviderError(
                f"credential env var {self.api_key_env!r} is not set",
                code="missing_credential",
                failure_class=FailureClass.CONFIG,
            )
        model = req.model or self.default_model
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_output_tokens:
            body["max_tokens"] = req.max_output_tokens
        if req.json_schema and self.capabilities.structured_output:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "step_output", "schema": req.json_schema,
                                "strict": True},
            }
        timeout = req.timeout_seconds or self.timeout_seconds
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions",
                                         json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise ProviderError(
                f"provider timed out after {timeout}s",
                code="provider_timeout", failure_class=FailureClass.TRANSIENT,
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(
                f"provider transport error: {type(e).__name__}",
                code="provider_unavailable", failure_class=FailureClass.TRANSIENT,
            ) from e

        if resp.status_code == 429:
            hint = _retry_after(resp.headers.get("retry-after"))
            raise ProviderError("provider rate limited",
                                code="rate_limited", failure_class=FailureClass.RATE_LIMITED,
                                retry_after=hint)
        if resp.status_code in (500, 502, 503, 504):
            raise ProviderError(f"provider server error {resp.status_code}",
                                code="provider_server_error",
                                failure_class=FailureClass.TRANSIENT)
        if resp.status_code in (401, 403):
            raise ProviderError(f"provider authentication failed ({resp.status_code})",
                                code="provider_auth", failure_class=FailureClass.PERMANENT)
        if 400 <= resp.status_code < 500:
            # Malformed request OR unsupported parameter — surface clearly.
            detail = _safe_detail(resp)
            raise ProviderError(
                f"provider rejected request ({resp.status_code}): {detail}",
                code="provider_rejected", failure_class=FailureClass.PERMANENT,
                details={"unsupported_check": "see message; verify model & parameters"},
            )

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError("provider returned malformed response body",
                                code="malformed_response",
                                failure_class=FailureClass.TRANSIENT) from e
        if not isinstance(text, str):
            raise ProviderError("provider message content was not a string",
                                code="malformed_response",
                                failure_class=FailureClass.TRANSIENT)
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = None  # never fabricate usage numbers
        return GenerationResult(text=text, model_used=data.get("model", model),
                                provider=self.name, usage=usage)


def _retry_after(v: str | None) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _safe_detail(resp: httpx.Response) -> str:
    """Extract an error message without echoing back anything that looks like
    a credential."""
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except Exception:
        msg = resp.text[:200]
    from ..security.redaction import scrub_text

    return scrub_text(str(msg))[:300]

"""Provider registry built from deployment configuration.

Profiles are declared via env vars (JSON blob) so nothing is hardcoded:

FLOWCRAFT_PROVIDERS_JSON='[{"name":"openai","type":"openai_compat",
  "base_url":"https://api.openai.com/v1","api_key_env":"OPENAI_API_KEY",
  "default_model":"gpt-4o-mini","supports_structured_output":true},
 {"name":"local","type":"openai_compat","base_url":"http://localhost:11434/v1",
  "api_key_env":"","default_model":"llama3.1"}]'

The `mock` provider always exists (no credentials, deterministic) so the
product is testable and demoable on a fresh install without any paid account.
"""

from __future__ import annotations

import json
import os

from ..errors import FlowcraftError, FailureClass
from .base import ProviderAdapter
from .openai_compat import OpenAICompatProvider


class MockProvider(ProviderAdapter):
    """Deterministic offline provider for tests/demos.

    Returns whatever JSON object is embedded in the last user message under
    marker `MOCK_REPLY:<json>` if present; otherwise echoes a canned structured
    reply. Never contacts the network; never reports usage."""

    name = "mock"

    def __init__(self) -> None:
        from .base import ProviderCapabilities

        super().__init__(ProviderCapabilities(chat=True, structured_output=False,
                                              temperature=False, max_output_tokens=False))

    async def generate(self, req):  # pragma: no cover - trivial
        from .base import GenerationResult

        for m in reversed(req.messages):
            if m.content.startswith("MOCK_REPLY:"):
                return GenerationResult(text=m.content[len("MOCK_REPLY:"):],
                                        model_used="mock", provider=self.name)
        return GenerationResult(text='{"verdict":"ok","summary":"mock reply"}',
                                model_used="mock", provider=self.name)


def build_providers() -> dict[str, ProviderAdapter]:
    providers: dict[str, ProviderAdapter] = {"mock": MockProvider()}
    raw = os.environ.get("FLOWCRAFT_PROVIDERS_JSON", "").strip()
    if raw:
        try:
            profiles = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FlowcraftError(
                f"FLOWCRAFT_PROVIDERS_JSON is not valid JSON: {e}",
                code="config_error", failure_class=FailureClass.CONFIG)
        for p in profiles:
            kind = p.get("type")
            name = p.get("name")
            if not name or not kind:
                raise FlowcraftError(
                    "provider profile requires 'name' and 'type'",
                    code="config_error", failure_class=FailureClass.CONFIG)
            if kind == "openai_compat":
                providers[name] = OpenAICompatProvider(
                    base_url=p["base_url"],
                    api_key_env=p.get("api_key_env", ""),
                    default_model=p["default_model"],
                    supports_structured_output=bool(p.get("supports_structured_output")),
                    timeout_seconds=float(p.get("timeout_seconds", 60)),
                )
            elif kind == "mock":
                providers[name] = MockProvider()
            else:
                raise FlowcraftError(
                    f"unknown provider type {kind!r} (available: openai_compat, mock)",
                    code="config_error", failure_class=FailureClass.CONFIG)
    return providers

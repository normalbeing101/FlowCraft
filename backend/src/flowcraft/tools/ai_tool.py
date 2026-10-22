"""ai.generate — invoke a configured provider profile.

Workflows reference a *profile name* (deployment config), never a raw model
or credential. Structured output: if the provider lacks native JSON-schema
support, the engine validates client-side and performs ONE deterministic
repair attempt by re-prompting with the validation errors. Model text is
treated as untrusted: it can never grant itself permissions or tools.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import FailureClass, FlowcraftError
from ..providers.base import ChatMessage, GenerationRequest
from .base import Tool, ToolContext, ToolSpec, register_tool


class AIGenerateTool(Tool):
    spec = ToolSpec(
        name="ai.generate",
        description="Call a configured AI provider profile with prompt(s).",
        input_schema={
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "system": {"type": "string"},
            },
        },
        output_schema={"type": "object"},  # shape defined per-step via config.output_schema
        config_schema={
            "type": "object",
            "required": ["provider"],
            "properties": {
                "provider": {"type": "string"},
                "model": {"type": "string"},          # optional override of profile default
                "temperature": {"type": "number"},
                "max_output_tokens": {"type": "integer"},
                "output_schema": {"type": "object"},  # require structured JSON output
            },
        },
        permissions=["ai.provider"],
        idempotent=True,   # generation is replay-safe; duplicates are harmless reads
        side_effects="read",
    )

    async def run(self, inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        cfg = ctx.extras.get("config", {})
        pname = cfg["provider"]
        provider = ctx.providers.get(pname)
        if provider is None:
            raise FlowcraftError(
                f"provider profile {pname!r} is not configured in this deployment; "
                f"available: {sorted(ctx.providers)}",
                code="unknown_provider", failure_class=FailureClass.CONFIG)

        schema = cfg.get("output_schema")
        messages = []
        if inputs.get("system"):
            messages.append(ChatMessage("system", inputs["system"]))
        messages.append(ChatMessage("user", inputs["prompt"]))

        req = GenerationRequest(
            messages=messages,
            model=cfg.get("model"),
            temperature=cfg.get("temperature"),
            max_output_tokens=cfg.get("max_output_tokens"),
            timeout_seconds=float(ctx.settings.default_provider_timeout_seconds),
            json_schema=schema,
        )
        notes = provider.validate_request(req)

        result = await provider.generate(req)
        meta = {"model_used": result.model_used, "provider": pname,
                "usage_reported": result.usage,   # null unless provider reported it
                "capability_notes": notes}

        if not schema:
            return {"text": result.text, "meta": meta}

        parsed, errs = _parse_and_validate(result.text, schema)
        if errs:
            # One bounded, deterministic repair attempt (client-side enforcement).
            repair_msgs = list(messages) + [
                ChatMessage("assistant", result.text),
                ChatMessage("user",
                            "Your previous reply was not valid for this JSON schema: "
                            f"{json.dumps(errs)}. Reply again with ONLY a valid JSON object."),
            ]
            result = await provider.generate(GenerationRequest(
                messages=repair_msgs, model=req.model,
                timeout_seconds=req.timeout_seconds, json_schema=None))
            parsed, errs = _parse_and_validate(result.text, schema)
            if errs:
                raise FlowcraftError(
                    "model output failed schema validation after one repair attempt",
                    code="malformed_model_output", failure_class=FailureClass.TRANSIENT,
                    details={"errors": errs[:5]})
        return {"data": parsed, "raw_text": result.text, "meta": meta}


def _parse_and_validate(text: str, schema: dict) -> tuple[Any, list[str]]:
    from .base import _jsonschema_errors

    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    try:
        data = json.loads(t)
    except ValueError:
        # tolerate leading prose before first { ... last }
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            return None, ["reply was not JSON"]
        try:
            data = json.loads(t[s:e + 1])
        except ValueError:
            return None, ["reply contained unparseable JSON"]
    if not isinstance(data, dict):
        return None, ["top-level JSON value must be an object"]
    errs = _jsonschema_errors(data, schema)
    return (data if not errs else None), errs


register_tool(AIGenerateTool())

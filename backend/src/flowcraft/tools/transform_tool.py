"""data.transform — pure, deterministic data reshaping (no code execution).

Supports a fixed operation vocabulary applied in order:
  pick / rename / set_default / lowercase / wrap_list_field
Outputs are validated against the tool's output schema by the engine.
"""

from __future__ import annotations

from typing import Any

from ..errors import FailureClass, FlowcraftError
from .base import Tool, ToolContext, ToolSpec, register_tool


class DataTransformTool(Tool):
    spec = ToolSpec(
        name="data.transform",
        description="Deterministic field selection/rename/defaulting. No arbitrary code.",
        input_schema={"type": "object", "required": ["data"]},
        output_schema={"type": "object", "required": ["result"]},
        config_schema={
            "type": "object",
            "required": ["operations"],
            "properties": {
                "operations": {"type": "array", "minItems": 1, "items": {"type": "object",
                             "required": ["op"], "properties": {
                                 "op": {"type": "string",
                                        "enum": ["pick", "rename", "set_default", "lowercase"]},
                                 "fields": {"type": "array", "items": {"type": "string"}},
                                 "from": {"type": "string"}, "to": {"type": "string"},
                                 "field": {"type": "string"}, "value": {}}}},
            },
        },
        permissions=[],
        idempotent=True,          # pure function => always replay-safe
        side_effects="none",
    )

    async def run(self, inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        data = inputs["data"]
        if not isinstance(data, (dict, list)):
            raise FlowcraftError("transform input 'data' must be object or array",
                                 code="bad_input", failure_class=FailureClass.PERMANENT)
        cfg_ops = ctx.extras.get("config", {}).get("operations", [])
        if not cfg_ops:
            raise FlowcraftError("no operations configured for this step",
                                 code="bad_config", failure_class=FailureClass.CONFIG)
        result = data
        for op in cfg_ops:
            result = self._apply(op, result)
        return {"result": result}

    def _apply(self, op: dict[str, Any], data: Any) -> Any:
        kind = op["op"]
        items = data if isinstance(data, list) else [data]
        out = []
        for item in items:
            if not isinstance(item, dict):
                raise FlowcraftError("transform operations require object items",
                                     code="bad_input", failure_class=FailureClass.PERMANENT)
            if kind == "pick":
                item = {k: item[k] for k in op.get("fields", []) if k in item}
            elif kind == "rename":
                f, t = op["from"], op["to"]
                if f in item:
                    item = {t if k == f else k: v for k, v in item.items()}
            elif kind == "set_default":
                f = op["field"]
                if f not in item or item[f] in (None, ""):
                    item = {**item, f: op.get("value")}
            elif kind == "lowercase":
                f = op["field"]
                if isinstance(item.get(f), str):
                    item = {**item, f: item[f].lower()}
            out.append(item)
        return out if isinstance(data, list) else out[0]


register_tool(DataTransformTool())

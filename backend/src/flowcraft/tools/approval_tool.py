"""approval.request — human-in-the-loop gate.

The engine pauses the run (status `awaiting_approval`) BEFORE executing the
step body; this tool's `run` only executes once an operator has recorded a
decision through the API/CLI. Authorization is enforced in engine code —
a model output can never approve itself (see engine approval handling).
"""

from __future__ import annotations

from typing import Any

from ..errors import FailureClass, FlowcraftError
from .base import Tool, ToolContext, ToolSpec, register_tool


class ApprovalTool(Tool):
    spec = ToolSpec(
        name="approval.request",
        description="Pause the run until a human approves or rejects via API/CLI.",
        input_schema={"type": "object",
                      "properties": {"summary": {"type": "string"},
                                     "payload": {"type": "object"}}},
        output_schema={"type": "object", "required": ["approved"],
                       "properties": {"approved": {"type": "boolean"},
                                      "decided_by": {"type": "string"},
                                      "comment": {"type": "string"}}},
        config_schema={},
        permissions=["human.approval"],
        idempotent=True,     # decision is stored once; replays read the record
        side_effects="none",
    )

    async def run(self, inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        decision = ctx.extras.get("approval_decision")
        if decision is None:
            # Engine bug guard: should never execute without a decision.
            raise FlowcraftError("approval step executed before a decision was recorded",
                                 code="approval_not_resolved",
                                 failure_class=FailureClass.CONFIG)
        approved = bool(decision.get("approved"))
        if not approved and decision.get("strict", True):
            raise FlowcraftError(
                f"human rejected the action: {decision.get('comment', '')}"[:300],
                code="approval_rejected", failure_class=FailureClass.PERMANENT,
                details={"decided_by": decision.get("decided_by")})
        return {"approved": approved,
                "decided_by": str(decision.get("decided_by", "")),
                "comment": str(decision.get("comment", ""))}


register_tool(ApprovalTool())

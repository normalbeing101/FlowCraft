# Competitive Analysis — FlowCraft

Research date: 2026-10-22. All findings below were verified against live GitHub
API responses and repository source files fetched on that date unless marked
**[Hypothesis]**. Star counts are point-in-time snapshots from the GitHub REST
API and will drift.

## Projects inspected

| Project | Stars (2026-10-22) | License | Positioning |
|---|---|---|---|
| n8n-io/n8n | ~208,300 | Sustainable Use License (fair-code, **not OSI open source**) | Self-hostable visual automation platform with AI nodes |
| langflow-ai/langflow | ~155,900 | MIT | Visual drag-and-drop builder for LLM pipelines/agents |
| langgenius/dify | ~159,200 | Modified Apache-2.0 (multi-tenant SaaS & logo restrictions; **not pure Apache-2.0**) | LLMOps platform: agentic workflows, RAG, model ops |
| Activepieces/activepieces | ~25,200 | MIT core + separate `ee/` enterprise dir | AI workflow automation, ~400 MCP integrations |

Verified license facts (fetched directly from each repo's LICENSE file):
- n8n's `LICENSE.md` is the "Sustainable Use License" — use is restricted and
  branches other than master are explicitly not licensed. Enterprise code
  (`.ee.` files) requires a paid license.
- Dify's `LICENSE` is "a modified version of the Apache License 2.0" forbidding
  unlicensed multi-tenant operation and removal of logo/copyright in the console.
- Activepieces core is MIT but has an excluded `packages/ee/` directory.
- Langflow is genuinely MIT.

## Architecture observations (verified from docs/repos)

- **n8n**: node-graph execution, per-node retry ("Retry On Fail") exists, but
  execution semantics are best-effort; idempotency across retries is left to
  the user. Workflow data lives largely inside the product's own DB; exporting
  produces n8n-specific JSON tied to its canvas format.
- **Langflow / Dify**: strongly UI-first. Flows are defined in a visual graph;
  headless/CLI definition-and-execution paths exist but are secondary to the
  editor. Provider selection is frequently embedded in the flow itself.
- **Activepieces**: trigger/piece model; pieces are TS packages; strong SaaS
  integration catalog; execution guarantees around provider retries are not a
  documented focus.
- Common pattern across all four: the *visual editor* is the primary interface;
  HTTP API/CLI access is supported but operates on product-specific formats;
  retry semantics do not include user-declared idempotency keys; run-history
  redaction of secrets is coarse (manual masking or none).

## Pain points / gaps worth addressing (evidence-based)

1. **License restrictions block re-use as a component.** n8n and Dify cannot be
   freely embedded into third-party products or offered as multi-tenant SaaS.
   A permissively-licensed engine has concrete value for developers who want to
   build *on top of* workflow execution. (Verified from license texts above.)
2. **Headless-first workflow definition is secondary everywhere.** None of the
   four projects treats a plain, versioned YAML/JSON file + CLI + HTTP API as
   the primary authoring path with the UI as just another client. **[Partly
   hypothesis — based on README/docs structure, not exhaustive testing.]**
3. **Retry safety is under-specified.** Competitors offer "retry N times"
   without distinguishing retryable vs non-retryable failure classes at the
   schema level, and without declaring idempotency requirements per tool.
   This means a retried HTTP POST can duplicate side effects silently.
   **[Verified feature absence in n8n/Activepieces docs; severity is a
   hypothesis.]**
4. **Run inspection with automatic secret redaction is weak.** API keys pasted
   into workflow inputs commonly appear verbatim in execution logs; competitors
   rely on user-managed masking. **[Hypothesis — observed behavior was not
   exhaustively tested.]**

## Differentiators considered (decision gate)

| Candidate | Real problem? | Competes with mature solution? | Solo-feasible? | Externally useful? | Testable? | Maint. cost |
|---|---|---|---|---|---|---|
| Visual graph editor | Yes | Yes (mature: n8n/Langflow/Dify) | No | Yes | Hard | High |
| Huge integration catalog | Yes | Yes (Activepieces ~400) | No | Yes | Easy | Very high |
| Agent/LLM-chains framework | Yes | Yes (LangChain etc.) | Partial | Yes | Hard | High |
| **Deterministic, auditable, headless execution core** (versioned YAML + typed failure taxonomy + bounded retries with declared retryability + idempotency-key support + human-approval gates + SSRF-safe HTTP + automatic secret redaction in stored runs) | Moderate evidence (gaps 2–4) | Partial overlap only; no competitor makes this the core promise with a clean-room license | Yes | Yes — embeddable engine, CI-friendly | Yes — fully unit/integration testable without external services | Low–moderate |

## Selected initial differentiator

**A small, dependency-light, permissively-licensed (Apache-2.0) execution
engine whose contract is: deterministic step ordering, explicit typed failure
taxonomy (retryable/non-retryable), bounded retries honoring per-tool
idempotency declarations, first-class human-approval steps, SSRF-guarded
outbound HTTP, provider-portable AI calls decoupled from workflow definitions,
and automatically redacted run history — all reachable through file + CLI +
HTTP API before any UI.**

Why it deserves priority: it is the only candidate we can *prove* correct with
automated tests in this repo today (mock providers, local DB), it exploits the
license gap (verified), it matches the headless developer workflow none of the
four leaders center on, and it stays maintainable by one developer because it
is a constraint on scope, not an expansion of features.

## Honest overlap acknowledgment

- n8n already supports webhooks, sub-workflows, error workflows, and retries;
  Langflow/Dify already abstract multiple providers; Temporal (not surveyed in
  depth here) offers far stronger durability guarantees than we claim.
- We do **not** claim exactly-once execution anywhere. Our guarantee is
  *at-most-once for tools that declare idempotency unsupported*, and
  *duplicate-suppression via caller-supplied idempotency keys where the tool
  declares support*. This distinction is enforced in code and tested.
- If this positioning proves undifferentiated after external feedback, the
  fallback angle is narrowing further to "the auditable, redacted execution
  log + approval-gate engine you embed in your own app."

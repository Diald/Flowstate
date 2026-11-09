# Implementation plan and milestone status

## Repository reconnaissance (Milestone 0) — complete
- The requested directory was empty: no framework, code, conventions, or tests existed to preserve.
- Selected React + TypeScript + Vite + React Flow and FastAPI + Pydantic.
- Used SQLite for a runnable local first slice; PostgreSQL and migrations remain a hardening step.

## Initial vertical slice (Milestones 1–3) — local v1 complete
- Workflow CRUD, graph persistence, visual editor, validation, synchronous run execution, node records, run inspection, mock LLM, and sample workflow.
- CORS now supports both local frontend host spellings. Existing graphs saved with the old React Flow `workflow` type are normalized server-side.

## Milestone 4 — provider and control flow slice
- **Gemini provider adapter:** implemented behind a provider-neutral async protocol and registry. New adapters can be registered without changing the interpreter. API keys are read server-side from `GEMINI_API_KEY` or `GOOGLE_API_KEY`; errors omit response bodies and credential-bearing request details.
- **Condition:** safe equality, inequality, numeric comparisons, truthiness, falsiness, existence checks, and optional dot paths. Explicit true/false handles control which branch executes; inactive branch nodes are persisted as skipped.
- **Loop:** bounded collection mapping with configurable item template and index; hard server maximum 20; per-iteration input/output is recorded in node logs. This is intentionally not a cyclic subgraph interpreter.
- **Validation:** provider, condition operator/ports, node count, graph cycles, and loop limits are checked server-side.
- **Tests:** covered legacy graph normalization, CORS, CRUD/mock execution, branch selection, loop capping, and provider response handling.

## Runtime semantics
- Acyclic graph scheduled in topological order. A normal node activates all outgoing edges; a Condition activates only edges whose source handle matches its selected branch. A node executes when at least one incoming edge is active; inactive nodes are recorded as skipped.
- Loop is an explicit list map, not graph recursion. Each iteration stores its index, input, and mapped output in the loop node’s execution log.
- The graph remains provider-neutral. `LLMProvider` is the adapter contract and `ProviderRegistry` selects a provider by configured identifier. Gemini is the initial implementation; Mock LLM remains deterministic and keyless.

## Known limitations / next milestones
- Agent, Python and HTTP nodes remain unavailable. HTTP SSRF controls, arbitrary code isolation, auth/tenant ownership, PostgreSQL/Alembic, versions, cancellation, background/polling execution, retry policy, and run event streaming remain future work.
- SQLite and synchronous request execution are local-development choices, not production multi-user execution infrastructure.
- Frontend build depends on Node.js/npm; backend runtime tests run with the project virtual environment.

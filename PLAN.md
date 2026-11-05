# Initial implementation plan

## Reconnaissance (Milestone 0)
- The requested directory was empty: no language, framework, conventions, code, or tests existed to preserve.
- Use React + TypeScript + Vite + React Flow for the editor and FastAPI + Pydantic for the API.
- Start with SQLite so the first local slice runs without external services; keep persistence behind a small repository boundary and document PostgreSQL as the next hardening step.

## Vertical slice (Milestones 1–3, initial v1)
1. Local API with workflow CRUD, graph validation, synchronous run execution, persisted runs and node records.
2. React Flow editor with node palette, editable node config, graph save/load, run input, and run inspection.
3. Sample workflow, setup instructions, and runtime tests.

## Runtime semantics and limits
- Execute a validated acyclic graph in topological order; node outputs are exposed as `nodes.<id>.<output>` template values.
- The first executable types are Input, Mock LLM, Condition, and Output. Python, Agent, Loop, and HTTP are explicitly unavailable in this slice; adding unsafe pseudo-support would misrepresent their behavior.
- The Mock LLM is deterministic and keyless. Real provider integration, safe HTTP destination validation, and isolated Python require dedicated follow-up work.
- Runs execute synchronously in the API process and have node-count/output-size caps. SQLite is appropriate for local development, not multi-user production.
- Local development uses a fixed local owner boundary; authentication, PostgreSQL migrations, cancellation, and durable async execution remain follow-up milestones.

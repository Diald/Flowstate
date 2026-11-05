# Flowstate — Visual AI Workflow Studio (v1)

A local developer tool for creating a small workflow graph, saving it, running it, and inspecting node results. This is an initial working vertical slice, not the full production MVP in the build specification.

## Quick start

Requirements: Python 3.11+ and Node.js 20+.

```sh
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

In another terminal:

```sh
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. The API is at http://127.0.0.1:8000 and its OpenAPI page at `/docs`. SQLite data is stored in `backend/workflow.db`. Set `WORKFLOW_DB` to use another local path.

## Build a demo

Create a workflow, connect Input → Mock LLM → Output, save, enter JSON such as `{"text":"workflow orchestration"}`, and run. Select **Run inspector** to view the timeline and click a timeline row to select its node. The included sample graph is in `examples/sample-workflow.json`.

## Available nodes

- **Input:** provides the supplied JSON object as `value`.
- **Mock LLM:** deterministic, no key required; renders `{{nodes.<id>.<field>}}` and prefixes the text with `Mock response:`.
- **Condition:** compares its connected input with a configured value and emits a boolean and branch label. The initial interpreter is linear DAG execution, so it does not yet suppress downstream execution by branch.
- **Output:** resolves a template or passes through its connected input.

Agent, Loop, Python, and HTTP Request are shown as unavailable. No unsafe code execution or unrestricted network access is implied. Real model providers, safe HTTP/SSRF controls, loop execution, full conditional routing, authentication, PostgreSQL migrations, background execution, cancellation, and immutable workflow versions are follow-up work.

## API

- `GET /api/health`, `GET /api/node-types`
- Workflow CRUD at `/api/workflows`; `POST /{id}/duplicate`
- `POST /api/workflows/{id}/validate`
- `POST /api/workflows/{id}/runs`
- `GET /api/runs/{id}`

All data is local and single-user. Do not expose this development server to untrusted networks. No credentials are stored by the mock provider.

## Tests

```sh
cd backend
pip install -r requirements.txt
pytest
```

Frontend build: `cd frontend && npm install && npm run build`.

## Architecture

- `backend/app/main.py`: FastAPI routes, SQLite persistence, server-side graph validation, deterministic topological interpreter.
- `frontend/src/App.tsx`: workflow list, React Flow editor, node settings, test-input panel, run inspector.
- `PLAN.md`: reconnaissance, milestone sequence, execution semantics, assumptions, and risks.

### Runtime limits and trade-offs

The interpreter rejects cycles and graphs above 50 nodes. It caps serialized final output at 256 KB. Runs execute synchronously and are persisted after completion; this means in-progress state and crash recovery are not implemented. SQLite and fixed local ownership are for local development only. Do not treat this build as a multi-tenant production executor.

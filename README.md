# Flowstate — Visual AI Workflow Studio

A local developer tool for authoring a workflow graph, running it, and inspecting node results. Providers run on the backend; the browser only stores workflow configuration, never provider credentials.

## Quick start

Requirements: Python 3.11+ and Node.js 20+.

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

To run a real Gemini node, set `GEMINI_API_KEY` in the backend terminal before starting the API:

```sh
export GEMINI_API_KEY="your-key"
python -m app
```

Keep the key out of workflow definitions and frontend environment variables. Mock LLM workflows work without a key.

In another terminal:

```sh
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. The API is at http://127.0.0.1:8000 and its OpenAPI page is `/docs`. SQLite data is stored in `backend/workflow.db`; set `WORKFLOW_DB` to use another local path.

## Available nodes

- **Input:** exposes the supplied JSON object as `value`.
- **Mock LLM:** deterministic, no key required; resolves `{{nodes.<id>.<field>}}` prompt references and prefixes the text with `Mock response:`.
- **LLM:** provider-neutral runtime contract, initially with the Gemini Interactions API adapter. Configure provider, model, prompt, system instructions, temperature, and output-token limit. The server clamps model timeouts to 120 seconds and output tokens to 8,192. The provider adapter reports safe provider/model/latency and token metadata where returned.
- **Condition:** supports safe `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `truthy`, `falsy`, and `exists` comparisons. Optional dot-path lookup reads into connected JSON. Connect the `true` and `false` handles to the corresponding paths. Only the selected path runs; nodes on the other path are marked skipped.
- **Loop:** maps a collection through an item template. Use `{{item}}` and `{{index}}`. Server-side limit is 20 iterations; the default is 3. Each iteration’s input/output is stored in the node execution log. This loop is an explicit bounded collection map; it does not execute a cyclic subgraph.
- **Output:** resolves a configured template or passes through the connected input when the value field is blank.

Agent, arbitrary Python, and HTTP Request remain unavailable. We do not claim to sandbox user code or provide SSRF-safe network calls.

## Provider extension point

`backend/app/providers/base.py` defines the provider-neutral protocol and result contract. `backend/app/providers/registry.py` registers adapters by name; `backend/app/providers/gemini.py` implements the first adapter. Add another vendor by implementing `LLMProvider` and registering it, without changing graph traversal or the editor’s execution engine. Provider secrets stay in backend environment variables.

## Demo

Create a workflow, connect Input → Mock LLM → Output, save, enter JSON such as `{"text":"workflow orchestration"}`, and run. Select **Run inspector** to view the node timeline and final output. To use Gemini, add an LLM node, set the backend `GEMINI_API_KEY`, save, and run. The included sample graph is in `examples/sample-workflow.json`.

## API

- `GET /api/health`, `GET /api/node-types`
- Workflow CRUD at `/api/workflows`; `POST /{id}/duplicate`
- `POST /api/workflows/{id}/validate`
- `POST /api/workflows/{id}/runs`
- `GET /api/runs/{id}`

## Tests and build

```sh
cd backend
pip install -r requirements.txt
pytest
```

Frontend: `cd frontend && npm install && npm run build`.

## Runtime limits and trade-offs

The server rejects graph cycles, graphs above 50 nodes, invalid condition ports, unsupported comparisons, and loop limits outside 1–20. Final run output is capped at 256 KB. Runs execute synchronously and are persisted after completion, so in-progress updates, cancellation, and crash recovery are not implemented. SQLite and fixed local ownership are for local development only; do not expose this development server to untrusted networks. Workflow versions, authentication, PostgreSQL migrations, durable asynchronous execution, secret-store references, HTTP SSRF controls, Agent tools, and isolated Python remain future work.

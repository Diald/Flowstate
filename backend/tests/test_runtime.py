from fastapi.testclient import TestClient
from app.main import app, validate_graph


def graph():
    return {"schema_version": "1.0", "nodes": [
        {"id": "input", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "Input", "kind": "input", "config": {}}},
        {"id": "llm", "type": "mock_llm", "position": {"x": 200, "y": 0}, "data": {"label": "Mock LLM", "kind": "mock_llm", "config": {"prompt": "Hello {{nodes.input.value.name}}"}}},
        {"id": "output", "type": "output", "position": {"x": 400, "y": 0}, "data": {"label": "Output", "kind": "output", "config": {"value": "{{nodes.llm.text}}"}}}],
        "edges": [{"id": "a", "source": "input", "target": "llm"}, {"id": "b", "source": "llm", "target": "output"}]}


def test_validator_rejects_cycles_and_unavailable_nodes():
    definition = graph()
    definition["edges"].append({"id": "c", "source": "output", "target": "input"})
    assert any("Cycles" in e for e in validate_graph(definition))
    definition = graph()
    definition["nodes"][1]["type"] = "python"
    assert any("unavailable" in e for e in validate_graph(definition))


def test_workflow_crud_and_run(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "test.db")
    with TestClient(app) as client:
        created = client.post("/api/workflows", json={"name": "Demo", "description": "", "definition": graph()}).json()
        assert client.get(f"/api/workflows/{created['id']}").status_code == 200
        run = client.post(f"/api/workflows/{created['id']}/runs", json={"input": {"name": "Ada"}}).json()
        assert run["status"] == "succeeded"
        assert run["output"]["value"] == "Mock response: Hello Ada"
        inspected = client.get(f"/api/runs/{run['id']}").json()
        assert len(inspected["nodes"]) == 3
        assert client.delete(f"/api/workflows/{created['id']}").json()["deleted"]


def test_local_frontend_cors_preflight():
    with TestClient(app) as client:
        response = client.options(
            "/api/workflows",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_legacy_react_flow_workflow_type_can_run(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "legacy.db")
    legacy = graph()
    for node in legacy["nodes"]:
        node["type"] = "workflow"
    with TestClient(app) as client:
        created = client.post("/api/workflows", json={"name": "Legacy", "description": "", "definition": legacy}).json()
        valid = client.post(f"/api/workflows/{created['id']}/validate").json()
        assert valid["valid"]
        run = client.post(f"/api/workflows/{created['id']}/runs", json={"input": {"name": "Ada"}}).json()
        assert run["status"] == "succeeded"
        assert run["output"]["value"] == "Mock response: Hello Ada"


def test_condition_only_runs_selected_branch(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "branch.db")
    definition = {
        "schema_version": "1.0",
        "nodes": [
            {"id": "input", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "Input", "kind": "input", "config": {}}},
            {"id": "condition", "type": "condition", "position": {"x": 150, "y": 0}, "data": {"label": "Condition", "kind": "condition", "config": {"path": "approved", "operator": "eq", "value": True}}},
            {"id": "yes", "type": "output", "position": {"x": 300, "y": -60}, "data": {"label": "Approved", "kind": "output", "config": {"value": "{{nodes.condition.true}}"}}},
            {"id": "no", "type": "output", "position": {"x": 300, "y": 60}, "data": {"label": "Rejected", "kind": "output", "config": {"value": "{{nodes.condition.false}}"}}},
        ],
        "edges": [
            {"id": "a", "source": "input", "sourceHandle": "value", "target": "condition", "targetHandle": "input"},
            {"id": "b", "source": "condition", "sourceHandle": "true", "target": "yes", "targetHandle": "input"},
            {"id": "c", "source": "condition", "sourceHandle": "false", "target": "no", "targetHandle": "input"},
        ],
    }
    with TestClient(app) as client:
        workflow = client.post("/api/workflows", json={"name": "Branch", "description": "", "definition": definition}).json()
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {"approved": True}}).json()
        false_run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {"approved": False}}).json()
    assert run["status"] == "succeeded"
    assert run["output"]["value"] == {"approved": True}
    assert next(n for n in run["nodes"] if n["node_id"] == "yes")["status"] == "succeeded"
    assert next(n for n in run["nodes"] if n["node_id"] == "no")["status"] == "skipped"
    assert false_run["output"]["value"] == {"approved": False}
    assert next(n for n in false_run["nodes"] if n["node_id"] == "yes")["status"] == "skipped"
    assert next(n for n in false_run["nodes"] if n["node_id"] == "no")["status"] == "succeeded"


def test_loop_is_bounded_and_records_iterations(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "loop.db")
    definition = {
        "schema_version": "1.0",
        "nodes": [
            {"id": "input", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "Input", "kind": "input", "config": {}}},
            {"id": "loop", "type": "loop", "position": {"x": 150, "y": 0}, "data": {"label": "Loop", "kind": "loop", "config": {"collection": "{{nodes.input.value.items}}", "item_template": "item={{item}} at {{index}}", "max_iterations": 2}}},
            {"id": "output", "type": "output", "position": {"x": 300, "y": 0}, "data": {"label": "Output", "kind": "output", "config": {"value": "{{nodes.loop.value}}"}}},
        ],
        "edges": [
            {"id": "a", "source": "input", "sourceHandle": "value", "target": "loop", "targetHandle": "input"},
            {"id": "b", "source": "loop", "sourceHandle": "value", "target": "output", "targetHandle": "input"},
        ],
    }
    with TestClient(app) as client:
        workflow = client.post("/api/workflows", json={"name": "Bounded loop", "description": "", "definition": definition}).json()
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {"items": ["a", "b", "c"]}}).json()
    assert run["status"] == "succeeded"
    assert run["output"]["value"] == ["item=a at 0", "item=b at 1"]
    loop_record = next(n for n in run["nodes"] if n["node_id"] == "loop")
    assert loop_record["outputs"]["stop_reason"] == "max_iterations"
    assert [entry["iteration"] for entry in loop_record["logs"] if isinstance(entry, dict) and "iteration" in entry] == [1, 2]


def test_invalid_loop_limit_is_rejected():
    definition = graph()
    loop = {"id": "loop", "type": "loop", "data": {"label": "Loop", "kind": "loop", "config": {"max_iterations": 100}}}
    definition["nodes"].append(loop)
    assert any("max_iterations" in error for error in validate_graph(definition))


def test_gemini_provider_returns_provider_neutral_result(monkeypatch):
    from app.providers.gemini import GeminiProvider
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")
    monkeypatch.setattr(GeminiProvider, "_request_json", staticmethod(lambda request, timeout: {"output_text": "hello", "usage": {"input_tokens": 3, "output_tokens": 2}}))
    import asyncio
    result = asyncio.run(GeminiProvider().generate(prompt="say hello", system_prompt="", model="test-model", temperature=1, max_output_tokens=100, timeout_seconds=2))
    assert (result.text, result.provider, result.model) == ("hello", "gemini", "test-model")
    assert (result.input_tokens, result.output_tokens) == (3, 2)


def test_llm_provider_metadata_is_persisted(monkeypatch, tmp_path):
    from app.main import providers
    from app.providers.base import ProviderResult

    class FakeProvider:
        name = "fake"

        async def generate(self, **kwargs):
            return ProviderResult("provider answer", "fake", kwargs["model"], 12, 5, 2)

    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "provider-run.db")
    monkeypatch.setitem(providers._providers, "fake", FakeProvider())
    definition = graph()
    definition["nodes"][1]["type"] = "llm"
    definition["nodes"][1]["data"]["kind"] = "llm"
    definition["nodes"][1]["data"]["config"] = {"provider": "fake", "model": "test-model", "prompt": "Hello"}
    definition["nodes"][1]["data"]["label"] = "LLM"
    definition["edges"][1]["sourceHandle"] = "text"
    with TestClient(app) as client:
        workflow = client.post("/api/workflows", json={"name": "Provider", "description": "", "definition": definition}).json()
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {}}).json()
        inspected = client.get(f"/api/runs/{run['id']}").json()
    llm_record = next(n for n in inspected["nodes"] if n["node_id"] == "llm")
    assert llm_record["provider_metadata"] == {"provider": "fake", "model": "test-model", "latency_ms": 12, "input_tokens": 5, "output_tokens": 2}


def test_provider_registry_endpoint_is_extensible():
    with TestClient(app) as client:
        response = client.get("/api/node-types").json()
    assert "gemini" in response["providers"]
    assert "llm" in response["available"]
    assert "loop" in response["available"]


def test_output_node_passes_through_connected_llm_value(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "passthrough.db")
    definition = graph()
    definition["nodes"][2]["data"]["config"] = {}
    with TestClient(app) as client:
        workflow = client.post("/api/workflows", json={"name": "Pass through", "description": "", "definition": definition}).json()
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": {"name": "Ada"}}).json()
    assert run["output"]["value"] == "Mock response: Hello Ada"

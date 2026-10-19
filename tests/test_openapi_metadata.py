"""Тесты enterprise-метаданных Swagger UI (OpenAPI-спека src/main.py)."""

from __future__ import annotations

import pytest

from src.main import app


@pytest.fixture(scope="module")
def spec() -> dict:
    return app.openapi()


class TestInfo:
    def test_title_version(self, spec: dict) -> None:
        assert spec["info"]["title"] == "AI DevOps Agent API"
        assert spec["info"]["version"] == "1.0.0"

    def test_contact_and_license(self, spec: dict) -> None:
        assert spec["info"]["contact"]["name"] == "AI DevOps Team"
        assert "Mkft1/ai_agent_DevOps" in spec["info"]["contact"]["url"]
        assert spec["info"]["license"]["name"] == "MIT"

    def test_description_mentions_llm_rag_guardrails(self, spec: dict) -> None:
        desc = spec["info"]["description"].lower()
        for keyword in ("llm", "rag", "prometheus", "read-only", "rca"):
            assert keyword in desc


class TestTagsAndOperations:
    def test_endpoint_tags(self, spec: dict) -> None:
        paths = spec["paths"]
        assert paths["/health"]["get"]["tags"] == ["Health & Status"]
        assert paths["/ready"]["get"]["tags"] == ["Health & Status"]
        assert paths["/diagnose"]["post"]["tags"] == ["Diagnostics"]
        assert paths["/webhook/alertmanager"]["post"]["tags"] == ["Webhooks"]

    def test_metrics_excluded_from_schema(self, spec: dict) -> None:
        assert "/metrics" not in spec["paths"]

    @pytest.mark.parametrize(
        ("path", "method", "summary"),
        [
            ("/health", "get", "Liveness probe"),
            ("/ready", "get", "Readiness probe"),
            ("/diagnose", "post", "Diagnose Incident"),
            ("/webhook/alertmanager", "post", "Alertmanager webhook"),
        ],
    )
    def test_summaries_and_descriptions(self, spec: dict, path, method, summary) -> None:
        op = spec["paths"][path][method]
        assert op["summary"] == summary
        assert len(op["description"]) > 20

    def test_tag_metadata_present(self, spec: dict) -> None:
        names = {t["name"] for t in spec.get("tags", [])}
        assert {"Health & Status", "Diagnostics", "Webhooks"} <= names


class TestSchemaDocs:
    def test_request_fields_have_descriptions_and_examples(self, spec: dict) -> None:
        props = spec["components"]["schemas"]["DiagnoseRequest"]["properties"]
        for field in ("namespace", "pod_name", "deployment_name", "description"):
            assert props[field].get("description"), field
        assert props["namespace"].get("examples") == ["default"]

    def test_alertmanager_payload_documented(self, spec: dict) -> None:
        schemas = spec["components"]["schemas"]
        alert = schemas["AlertmanagerAlert"]["properties"]
        assert alert["labels"]["description"]
        assert alert["startsAt" if "startsAt" in alert else "starts_at"].get("examples")
        payload = schemas["AlertmanagerPayload"]["properties"]
        assert payload["alerts"]["description"]

    def test_swagger_ui_theme(self) -> None:
        assert app.swagger_ui_parameters.get("syntaxHighlight.theme") == "obsidian"

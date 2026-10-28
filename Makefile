PYTHON=python3
PIP=pip

.PHONY: install lint test run docker-build chaos e2e jaeger eval dashboard helm-lint helm-install docs-check

install:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

lint:
	ruff check src tests scripts eval ui

test:
	pytest -n auto --cov=src --cov-report=term-missing tests/

run:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

docker-build:
	docker build -t ai-agent-devops:local .

chaos:
	$(PYTHON) scripts/chaos_test.py

e2e:
	bash scripts/run_integration_test.sh

jaeger:
	docker rm -f jaeger 2>/dev/null || true
	docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
	@echo "Jaeger UI: http://localhost:16686 (OTLP gRPC: 4317)"

eval:
	$(PYTHON) eval/evaluate.py

dashboard:
	streamlit run ui/dashboard.py

helm-lint:
	helm lint charts/ai-agent

helm-install:
	helm upgrade --install ai-agent charts/ai-agent -f charts/ai-agent/values.yaml

docs-check:
	python scripts/check_docs_metrics.py

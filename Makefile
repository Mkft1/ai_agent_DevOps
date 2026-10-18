PYTHON=python3
PIP=pip

.PHONY: install lint test run docker-build chaos e2e

install:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

lint:
	ruff check src tests scripts

test:
	pytest --cov=src --cov-report=term-missing tests/

run:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

docker-build:
	docker build -t ai-agent-devops:local .

chaos:
	$(PYTHON) scripts/chaos_test.py

e2e:
	bash scripts/run_integration_test.sh

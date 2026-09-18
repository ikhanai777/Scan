# Local development and a one-command localhost deploy.
.PHONY: help install dev doctor scan serve up down logs test lint record backtest sweep clean

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

help:                       ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)

install: $(BIN)/python       ## Create a venv and install the app
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[api]"
	@test -f .env || cp .env.example .env
	@echo "installed. next:  make doctor"

dev: $(BIN)/python           ## Install with test and lint tooling
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[api,dev]"
	@test -f .env || cp .env.example .env

doctor: ## Prove every live source end to end
	$(BIN)/cryptosignal doctor

scan:   ## One real scan cycle
	$(BIN)/cryptosignal scan --once

serve:  ## Dashboard + scanner on http://localhost:8000
	$(BIN)/cryptosignal serve --scan --host 0.0.0.0

record: ## Capture real market data as test fixtures
	$(BIN)/cryptosignal record

backtest: ## Replay the engine over real history
	$(BIN)/cryptosignal backtest

sweep:  ## Tune parameters against real history
	$(BIN)/cryptosignal sweep

test:   ## Run the suite
	$(BIN)/python -m pytest -q

lint:   ## Lint
	$(BIN)/ruff check .

up:     ## Start in Docker on localhost:8000
	@test -f .env || cp .env.example .env
	docker compose up --build -d
	@echo "dashboard: http://localhost:$${CS_API_PORT:-8000}"

down:   ## Stop the Docker stack
	docker compose down

logs:   ## Follow container logs
	docker compose logs -f

clean:  ## Remove the venv and local database
	rm -rf $(VENV) .pytest_cache .ruff_cache
	rm -f cryptosignal.db cryptosignal.db-wal cryptosignal.db-shm

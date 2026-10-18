# Shortcuts for common tasks. `make help` lists them.
.PHONY: help install test lint format typecheck check web web-dev run scan clean

help: ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python deps (uv) and web deps (npm)
	uv sync --all-extras
	cd web && npm ci

test: ## Run the test suite (no network)
	uv run pytest -q

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and auto-fix lint
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy over src/
	uv run mypy src

check: lint typecheck test ## Everything CI runs on the Python side

web: ## Typecheck and build the UI into web/dist
	cd web && npm run build

web-dev: ## UI dev server on :5173 (proxies to :8787)
	cd web && npm run dev

run: ## Full system, paper mode (autotrade per config)
	uv run kst run

scan: ## Scanner + UI only, no automatic orders
	uv run kst scan

clean: ## Remove caches and build output (keeps data/)
	rm -rf .pytest_cache .mypy_cache .ruff_cache web/dist

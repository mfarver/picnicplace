# Developer shortcuts. Requires uv (https://docs.astral.sh/uv/) and Node.js 22+.

.PHONY: setup core studio dev api check test

setup:  ## Install core and Studio dependencies
	cd core && uv sync
	cd studio && npm install

core:  ## Run the core with the simulator, serving the built Studio
	cd studio && npm run build
	cd core && uv run picnicd --studio-dir ../studio/dist

dev:  ## Run the Studio dev server (start the core separately with: cd core && uv run picnicd)
	cd studio && npm run dev

api:  ## Regenerate the OpenAPI document and the Studio's TypeScript types
	cd core && uv run picnicd --print-openapi > ../docs/api/openapi.json
	cd studio && npm run gen:api

check:  ## Run every linter, type checker, and test suite (what CI runs)
	cd core && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
	cd studio && npm run lint && npm run typecheck && npm test && npm run build

test: check

SHELL := /bin/bash
PY := .venv/bin/python
PIP := .venv/bin/pip
NODE_BIN := $(HOME)/.nvm/versions/node/v22.23.2/bin
export PATH := $(NODE_BIN):$(PATH)

COMFY_ROOT ?= /home/magix/ai/ComfyUI
NODEPACK_NAME := comfyui-webstudio

.PHONY: help setup venv frontend-deps dev backend frontend build test test-backend test-nodepack \
        test-frontend ui-smoke lint fmt link-nodepack unlink-nodepack clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: venv frontend-deps ## Install backend and frontend dependencies

venv: ## Create the backend virtualenv and install deps
	test -d .venv || python3 -m venv .venv
	$(PIP) install -q -e ".[dev]"

frontend-deps: ## Install frontend node modules
	cd frontend && npm install

dev: ## Run backend (:8500) and frontend dev server (:5173) together
	$(MAKE) -j2 backend frontend

backend: ## Run the FastAPI backend with reload
	$(PY) -m comfywebstudio.main --reload

frontend: ## Run the Vite dev server
	cd frontend && npm run dev

build: ## Build the frontend into frontend/dist (served by the backend)
	cd frontend && npm run build

test: test-backend test-nodepack test-frontend ## Run every test suite

test-backend: ## Run the backend pytest suite
	$(PY) -m pytest -q

test-nodepack: ## Run node pack tests inside ComfyUI's own venv
	COMFY_ROOT=$(COMFY_ROOT) $(COMFY_ROOT)/comfyenv/bin/python -m pytest -q tests/nodepack

test-frontend: ## Run frontend unit tests and typecheck
	cd frontend && npx tsc -b && npx vitest run

ui-smoke: ## Browser smoke test against a running app (needs `make backend` and a project with a run)
	$(PY) scripts/ui_smoke.py --shots-dir .data/screenshots

lint: ## Ruff check
	$(PY) -m ruff check backend tests

fmt: ## Ruff format + import sort
	$(PY) -m ruff check --fix backend tests
	$(PY) -m ruff format backend tests

link-nodepack: ## Symlink the node pack into ComfyUI/custom_nodes (live-editable)
	@test -d "$(COMFY_ROOT)/custom_nodes" || { echo "No custom_nodes at $(COMFY_ROOT)"; exit 1; }
	ln -sfn "$(CURDIR)/comfy_nodes" "$(COMFY_ROOT)/custom_nodes/$(NODEPACK_NAME)"
	@echo "Linked -> $(COMFY_ROOT)/custom_nodes/$(NODEPACK_NAME)"

unlink-nodepack: ## Remove the symlink
	rm -f "$(COMFY_ROOT)/custom_nodes/$(NODEPACK_NAME)"

clean:
	rm -rf .pytest_cache .ruff_cache frontend/dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

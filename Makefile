.PHONY: help install dev backend frontend serve build test eval forecast smoke config \
        diagram refresh resolve cli docker docker-dev docker-down clean

.DEFAULT_GOAL := help

UV := cd backend && uv run
CLI := $(UV) superforecaster

help: ## List every target
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

# ---------- setup ----------

install: ## Install backend and frontend dependencies
	cd backend && uv sync
	cd frontend && npm install

# ---------- running it ----------

dev: ## Backend :8099 + frontend :5173, both hot-reloading, one Ctrl+C stops both
	@trap 'kill 0' EXIT; \
	(cd backend && uv run uvicorn api.main:app --port 8099 --reload) & \
	(cd frontend && npm install && npm run dev) & \
	wait

backend: ## Backend alone on :8099, hot-reloading
	cd backend && uv run uvicorn api.main:app --port 8099 --reload

frontend: ## Frontend alone on :5173, proxying the API on :8099
	cd frontend && npm run dev

serve: build ## The whole app as one process on :8000
	$(CLI) serve

build: ## Build the frontend into frontend/dist
	cd frontend && npm run build

test: ## The backend suite — no network, no API keys
	$(UV) pytest

# ---------- evals ----------

# The eleven agent names, so `make eval decompose` can name one as a second goal. Each
# is a do-nothing target: without them make would try to build `decompose` and fail.
# Listing them rather than a catch-all `%:` rule keeps a typo'd target an error.
AGENTS := decompose lenses outside_view inside_view reflect synthesize critic draft \
          resolution update postmortem

EVAL_AGENT := $(filter $(AGENTS),$(MAKECMDGOALS))

eval: ## Score one agent against its eval cases — make eval decompose [ARGS="--model ..."]
	@test -n "$(EVAL_AGENT)" || { echo 'usage: make eval <agent>, e.g. make eval decompose'; exit 2; }
	@test -f backend/app/evals/$(EVAL_AGENT)_eval.py \
	  || { echo 'no eval for $(EVAL_AGENT) yet — only decompose and critic have one'; exit 2; }
	$(UV) python -m app.evals.$(EVAL_AGENT)_eval $(ARGS)

.PHONY: $(AGENTS)
$(AGENTS):
	@:

# ---------- the CLI ----------

forecast: ## One forecast, interactive, saved to SQLite
	$(CLI) forecast

smoke: ## Forecast a bundled question without saving — the cheap end-to-end check
	$(CLI) forecast --fixture --no-save --max-iterations 3 -v

config: ## Every setting and where its value came from — secrets redacted
	$(CLI) config

diagram: ## The pipeline shape, as mermaid
	$(CLI) diagram

refresh: ## Re-check a saved forecast against new evidence — make refresh ID=<uuid>
	@test -n "$(ID)" || { echo "usage: make refresh ID=<uuid>"; exit 2; }
	$(CLI) refresh --id $(ID)

resolve: ## Has a saved forecast resolved yet — make resolve ID=<uuid>
	@test -n "$(ID)" || { echo "usage: make resolve ID=<uuid>"; exit 2; }
	$(CLI) resolve --id $(ID)

cli: ## Anything else — make cli ARGS="postmortem <uuid>"
	$(CLI) $(ARGS)

# ---------- docker ----------

# Compose v2.7 fails when `env_file` names a file that does not exist, and it predates
# the `required: false` form. Nothing goes in it — keys are exported, or set in the Keys
# panel — so an empty file is enough to let compose start.
backend/.env:
	@touch $@

docker: backend/.env ## The whole app in one container on :8000
	docker compose up --build

docker-dev: backend/.env ## Containerized hot-reload: frontend :5173, api :8000
	docker compose --profile dev up --build

docker-down: ## Stop the compose stack
	docker compose down

# ---------- housekeeping ----------

clean: ## Remove build output, node_modules, and the venv
	rm -rf frontend/dist frontend/node_modules backend/.venv

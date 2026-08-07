.PHONY: install dev backend frontend build test docker docker-dev docker-down clean

BACKEND_ENV := backend/.env

install:
	cd backend && uv sync
	cd frontend && npm install

$(BACKEND_ENV):
	cp backend/.env.example $(BACKEND_ENV)
	@echo "Created $(BACKEND_ENV) — add ANTHROPIC_API_KEY (or PYDANTIC_AI_GATEWAY_API_KEY)"
	@echo "and TAVILY_API_KEY before running again."

## Backend + frontend dev servers together, hot-reload on both, one Ctrl+C stops both.
dev: $(BACKEND_ENV)
	@trap 'kill 0' EXIT; \
	(cd backend && uv run uvicorn api.main:app --port 8099 --reload) & \
	(cd frontend && npm install && npm run dev) & \
	wait

backend: $(BACKEND_ENV)
	cd backend && uv run uvicorn api.main:app --port 8099 --reload

frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	cd backend && uv run pytest

## The whole app, one process, :8000 — builds the frontend inside the image.
docker: $(BACKEND_ENV)
	docker compose up --build

## Hot-reloading frontend (:5173) + api (:8000), each in its own container.
docker-dev: $(BACKEND_ENV)
	docker compose --profile dev up --build

docker-down:
	docker compose down

clean:
	rm -rf frontend/dist frontend/node_modules backend/.venv

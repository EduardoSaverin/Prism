.PHONY: sync lint format typecheck test check run postman postman-check hooks

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test postman-check

run:
	uv run prism serve --config config/prism.example.yaml

postman:
	uv run python scripts/generate_postman.py

postman-check:
	uv run python scripts/generate_postman.py --check

hooks:
	./scripts/install-githooks.sh

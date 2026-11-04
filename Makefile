.PHONY: sync source dry-run migrate archive experiment plot validate test lint

sync:
	uv sync

source:       ## rebuild the legacy side from the committed snapshot (offline, seeded)
	uv run python -m gxpmig.source

dry-run:
	uv run python -m gxpmig.migrate --dry-run

migrate:
	uv run python -m gxpmig.migrate --batch B001

archive:
	uv run python -m gxpmig.archive

experiment:   ## baseline vs pipeline + integrity controls → results.json
	uv run python experiment.py

plot:
	uv run python plot.py

validate:     ## run the verification tests and regenerate the traceability matrix
	uv run pytest -q
	uv run python scripts/traceability.py

test: validate

lint:
	uv run ruff check . && uv run ruff format --check .

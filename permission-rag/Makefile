# Rootless podman only. The port is published on loopback and nowhere else.
CONTAINER := lab-pgvector
IMAGE     := docker.io/pgvector/pgvector:pg17
PORT      := 55432

.PHONY: up down seed test eval lint query

up:
	@podman container exists $(CONTAINER) || podman run -d --name $(CONTAINER) \
		--memory 512m --cpus 1 \
		-e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=ragdb \
		-p 127.0.0.1:$(PORT):5432 $(IMAGE) >/dev/null
	@podman start $(CONTAINER) >/dev/null
	@echo "waiting for PostgreSQL..."
	@for i in $$(seq 1 60); do \
		podman exec $(CONTAINER) psql -U postgres -d ragdb -Atc 'select 1' >/dev/null 2>&1 && break; \
		sleep 1; done
	@podman exec -i $(CONTAINER) psql -U postgres -d ragdb -v ON_ERROR_STOP=1 -q < sql/roles.sql
	@echo "up: $(CONTAINER) on 127.0.0.1:$(PORT)"

# Removes the container and its anonymous data volume. Nothing is left running.
down:
	@podman rm --force --volumes --ignore $(CONTAINER) >/dev/null
	@echo "down: $(CONTAINER) removed (the data goes with it; run make up and make seed again)"

seed:
	uv run python -m permission_rag seed

test:
	uv run pytest

eval:
	uv run python -m permission_rag eval

lint:
	uv run ruff check .
	uv run mypy

query:
	uv run python -m permission_rag query "How often are torque wrenches calibrated?" --level INTERNAL

# Rootless podman only. Both ports are published on loopback and nowhere else.
CONTAINER := lab-jaeger
IMAGE     := docker.io/jaegertracing/jaeger:2.23.0
UI        := http://127.0.0.1:16686
OTLP      := http://127.0.0.1:4318

.PHONY: test lint jaeger-up jaeger-down trace-demo

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy

# Jaeger all-in-one: traces are kept in memory and go when the container goes.
jaeger-up:
	@podman container exists $(CONTAINER) || podman run -d --name $(CONTAINER) \
		--memory 256m --cpus 1 -e GOMEMLIMIT=200MiB \
		-p 127.0.0.1:16686:16686 -p 127.0.0.1:4318:4318 $(IMAGE) >/dev/null
	@podman start $(CONTAINER) >/dev/null
	@for i in $$(seq 1 30); do curl -fsS -o /dev/null $(UI)/api/v3/services 2>/dev/null && break; sleep 1; done
	@curl -fsS -o /dev/null $(UI)/api/v3/services && echo "up: $(CONTAINER), UI on $(UI), OTLP HTTP on $(OTLP)"

# Removes the container. Nothing is left running, and the traces go with it.
jaeger-down:
	@podman rm --force --ignore $(CONTAINER) >/dev/null
	@echo "down: $(CONTAINER) removed"

# Runs the demo with tracing on (start, then resume --approve, as two processes), then asks Jaeger
# for the two traces and prints the span tree. It fails if the traces did not arrive.
# SAVE=1 also writes docs/trace-example.json and docs/trace-example.txt.
trace-demo:
	@dir=$$(mktemp -d) && export OTEL_EXPORTER_OTLP_ENDPOINT=$(OTLP) && \
		uv run python -m heal_loop start --dir $$dir/demo >/dev/null && \
		uv run python -m heal_loop resume --dir $$dir/demo --approve && \
		uv run python -m heal_loop.jaeger $$dir/demo/trace-demo.json --jaeger $(UI) \
			$(if $(SAVE),--json docs/trace-example.json --text docs/trace-example.txt) && \
		rm -rf "$$dir"

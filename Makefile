SHELL := /bin/bash

TELEMETRY_PORT ?= 8765
VITE_PORT ?= 5173

.PHONY: telemetry-web-install telemetry-web telemetry-server telemetry-dev

telemetry-web-install:
	cd src/mash/telemetry/web && npm install

telemetry-web:
	cd src/mash/telemetry/web && npm run dev -- --port $(VITE_PORT)

telemetry-server:
	@if [ -z "$(TELEMETRY_LOG)" ]; then echo "TELEMETRY_LOG is required"; exit 1; fi
	@if [ -z "$(TELEMETRY_MEMORY_DB)" ]; then echo "TELEMETRY_MEMORY_DB is required"; exit 1; fi
	python -m mash.telemetry --log "$(TELEMETRY_LOG)" --port $(TELEMETRY_PORT) --memory-db "$(TELEMETRY_MEMORY_DB)"

# Usage:
#   make telemetry-dev TELEMETRY_LOG=/path/to/telemetry.jsonl TELEMETRY_MEMORY_DB=/path/to/memory.db
telemetry-dev: telemetry-web-install
	@echo "Starting telemetry server on :$(TELEMETRY_PORT) and Vite on :$(VITE_PORT)"
	@bash -c 'set -euo pipefail; \
	shutdown() { \
	  if [ -n "$${_shutdown:-}" ]; then return; fi; \
	  _shutdown=1; \
	  echo "Shutting down..."; \
	  kill 0; \
	}; \
	trap shutdown INT TERM EXIT; \
	$(MAKE) telemetry-server & \
	$(MAKE) telemetry-web'

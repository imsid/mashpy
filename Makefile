SHELL := /bin/bash

TELEMETRY_LOG ?= $(HOME)/.mash/logs/codebase.jsonl
TELEMETRY_PORT ?= 8765
VITE_PORT ?= 5173

.PHONY: telemetry-web-install telemetry-web telemetry-server telemetry-dev

telemetry-web-install:
	cd src/mash/telemetry/web && npm install

telemetry-web:
	cd src/mash/telemetry/web && npm run dev -- --port $(VITE_PORT)

telemetry-server:
	python -m mash.telemetry --log $(TELEMETRY_LOG) --port $(TELEMETRY_PORT)

telemetry-dev: telemetry-web-install
	@echo "Starting telemetry server on :$(TELEMETRY_PORT) and Vite on :$(VITE_PORT)"
	@bash -c 'set -euo pipefail; \
	trap "echo Shutting down...; kill 0" INT TERM EXIT; \
	$(MAKE) telemetry-server & \
	$(MAKE) telemetry-web'

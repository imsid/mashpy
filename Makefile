SHELL := /bin/bash

VITE_PORT ?= 5173
TELEMETRY_WEB_STATIC_DIR ?= packages/mash-telemetry-web/src/mash_telemetry_web/static

# Maintainer-only helpers for telemetry observer UI packaging.
# Runtime telemetry server usage is:
#   python -m mash.telemetry --log /path/to/events.jsonl [--memory-db ...] [--ui ...]
.PHONY: telemetry-web-install telemetry-web telemetry-web-build telemetry-web-package-sync

telemetry-web-install:
	cd src/mash/telemetry/web && npm install

telemetry-web:
	cd src/mash/telemetry/web && npm run dev -- --port $(VITE_PORT)

telemetry-web-build:
	cd src/mash/telemetry/web && npm run build

telemetry-web-package-sync: telemetry-web-build
	rm -rf "$(TELEMETRY_WEB_STATIC_DIR)"
	mkdir -p "$(TELEMETRY_WEB_STATIC_DIR)"
	cp -R src/mash/telemetry/web/dist/. "$(TELEMETRY_WEB_STATIC_DIR)/"

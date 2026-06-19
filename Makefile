SHELL := /bin/bash

VITE_PORT ?= 5173
TELEMETRY_WEB_SOURCE_DIR ?= src/mash/api/web
TELEMETRY_WEB_STATIC_DIR ?= src/mash/api/static/telemetry

ADMIN_VITE_PORT ?= 5174
ADMIN_WEB_SOURCE_DIR ?= src/mash/api/web-admin
ADMIN_WEB_STATIC_DIR ?= src/mash/api/static/admin

# Maintainer-only helpers for the built-in telemetry UI.
# `make telemetry-web-install`
#   Run once after cloning, or any time frontend dependencies change.
# `make telemetry-web`
#   Run while iterating on the UI locally with the Vite dev server.
# `make telemetry-web-build`
#   Run to produce a fresh production bundle in `src/mash/api/web/dist`.
# `make telemetry-web-package-sync`
#   Run after building the UI, before packaging or testing the embedded `/telemetry` route.
.PHONY: telemetry-web-install telemetry-web telemetry-web-build telemetry-web-package-sync

telemetry-web-install:
	cd $(TELEMETRY_WEB_SOURCE_DIR) && npm install

telemetry-web:
	cd $(TELEMETRY_WEB_SOURCE_DIR) && npm run dev -- --port $(VITE_PORT)

telemetry-web-build:
	cd $(TELEMETRY_WEB_SOURCE_DIR) && npm run build

telemetry-web-package-sync: telemetry-web-build
	rm -rf "$(TELEMETRY_WEB_STATIC_DIR)"
	mkdir -p "$(TELEMETRY_WEB_STATIC_DIR)"
	cp -R $(TELEMETRY_WEB_SOURCE_DIR)/dist/. "$(TELEMETRY_WEB_STATIC_DIR)/"

# Maintainer-only helpers for the admin dashboard UI. Mirrors the telemetry
# targets: install once, `admin-web` to iterate, `admin-web-package-sync` to
# refresh the embedded `/admin` route before packaging or testing.
.PHONY: admin-web-install admin-web admin-web-build admin-web-package-sync

admin-web-install:
	cd $(ADMIN_WEB_SOURCE_DIR) && npm install

admin-web:
	cd $(ADMIN_WEB_SOURCE_DIR) && npm run dev -- --port $(ADMIN_VITE_PORT)

admin-web-build:
	cd $(ADMIN_WEB_SOURCE_DIR) && npm run build

admin-web-package-sync: admin-web-build
	rm -rf "$(ADMIN_WEB_STATIC_DIR)"
	mkdir -p "$(ADMIN_WEB_STATIC_DIR)"
	cp -R $(ADMIN_WEB_SOURCE_DIR)/dist/. "$(ADMIN_WEB_STATIC_DIR)/"

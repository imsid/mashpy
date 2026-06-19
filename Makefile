SHELL := /bin/bash

ADMIN_VITE_PORT ?= 5174
ADMIN_WEB_SOURCE_DIR ?= src/mash/api/web-admin
ADMIN_WEB_STATIC_DIR ?= src/mash/api/static/admin

# Maintainer-only helpers for the admin dashboard UI.
# `make admin-web-install`
#   Run once after cloning, or any time frontend dependencies change.
# `make admin-web`
#   Run while iterating on the UI locally with the Vite dev server.
# `make admin-web-build`
#   Run to produce a fresh production bundle in `src/mash/api/web-admin/dist`.
# `make admin-web-package-sync`
#   Run after building the UI, before packaging or testing the embedded `/admin` route.
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

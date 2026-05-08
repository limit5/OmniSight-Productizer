# OmniSight repo Makefile.
#
# Currently only hosts docs-site dev-server / build targets (OP-786). Add
# additional targets above the .PHONY line below as new operator commands
# land.

DOCS_SITE_DIR := docs-site
DOCS_VENV     := $(DOCS_SITE_DIR)/.venv
DOCS_PY       := $(DOCS_VENV)/bin/python
DOCS_PIP      := $(DOCS_VENV)/bin/pip
DOCS_MKDOCS   := $(DOCS_VENV)/bin/mkdocs
DOCS_PORT    ?= 8765

.PHONY: docs-install docs-serve docs-build docs-clean help

help:
	@echo "OmniSight Make targets:"
	@echo "  make docs-install                      Create docs-site/.venv and install MkDocs + Material"
	@echo "  make docs-serve [DOCS_PORT=NNNN]       Run the docs dev-server (default port 8765)"
	@echo "  make docs-build                        Build the static site into docs-site-dist/"
	@echo "  make docs-clean                        Remove docs-site/.venv and docs-site-dist/"

$(DOCS_VENV):
	python3 -m venv $(DOCS_VENV)

docs-install: $(DOCS_VENV)
	$(DOCS_PIP) install --upgrade pip
	$(DOCS_PIP) install -r $(DOCS_SITE_DIR)/requirements.txt

docs-serve: docs-install
	cd $(DOCS_SITE_DIR) && ../$(DOCS_MKDOCS) serve --dev-addr 127.0.0.1:$(DOCS_PORT)

docs-build: docs-install
	cd $(DOCS_SITE_DIR) && ../$(DOCS_MKDOCS) build

docs-clean:
	rm -rf $(DOCS_VENV) docs-site-dist

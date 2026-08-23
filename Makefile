.PHONY: install generate test eval demo ui lint

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

install-ui:
	$(PYTHON) -m pip install -e ".[dev,ui]"

generate:
	$(PYTHON) -m ledgerloop.generate.generator --profile all

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests

eval:
	@echo "eval harness lands in Phase 5 (src/ledgerloop/eval/) -- not implemented yet."

demo:
	@echo "demo pipeline lands in Phase 4-6 -- not implemented yet."
	@echo "Phase 1 is ready: run 'make generate' to produce data/dev and data/holdout."

ui:
	@echo "Streamlit dashboard lands in Phase 6 (src/ledgerloop/ui/app.py) -- not implemented yet."

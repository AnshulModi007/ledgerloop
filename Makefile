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
	$(PYTHON) -m ledgerloop.generate.generator --profile dev
	$(PYTHON) -m ledgerloop.reconcile --profile dev --no-llm
	@echo ""
	@echo "Exceptions, journal entries, and the dashboard land in Phase 4-6."
	@echo "Set GEMINI_API_KEY/GROQ_API_KEY/OPENROUTER_API_KEY, or run a local Ollama,"
	@echo "and drop --no-llm above to let tier3 adjudicate the remainder."

ui:
	@echo "Streamlit dashboard lands in Phase 6 (src/ledgerloop/ui/app.py) -- not implemented yet."

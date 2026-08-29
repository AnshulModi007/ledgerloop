.PHONY: install generate test eval sensitivity scale demo ui lint

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
	$(PYTHON) -m ledgerloop.eval.metrics --profile dev --no-llm
	$(PYTHON) -m ledgerloop.eval.ablation --profile dev --no-llm
	$(PYTHON) -m ledgerloop.eval.calibration --profile dev --no-llm

# Threshold trade-off curves: what each tier2 knob would have cost or bought. ~30s.
sensitivity:
	$(PYTHON) -m ledgerloop.eval.sensitivity --profile dev

# Volume benchmark, 5k -> 100k transactions. ~2 minutes, and it writes ~45 MB of
# generated CSV under data/scale_* (gitignored).
scale:
	$(PYTHON) -m ledgerloop.eval.scale

demo:
	$(PYTHON) -m ledgerloop.generate.generator --profile dev
	$(PYTHON) -m ledgerloop.reconcile --profile dev --no-llm --approve
	@echo ""
	@echo "Re-running now (same command) will show 0 new postings -- the loop is closed."
	@echo "The dashboard (Phase 6) puts a one-click button on that same re-run."
	@echo "Set GEMINI_API_KEY/GROQ_API_KEY/OPENROUTER_API_KEY, or run a local Ollama,"
	@echo "and drop --no-llm above to let tier3 adjudicate the remainder."

ui:
	$(PYTHON) -m streamlit run src/ledgerloop/ui/app.py

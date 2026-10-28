# TCP vs UDP Performance Comparison System
# Usage: make setup | quick | full | dashboard | report | test | clean
VENV    ?= .venv
PY      ?= $(VENV)/bin/python
MODE    ?= auto
REPEATS ?=
ARGS    ?=

REPEAT_FLAG := $(if $(REPEATS),--repeats $(REPEATS),)
# keep macOS awake during long runs (sleep corrupts timings); no-op elsewhere
AWAKE       := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)

.PHONY: help setup quick full analyze conclusion report dashboard test netem-check clean clean-results

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup:           ## create .venv and install requirements
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt

quick:           ## quick demo matrix (~2-3 min) + plots + conclusion
	$(AWAKE) $(PY) harness/runner.py --quick --mode $(MODE) $(REPEAT_FLAG) $(ARGS)
	$(PY) analysis/analyzer.py
	$(PY) analysis/conclusion.py > /dev/null && echo "conclusion -> report/conclusion.md"

full:            ## full factorial matrix (hours; resumable) + plots + report
	$(AWAKE) $(PY) harness/runner.py --full --resume --mode $(MODE) $(REPEAT_FLAG) $(ARGS)
	$(PY) analysis/analyzer.py
	$(PY) analysis/report_builder.py

analyze:         ## regenerate plots from results/results.csv
	$(PY) analysis/analyzer.py

conclusion:      ## regenerate report/conclusion.md
	$(PY) analysis/conclusion.py

report:          ## plots + conclusion + report/report.md
	$(PY) analysis/report_builder.py

dashboard:       ## launch the Streamlit dashboard (http://localhost:8501)
	$(PY) -m streamlit run dashboard/app.py

test:            ## run the pytest suite
	$(PY) -m pytest -v

netem-check:     ## is tc netem usable here?
	-$(PY) netem/netem_py.py check

clean:           ## remove caches, plots and generated reports (keeps CSV results)
	find . -name __pycache__ -type d -prune -not -path './$(VENV)/*' -exec rm -rf {} +
	rm -rf .pytest_cache results/plots/*.png report/report.md report/conclusion.md

clean-results:   ## ALSO delete all CSV results
	rm -f results/*.csv

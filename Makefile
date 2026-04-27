PYTHON ?= ./venv/bin/python3
PYTEST ?= ./venv/bin/pytest

.PHONY: test test-quick test-verbose lint check capture-fixture

test:
	$(PYTEST) -v

test-quick:
	$(PYTEST) -q

test-verbose:
	$(PYTEST) -vv -s

lint:
	$(PYTHON) -m py_compile zoom_notes.py zoom_engine.py zoom_config.py

check: lint test

# Capture a fresh WAL fixture during a live meeting:
#   make capture-fixture NAME=single_meeting
capture-fixture:
	@if [ -z "$(NAME)" ]; then echo "Usage: make capture-fixture NAME=<fixture-name>"; exit 1; fi
	$(PYTHON) tools/capture_wal.py $(NAME)

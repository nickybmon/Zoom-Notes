PYTHON ?= ./venv/bin/python3
PYTEST ?= ./venv/bin/pytest
BUNDLED_PYTHON ?= ./python-runtime/bin/python3.12

.PHONY: test test-quick test-verbose lint check check-bundled capture-fixture install-hooks install-cli install uninstall-cli deploy

test:
	$(PYTEST) -v

test-quick:
	$(PYTEST) -q

test-verbose:
	$(PYTEST) -vv -s

lint:
	$(PYTHON) -m py_compile zoom_notes.py zoom_engine.py zoom_config.py

check: lint test

# Run the same checks under the BUNDLED Python 3.12 runtime — the one
# that actually ships inside the .app bundle. Catches bugs that 3.14's
# deferred-annotation behavior (PEP 649) hides from the venv suite,
# such as missing type imports referenced in method signatures.
# This is the gate that should pass before any release build.
check-bundled:
	@if [ ! -x "$(BUNDLED_PYTHON)" ]; then \
		echo "✗ Bundled Python not found at $(BUNDLED_PYTHON)"; \
		echo "  Run scripts/fetch-python-runtime.sh first."; \
		exit 1; \
	fi
	@if ! $(BUNDLED_PYTHON) -c "import pytest" 2>/dev/null; then \
		echo "▶ Installing pytest into bundled runtime (one-time)…"; \
		$(BUNDLED_PYTHON) -m pip install --quiet pytest; \
	fi
	$(BUNDLED_PYTHON) -m py_compile zoom_notes.py zoom_engine.py zoom_config.py
	$(BUNDLED_PYTHON) -c "import zoom_engine, zoom_notes, zoom_config" \
		|| (echo "✗ Module import failed on bundled Python — check for missing type imports"; exit 1)
	$(BUNDLED_PYTHON) -m pytest -q

# Capture a fresh WAL fixture during a live meeting:
#   make capture-fixture NAME=single_meeting
capture-fixture:
	@if [ -z "$(NAME)" ]; then echo "Usage: make capture-fixture NAME=<fixture-name>"; exit 1; fi
	$(PYTHON) tools/capture_wal.py $(NAME)

# Symlink the tracked pre-commit hook into .git/hooks/. Run once per clone.
# The hook blocks accidental commits of WALs, generated meeting notes,
# .env files, and high-confidence API key strings.
install-hooks:
	@mkdir -p .git/hooks
	@ln -sf ../../scripts/git-hooks/pre-commit .git/hooks/pre-commit
	@chmod +x scripts/git-hooks/pre-commit
	@echo "Installed pre-commit hook -> .git/hooks/pre-commit"
	@echo "Bypass (sparingly): git commit --no-verify"

# ── Local CLI launcher ────────────────────────────────────────────────────────
# Symlink scripts/zoomnotes into ~/.local/bin so it's available on $PATH.
# After this, run `zoomnotes` from anywhere to rebuild and relaunch the app.
BIN_DIR ?= $(HOME)/.local/bin

install-cli:
	@mkdir -p "$(BIN_DIR)"
	@chmod +x scripts/zoomnotes scripts/install-local.sh
	@ln -sf "$(CURDIR)/scripts/zoomnotes" "$(BIN_DIR)/zoomnotes"
	@echo "Linked $(BIN_DIR)/zoomnotes -> $(CURDIR)/scripts/zoomnotes"
	@case ":$$PATH:" in *":$(BIN_DIR):"*) ;; *) echo "⚠  $(BIN_DIR) is not on your PATH — add it to ~/.zshrc";; esac
	@echo "Try it: zoomnotes --help"

uninstall-cli:
	@rm -f "$(BIN_DIR)/zoomnotes"
	@echo "Removed $(BIN_DIR)/zoomnotes"

# Hot-swap the Python engine files into the running app without a full Xcode
# rebuild. Safe when only zoom_notes.py / zoom_engine.py / zoom_config.py
# changed. Kills the engine subprocess so the Swift app restarts it with the
# new code (~2s). Use `make install` instead when the Swift binary changed.
deploy:
	@cp zoom_notes.py zoom_engine.py zoom_config.py \
		"/Applications/Zoom Notes.app/Contents/Resources/"
	@pkill -f zoom_engine.py 2>/dev/null || true
	@echo "✓ Engine updated and restarted"

# Build + install the app locally without symlinking the CLI launcher.
install:
	@./scripts/install-local.sh

PYTHON ?= ./venv/bin/python3
PYTEST ?= ./venv/bin/pytest

.PHONY: test test-quick test-verbose lint check capture-fixture install-hooks install-cli install uninstall-cli

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

# Build + install the app locally without symlinking the CLI launcher.
install:
	@./scripts/install-local.sh

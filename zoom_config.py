#!/usr/bin/env python3
"""
zoom_config.py — Configuration management for Zoom Meeting Notes Assistant.

All user-configurable settings live here. Settings are split into two stores:
  - Non-sensitive prefs: ~/Library/Application Support/zoom-notes/settings.json
  - API keys:           macOS Keychain (service "zoom-notes-assistant")

Environment variables and .env file are checked as fallback/override, so
existing launchd plists with ANTHROPIC_API_KEY baked in continue to work.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ── Default system prompt ──────────────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = """You are a meticulous meeting notetaker. Produce detailed, well-structured meeting notes from the transcript. Be thorough — a reader who wasn't in the meeting should come away with a complete picture of what was discussed, decided, and committed to.

Use this structure exactly. Include every section even if brief.

## Overview
2-4 sentences capturing the purpose and outcome of the meeting. Who was involved, what was the core focus, what was resolved or left open.

## Attendees
Bullet list of attendee names (use the speaker names from the transcript).

## Topics Discussed
A sequenced list of the main topics covered. For each topic, 1-3 sentences on what was said — include specific details, numbers, names, and context. Don't collapse important nuance into vague summaries.

Format:
- **[Topic name]** — [What was discussed. Be specific.]

## Key Decisions
Decisions that were explicitly made or agreed upon. If none, write "No explicit decisions recorded."

Format:
- [Decision] — [Who made it or who it affects, if clear]

## Action Items
A table of all commitments, tasks, and follow-ups. Include owner, task description, and due date if mentioned.

| Owner | Task | Due Date |
|-------|------|----------|
| [name] | [what they committed to] | [date or null] |

## Open Questions
Unresolved questions, decisions deferred, or topics that need follow-up. Omit this section entirely if none.

## Notes
Any additional context, background, or detail worth capturing that didn't fit above. Omit if nothing relevant.

---

Output only the meeting notes. No preamble, no explanation, no meta-commentary."""

# ── Config file path ───────────────────────────────────────────────────────────

_CONFIG_DIR = Path.home() / "Library/Application Support/zoom-notes"
_CONFIG_FILE = _CONFIG_DIR / "settings.json"
_KEYCHAIN_SERVICE = "zoom-notes-assistant"


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class ZoomNotesConfig:
    # LLM provider and model
    llm_provider: str = "claude"          # "claude" | "openai" | "gemini" | "ollama"
    llm_model: str = "claude-sonnet-4-6"

    # Output paths
    notes_dir: str = str(Path.home() / "Desktop/Meeting Notes/Notes")
    transcripts_dir: str = str(Path.home() / "Desktop/Meeting Notes/Transcripts")

    # Output structure
    subfolder_pattern: str = "day"        # "none" | "day" | "month"
    filename_pattern: str = "{title}"
    transcript_filename_pattern: str = "{title} \u2014 transcript"

    # Prompt (None = use DEFAULT_SYSTEM_PROMPT)
    system_prompt: str | None = None

    # Polling / idle detection (used by zoom_menu_bar.py)
    poll_interval_secs: int = 5
    idle_threshold_secs: int = 30

    # WAL prefixes (override if Zoom changes their IndexedDB hash)
    transcript_db_prefix: str = "1CB477F679D6"
    blocks_db_prefix: str = "DDEC8414E29A"

    # Provider base URLs (override to route through a proxy)
    ollama_base_url: str = "http://localhost:11434"
    openai_base_url: str = "https://api.openai.com/v1/chat/completions"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/models"

    @property
    def effective_system_prompt(self) -> str:
        return self.system_prompt or DEFAULT_SYSTEM_PROMPT

    @property
    def notes_path(self) -> Path:
        return Path(self.notes_dir).expanduser()

    @property
    def transcripts_path(self) -> Path:
        return Path(self.transcripts_dir).expanduser()


# ── JSON serialisation ─────────────────────────────────────────────────────────

_KNOWN_KEYS = set(ZoomNotesConfig.__dataclass_fields__.keys())  # type: ignore[attr-defined]


def _config_to_dict(cfg: ZoomNotesConfig) -> dict:
    return asdict(cfg)


def _config_from_dict(d: dict) -> ZoomNotesConfig:
    filtered = {k: v for k, v in d.items() if k in _KNOWN_KEYS}
    return ZoomNotesConfig(**filtered)


# ── Keychain helpers ───────────────────────────────────────────────────────────

_KEY_ACCOUNTS = {
    "claude":  "anthropic_api_key",
    "openai":  "openai_api_key",
    "gemini":  "gemini_api_key",
}

# Env var fallbacks per provider (existing behaviour preserved)
_KEY_ENV_VARS = {
    "claude":  "ANTHROPIC_API_KEY",
    "openai":  "OPENAI_API_KEY",
    "gemini":  "GEMINI_API_KEY",
}


def keychain_get(account: str) -> str | None:
    """Read a password from the macOS Keychain. Returns None if not found."""
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", _KEYCHAIN_SERVICE,
                "-a", account,
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            return value if value else None
    except OSError:
        pass
    return None


def keychain_set(account: str, value: str) -> bool:
    """Write (or update) a password in the macOS Keychain. Returns True on success."""
    if not value:
        return keychain_delete(account)
    try:
        # Try to update existing entry first
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", _KEYCHAIN_SERVICE,
                "-a", account,
                "-w", value,
                "-U",  # update if exists
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except OSError:
        return False


def keychain_delete(account: str) -> bool:
    """Delete a Keychain entry. Returns True if deleted or already absent."""
    try:
        result = subprocess.run(
            [
                "security", "delete-generic-password",
                "-s", _KEYCHAIN_SERVICE,
                "-a", account,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode in (0, 44)  # 44 = not found, which is fine
    except OSError:
        return False


def get_api_key(provider: str) -> str | None:
    """
    Return the API key for the given provider.
    Priority: Keychain → env var (for backwards compat with .env / launchd plist).
    """
    account = _KEY_ACCOUNTS.get(provider)
    if account:
        key = keychain_get(account)
        if key:
            return key

    # Env var fallback (covers existing ANTHROPIC_API_KEY usage)
    env_var = _KEY_ENV_VARS.get(provider)
    if env_var:
        return os.environ.get(env_var)

    return None


def set_api_key(provider: str, value: str) -> bool:
    """Store the API key for the given provider in the Keychain."""
    account = _KEY_ACCOUNTS.get(provider)
    if not account:
        return False
    return keychain_set(account, value)


# ── Load / save ────────────────────────────────────────────────────────────────

_cached_config: ZoomNotesConfig | None = None


def load_config(force: bool = False) -> ZoomNotesConfig:
    """
    Load config from settings.json. Results are cached; pass force=True to reload.
    Missing keys fall back to dataclass defaults, so adding new fields is safe.
    """
    global _cached_config
    if _cached_config is not None and not force:
        return _cached_config

    if _CONFIG_FILE.exists():
        try:
            raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = _config_from_dict(raw)
        except Exception:
            cfg = ZoomNotesConfig()
    else:
        cfg = ZoomNotesConfig()

    # Env var overrides for paths (preserves ZOOM_NOTES_OUTPUT_DIR / ZOOM_NOTES_TRANSCRIPTS_DIR)
    if val := os.environ.get("ZOOM_NOTES_OUTPUT_DIR"):
        cfg.notes_dir = val
    if val := os.environ.get("ZOOM_NOTES_TRANSCRIPTS_DIR"):
        cfg.transcripts_dir = val
    if val := os.environ.get("ZOOM_NOTES_API_URL"):
        # Legacy: if a proxy URL was set, treat as claude provider with custom base
        cfg.openai_base_url = val  # stored for reference; callers handle routing

    _cached_config = cfg
    return cfg


def save_config(cfg: ZoomNotesConfig) -> None:
    """Persist config to settings.json and update the in-memory cache."""
    global _cached_config
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_config_to_dict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)
    _cached_config = cfg


def get_config() -> ZoomNotesConfig:
    """Convenience alias for load_config()."""
    return load_config()


def invalidate_config_cache() -> None:
    """Force the next get_config() call to re-read from disk."""
    global _cached_config
    _cached_config = None


# ── Subfolder / filename helpers ───────────────────────────────────────────────

def resolve_subfolder(cfg: ZoomNotesConfig, date_str: str) -> str:
    """
    Return the subfolder component for the given date_str (YYYY-MM-DD),
    based on cfg.subfolder_pattern.
    """
    if cfg.subfolder_pattern == "day":
        return date_str
    if cfg.subfolder_pattern == "month":
        return date_str[:7]  # YYYY-MM
    return ""  # "none"


def resolve_filename(pattern: str, title: str, date_str: str) -> str:
    """Expand {title} and {date} tokens in a filename pattern."""
    return pattern.replace("{title}", title).replace("{date}", date_str)


# ── CLI helper (for debugging) ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="zoom_config — view/edit settings")
    parser.add_argument("--show", action="store_true", help="Print current config")
    parser.add_argument("--set-key", metavar="PROVIDER", help="Store API key for provider (reads from stdin)")
    args = parser.parse_args()

    if args.show:
        cfg = get_config()
        print(json.dumps(_config_to_dict(cfg), indent=2))
        for provider in _KEY_ACCOUNTS:
            key = get_api_key(provider)
            masked = f"{key[:8]}…" if key and len(key) > 8 else ("(set)" if key else "(not set)")
            print(f"  {provider} key: {masked}")
    elif args.set_key:
        provider = args.set_key.lower()
        if provider not in _KEY_ACCOUNTS:
            print(f"Unknown provider: {provider}. Choose from: {', '.join(_KEY_ACCOUNTS)}", file=sys.stderr)
            sys.exit(1)
        print(f"Enter {provider} API key (input hidden): ", end="", flush=True)
        import getpass
        value = getpass.getpass("")
        if set_api_key(provider, value):
            print("Key saved to Keychain.")
        else:
            print("Failed to save key.", file=sys.stderr)
            sys.exit(1)

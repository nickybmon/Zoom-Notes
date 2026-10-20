"""Guards against the default Claude model drifting between Python and Swift."""
import re
from pathlib import Path

import zoom_config

ROOT = Path(__file__).resolve().parent.parent
SWIFT = ROOT / "ZoomNotesApp" / "ZoomNotesApp"


def _swift_match(relpath: str, pattern: str) -> str:
    text = (SWIFT / relpath).read_text()
    m = re.search(pattern, text)
    assert m, f"pattern not found in {relpath}"
    return m.group(1)


def test_default_model_matches_across_codebase():
    expected = zoom_config.ZoomNotesConfig().llm_model
    assert _swift_match("Models/ZoomNotesConfig.swift", r'var llmModel: String = "([^"]+)"') == expected
    assert _swift_match("Views/SettingsView.swift", r'case "claude":\s+return "([^"]+)"') == expected
    notes = (ROOT / "zoom_notes.py").read_text()
    assert re.search(r'def summarize_with_claude\([^)]*model: str = "([^"]+)"', notes, re.S).group(1) == expected


def test_default_model_is_in_settings_picker():
    expected = zoom_config.ZoomNotesConfig().llm_model
    picker = (SWIFT / "Views" / "SettingsView.swift").read_text()
    assert f'("{expected}",' in picker

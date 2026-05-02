# Security Policy

## Supported versions

Only the latest release is actively maintained.

## Reporting a vulnerability

Please **do not** file a public GitHub issue for security vulnerabilities.

Report security issues by opening a [GitHub Security Advisory](https://github.com/nickybmon/Zoom-Notes/security/advisories/new) in this repository. You'll receive a response within a few days.

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept if possible
- Any suggested mitigations you've identified

## Scope

Areas of most interest:

- **API key exposure** — the app stores keys in macOS Keychain and injects them into the Python process via environment variables. Vulnerabilities that cause keys to leak to disk, logs, or network are high severity.
- **WAL file access** — the app reads `~/Library/Application Support/zoom.us/...` with normal file permissions. Issues that cause it to read or exfiltrate files outside this scope are high severity.
- **Transcript data handling** — transcripts are sent to the configured LLM provider and written to the user's configured output directory. Issues that cause unintended transmission or storage of transcript data are high severity.
- **Code execution** — the Swift app spawns a Python process. Issues that allow arbitrary code injection via settings, the WAL, or the LLM response are high severity.

## Out of scope

- Issues requiring physical access to the user's machine
- Zoom itself (report those to Zoom)
- LLM provider security (report those to the respective provider)
- Social engineering attacks

//
//  EngineManager.swift
//  ZoomNotesApp
//
//  Manages the zoom_engine.py subprocess. Spawns it, reads newline-delimited
//  JSON events from stdout, and restarts it if it exits unexpectedly.
//
//  Adapted from LMA's ProcessManager.swift — no HTTP health check; readiness
//  is signalled via the first {"event":"state"} line on stdout.
//

import Foundation
import Combine

class EngineManager: ObservableObject {
    @Published var isRunning = false
    @Published var error: String?

    // Publish decoded engine events to subscribers (AppState, AppDelegate)
    let eventPublisher = PassthroughSubject<EngineEvent, Never>()

    private var process: Process?
    private let startQueue = DispatchQueue(label: "com.zoom-notes.enginemanager", qos: .userInitiated)
    private var restartCount = 0
    private let maxRestarts = 5

    /// Set to true when we're about to terminate the engine intentionally
    /// (settings reload, app shutdown). The exit watcher consults this to
    /// decide whether to auto-respawn — we still respawn after a settings
    /// reload, but we DO NOT respawn after stopEngine() / app quit.
    private var intentionalRestart = false
    private var intentionalStop = false

    // MARK: - Public

    func startEngine() {
        guard !isRunning else {
            log("[EngineManager] Engine already running, skipping start", level: .debug)
            return
        }
        log("[EngineManager] startEngine() called", level: .info)
        spawnEngine()
    }

    func stopEngine() {
        guard let proc = process else { return }
        log("[EngineManager] Stopping engine (PID \(proc.processIdentifier))", level: .info)
        intentionalStop = true
        (proc.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil
        (proc.standardError as? Pipe)?.fileHandleForReading.readabilityHandler = nil
        proc.terminate()
        DispatchQueue.global(qos: .utility).async {
            let deadline = Date().addingTimeInterval(3.0)
            while proc.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if proc.isRunning {
                log("[EngineManager] Engine didn't exit in 3s — SIGKILL", level: .warning)
                kill(proc.processIdentifier, SIGKILL)
            }
        }
        process = nil
        DispatchQueue.main.async { self.isRunning = false }
    }

    /// Send a JSON command to the engine via stdin.
    func sendCommand(_ payload: [String: Any]) {
        guard let proc = process, proc.isRunning,
              let stdin = proc.standardInput as? Pipe else { return }
        guard var data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        data.append(contentsOf: [0x0A]) // newline
        try? stdin.fileHandleForWriting.write(contentsOf: data)
    }

    /// Reload settings: SIGHUP for config-only changes, full restart when API keys may have changed.
    /// A full restart is required after key changes because the API keys are
    /// injected into the engine's environment at spawn time — SIGHUP only
    /// invalidates the Python config cache, not the env vars.
    func reloadSettings(restartForNewKeys: Bool = true) {
        guard let proc = process, proc.isRunning else {
            // Engine isn't running at all — start it instead of failing silently.
            log("[EngineManager] reloadSettings() called but engine isn't running — spawning", level: .info)
            spawnEngine()
            return
        }
        if restartForNewKeys {
            log("[EngineManager] Restarting engine to pick up new API keys", level: .info)
            intentionalRestart = true
            proc.terminate()
            // The waitUntilExit() watcher in spawnEngine() will see
            // intentionalRestart==true and re-spawn after the process dies.
        } else {
            kill(proc.processIdentifier, SIGHUP)
            log("[EngineManager] Sent SIGHUP to engine (PID \(proc.processIdentifier))", level: .info)
        }
    }

    // MARK: - Private

    private func spawnEngine() {
        guard let pythonPath = findPythonExecutable() else {
            DispatchQueue.main.async {
                self.error = "Python 3.10+ is required. Install it with: brew install python3"
            }
            log("[EngineManager] ERROR: Python 3.10+ not found", level: .error)
            return
        }

        guard let scriptPath = findEngineScript() else {
            DispatchQueue.main.async {
                self.error = "zoom_engine.py not found alongside the app bundle."
            }
            log("[EngineManager] ERROR: zoom_engine.py not found", level: .error)
            return
        }

        log("[EngineManager] Python: \(pythonPath)", level: .info)
        log("[EngineManager] Script: \(scriptPath)", level: .info)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = [scriptPath]

        let projectRoot = findProjectRoot()
        if let root = projectRoot {
            proc.currentDirectoryURL = root
        }

        var env = ProcessInfo.processInfo.environment
        var pathParts: [String] = []
        if let venv = findVenvPath() { pathParts.append("\(venv)/bin") }
        for brew in ["/opt/homebrew/bin", "/usr/local/bin"] where FileManager.default.fileExists(atPath: brew) {
            pathParts.append(brew)
        }
        env["PATH"] = pathParts.joined(separator: ":") + ":" + (env["PATH"] ?? "")

        // Ensure the script's directory is on sys.path so zoom_config / zoom_notes imports resolve
        let scriptDir = URL(fileURLWithPath: scriptPath).deletingLastPathComponent().path
        env["PYTHONPATH"] = scriptDir + ":" + (env["PYTHONPATH"] ?? "")

        // Inject API keys from Keychain so Python never needs to call `security`
        // (avoids repeated keychain permission prompts for the python/security binaries)
        let keyMap: [(account: String, envVar: String)] = [
            ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            ("openai_api_key",    "OPENAI_API_KEY"),
            ("gemini_api_key",    "GEMINI_API_KEY"),
        ]
        for pair in keyMap {
            if let key = keychainGet(account: pair.account), !key.isEmpty {
                env[pair.envVar] = key
            }
        }

        proc.environment = env

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let stdinPipe = Pipe()
        proc.standardOutput = stdoutPipe
        proc.standardError = stderrPipe
        proc.standardInput = stdinPipe

        // Read stdout line by line and decode JSON events
        var lineBuffer = Data()
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            lineBuffer.append(chunk)
            // Split on newlines
            while let nl = lineBuffer.firstIndex(of: UInt8(ascii: "\n")) {
                let lineData = lineBuffer[lineBuffer.startIndex..<nl]
                lineBuffer = lineBuffer[lineBuffer.index(after: nl)...]
                guard !lineData.isEmpty else { continue }
                self?.handleLine(lineData)
            }
        }

        // Drain stderr into our debug log. The pipe must be read; otherwise
        // an unexpectedly noisy Python (a deprecation warning loop, an
        // unraisable exception spammed by a daemon thread) will fill the
        // ~64KB pipe buffer and block the engine on its next stderr write —
        // which manifests as the menu bar app "freezing" with no visible
        // cause. We log at .debug because stderr is expected to be quiet
        // in normal operation; if it ever isn't, the log file shows why.
        var stderrBuffer = Data()
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            stderrBuffer.append(chunk)
            while let nl = stderrBuffer.firstIndex(of: UInt8(ascii: "\n")) {
                let lineData = stderrBuffer[stderrBuffer.startIndex..<nl]
                stderrBuffer = stderrBuffer[stderrBuffer.index(after: nl)...]
                guard !lineData.isEmpty,
                      let raw = String(data: lineData, encoding: .utf8),
                      !raw.trimmingCharacters(in: .whitespaces).isEmpty
                else { continue }
                log("[Engine stderr] \(raw)", level: .debug)
            }
        }

        startQueue.async {
            do {
                try proc.run()
                log("[EngineManager] Engine started, PID \(proc.processIdentifier)", level: .info)
                let startedAt = Date()
                DispatchQueue.main.async {
                    self.process = proc
                    self.isRunning = true
                    self.error = nil
                    // Don't reset restartCount yet — only do it if the engine
                    // stays up for at least `healthyUptimeSecs`. Otherwise a
                    // flapping process would reset the counter on every spawn
                    // and restart forever.
                }

                // Schedule the "you've stayed up long enough" reset.
                let healthyUptimeSecs: TimeInterval = 60
                DispatchQueue.main.asyncAfter(deadline: .now() + healthyUptimeSecs) { [weak self] in
                    guard let self = self else { return }
                    if self.process === proc && proc.isRunning {
                        self.restartCount = 0
                        log("[EngineManager] Engine stable for \(Int(healthyUptimeSecs))s — restart counter reset", level: .debug)
                    }
                }

                proc.waitUntilExit()
                let status = proc.terminationStatus
                let uptime = Date().timeIntervalSince(startedAt)
                log("[EngineManager] Engine exited with status \(status) after \(Int(uptime))s", level: status == 0 ? .info : .error)

                DispatchQueue.main.async {
                    self.isRunning = false
                    self.process = nil
                }

                // Decide what to do about the exit.
                if self.intentionalStop {
                    // App is shutting down or user explicitly stopped — do nothing.
                    self.intentionalStop = false
                    log("[EngineManager] Exit was intentional (stopEngine) — not restarting", level: .debug)
                } else if self.intentionalRestart {
                    // Settings reload requested a restart — respawn immediately.
                    self.intentionalRestart = false
                    log("[EngineManager] Exit was intentional (reloadSettings) — respawning", level: .info)
                    DispatchQueue.main.async {
                        self.spawnEngine()
                    }
                } else if status != 0 {
                    // If the engine died instantly, check if Python is too old
                    if uptime < 2 && self.restartCount == 0 {
                        self.checkPythonVersion(at: pythonPath)
                    }
                    self.scheduleRestart()
                } else {
                    // Clean exit we didn't ask for — surface it but don't loop.
                    log("[EngineManager] Engine exited cleanly without being asked — not restarting", level: .warning)
                    DispatchQueue.main.async {
                        self.error = "Engine exited unexpectedly. Reopen Settings or restart the app to recover."
                    }
                }
            } catch {
                log("[EngineManager] Failed to launch engine: \(error.localizedDescription)", level: .error)
                DispatchQueue.main.async {
                    self.error = "Failed to launch engine: \(error.localizedDescription)"
                }
            }
        }
    }

    private func handleLine(_ data: Data) {
        guard let event = try? JSONDecoder().decode(EngineEvent.self, from: data) else {
            if let raw = String(data: data, encoding: .utf8), !raw.trimmingCharacters(in: .whitespaces).isEmpty {
                log("[Engine] \(raw)", level: .debug)
            }
            return
        }
        if event.event == "diag" {
            // diag detail lives in fields beyond `value` (kind, from_id,
            // to_id, reason, scores, …). Log the full raw payload so
            // post-mortem investigation of meeting-switch decisions actually
            // has the data — `event=diag value=-` was useless on its own.
            let raw = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            log("[Engine] \(raw)", level: .debug)
        } else {
            log("[Engine] event=\(event.event) value=\(event.value ?? "-")", level: .debug)
        }
        DispatchQueue.main.async {
            self.eventPublisher.send(event)
        }
    }

    private func checkPythonVersion(at pythonPath: String) {
        let task = Process()
        task.launchPath = pythonPath
        task.arguments = ["-c", "import sys; v=sys.version_info; exit(0 if v>=(3,10) else 1)"]
        try? task.run()
        task.waitUntilExit()
        if task.terminationStatus != 0 {
            DispatchQueue.main.async {
                self.error = "Python 3.10+ is required but an older version was found. Install it with: brew install python3"
            }
            log("[EngineManager] Python version too old at \(pythonPath)", level: .error)
        }
    }

    private func scheduleRestart() {
        guard restartCount < maxRestarts else {
            DispatchQueue.main.async {
                self.error = "Engine failed to start. Check that Python 3.10+ is installed (brew install python3) and open logs for details."
            }
            return
        }
        restartCount += 1
        let delay = min(Double(restartCount) * 2.0, 10.0)
        log("[EngineManager] Scheduling restart \(restartCount)/\(maxRestarts) in \(delay)s", level: .warning)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.spawnEngine()
        }
    }

    // MARK: - Path discovery

    private func findProjectRoot() -> URL? {
        // Strategy 1: Inside the app bundle's Resources folder (distribution builds).
        // zoom_engine.py is copied here via a "Copy Files" Xcode build phase.
        if let resourcePath = Bundle.main.resourcePath {
            let resourceURL = URL(fileURLWithPath: resourcePath)
            if FileManager.default.fileExists(atPath: resourceURL.appendingPathComponent("zoom_engine.py").path) {
                return resourceURL
            }
        }

        // Strategy 2: Walk up from app bundle looking for zoom_engine.py (dev builds
        // where the app sits inside the repo directory).
        var url = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<12 {
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("zoom_engine.py").path) {
                return url
            }
            let parent = url.deletingLastPathComponent()
            if parent == url { break }
            url = parent
        }

        // Strategy 3: Walk up from source file (Xcode direct-run builds)
        var src = URL(fileURLWithPath: #file)
        for _ in 0..<8 {
            src = src.deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: src.appendingPathComponent("zoom_engine.py").path) {
                return src
            }
        }

        log("[EngineManager] Could not find project root", level: .error)
        return nil
    }

    private func findEngineScript() -> String? {
        findProjectRoot()?.appendingPathComponent("zoom_engine.py").path
    }

    private func findVenvPath() -> String? {
        guard let root = findProjectRoot() else { return nil }
        let venv = root.appendingPathComponent("venv")
        return FileManager.default.fileExists(atPath: venv.path) ? venv.path : nil
    }

    private func findPythonExecutable() -> String? {
        // Priority 1: Bundled Python inside the app bundle (distribution builds).
        // Lives at Contents/Resources/python-runtime/bin/python3.12
        if let resourcePath = Bundle.main.resourcePath {
            let bundled = "\(resourcePath)/python-runtime/bin/python3.12"
            if FileManager.default.fileExists(atPath: bundled) {
                log("[EngineManager] Using bundled Python: \(bundled)", level: .info)
                return bundled
            }
        }
        // Priority 2: Venv Python (dev builds)
        if let venv = findVenvPath() {
            let p = "\(venv)/bin/python"
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        // Priority 3: Homebrew Python 3.x (3.10+ required for X | Y type syntax)
        let brewCandidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        for p in brewCandidates where FileManager.default.fileExists(atPath: p) {
            return p
        }
        // Priority 4: System Python — macOS ships 3.9 which is too old; last resort only
        let system = "/usr/bin/python3"
        if FileManager.default.fileExists(atPath: system) { return system }
        return nil
    }
}

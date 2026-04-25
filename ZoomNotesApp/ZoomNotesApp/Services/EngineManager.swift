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
        (proc.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil
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

    /// Signal the engine to reload settings (SIGHUP).
    func reloadSettings() {
        guard let proc = process, proc.isRunning else { return }
        kill(proc.processIdentifier, SIGHUP)
        log("[EngineManager] Sent SIGHUP to engine (PID \(proc.processIdentifier))", level: .info)
    }

    // MARK: - Private

    private func spawnEngine() {
        guard let pythonPath = findPythonExecutable() else {
            DispatchQueue.main.async {
                self.error = "Python executable not found. Run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
            }
            log("[EngineManager] ERROR: Python not found", level: .error)
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
        proc.environment = env

        let stdoutPipe = Pipe()
        let stdinPipe = Pipe()
        proc.standardOutput = stdoutPipe
        proc.standardError = Pipe()  // discard stderr (Python writes logs to its own file)
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

        startQueue.async {
            do {
                try proc.run()
                log("[EngineManager] Engine started, PID \(proc.processIdentifier)", level: .info)
                DispatchQueue.main.async {
                    self.process = proc
                    self.isRunning = true
                    self.error = nil
                    self.restartCount = 0
                }

                // Monitor for unexpected exit
                proc.waitUntilExit()
                let status = proc.terminationStatus
                log("[EngineManager] Engine exited with status \(status)", level: status == 0 ? .info : .error)

                DispatchQueue.main.async {
                    self.isRunning = false
                    self.process = nil
                }

                // Auto-restart on crash (not on clean exit or SIGTERM)
                if status != 0 && proc.terminationReason != .uncaughtSignal {
                    self.scheduleRestart()
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
        log("[Engine] event=\(event.event) value=\(event.value ?? "-")", level: .debug)
        DispatchQueue.main.async {
            self.eventPublisher.send(event)
        }
    }

    private func scheduleRestart() {
        guard restartCount < maxRestarts else {
            DispatchQueue.main.async {
                self.error = "Engine crashed \(self.maxRestarts) times — not restarting. Check logs."
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
        // Strategy 1: Walk up from app bundle looking for zoom_engine.py
        var url = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<12 {
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("zoom_engine.py").path) {
                return url
            }
            let parent = url.deletingLastPathComponent()
            if parent == url { break }
            url = parent
        }

        // Strategy 2: Walk up from source file (development builds)
        var src = URL(fileURLWithPath: #file)
        for _ in 0..<8 {
            src = src.deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: src.appendingPathComponent("zoom_engine.py").path) {
                return src
            }
        }

        // Strategy 3: Known repo location
        let known = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Documents/Cursor/Zoom Meeting Notes Assistant")
        if FileManager.default.fileExists(atPath: known.appendingPathComponent("zoom_engine.py").path) {
            return known
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
        if let venv = findVenvPath() {
            let p = "\(venv)/bin/python"
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        // System Python
        let system = "/usr/bin/python3"
        if FileManager.default.fileExists(atPath: system) { return system }
        // `which python3`
        let task = Process()
        task.launchPath = "/usr/bin/which"
        task.arguments = ["python3"]
        let pipe = Pipe()
        task.standardOutput = pipe
        try? task.run()
        task.waitUntilExit()
        if task.terminationStatus == 0,
           let path = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
               .trimmingCharacters(in: .whitespacesAndNewlines),
           !path.isEmpty {
            return path
        }
        return nil
    }
}

//
//  ConsoleLogger.swift
//  ZoomNotesApp
//
//  Captures app log output and writes to ~/Library/Logs/zoom-notes/YYYY-MM-DD/app.log
//

import Foundation
import AppKit
#if os(macOS)
import Darwin
#endif

class ConsoleLogger {
    static let shared = ConsoleLogger()

    private var logFileHandle: FileHandle?
    private var isLogging = false
    private var currentDateString: String = ""

    private let logsDirectory: URL = {
        let lib = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first!
        return lib.appendingPathComponent("Logs/zoom-notes")
    }()

    private init() {
        currentDateString = todayString()
        ensureLogsDirectory()
    }

    deinit { stopLogging() }

    // MARK: - Public API

    func startLogging() {
        guard !isLogging else { return }
        ensureLogsDirectory()
        pruneOldLogDirectories(olderThanDays: 30)
        openLogFile()
        isLogging = true
        log("Zoom Notes Assistant — logging started", level: .info)
        logAppSystemInfo()
    }

    /// Delete dated log subfolders (`YYYY-MM-DD/`) under `~/Library/Logs/zoom-notes/`
    /// whose modification time is older than `olderThanDays`. Called once per
    /// app launch so logs don't grow unbounded.
    private func pruneOldLogDirectories(olderThanDays days: Int) {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(
            at: logsDirectory,
            includingPropertiesForKeys: [.contentModificationDateKey, .isDirectoryKey]
        ) else { return }
        let cutoff = Date().addingTimeInterval(-Double(days) * 86400)
        for url in entries {
            let values = try? url.resourceValues(forKeys: [.contentModificationDateKey, .isDirectoryKey])
            guard values?.isDirectory == true,
                  let mtime = values?.contentModificationDate,
                  mtime < cutoff else { continue }
            try? fm.removeItem(at: url)
        }
    }

    func stopLogging() {
        guard isLogging else { return }
        log("Zoom Notes Assistant — logging stopped", level: .info)
        closeLogFile()
        isLogging = false
    }

    func log(_ message: String, level: LogLevel = .info,
             file: String = #file, function: String = #function, line: Int = #line) {
        let fileName = (file as NSString).lastPathComponent
        let ts = timeString()
        let entry = "[\(ts)] [\(level.rawValue)] [\(fileName):\(line)] \(message)\n"
        writeEntry(entry)
        print(entry, terminator: "")
    }

    func openLogsDirectory() {
        NSWorkspace.shared.open(logsDirectory)
    }

    func openTodayLogDirectory() {
        NSWorkspace.shared.open(todayLogDirectory())
    }

    func getCurrentLogPath() -> String {
        return currentLogFilePath().path
    }

    // MARK: - Structured helpers

    func logUserAction(_ action: String, details: [String: Any]? = nil) {
        var msg = "User Action: \(action)"
        if let d = details, !d.isEmpty {
            msg += " [\(d.map { "\($0.key)=\($0.value)" }.joined(separator: ", "))]"
        }
        log(msg, level: .info)
    }

    func logAppSystemInfo() {
        log("=== App Info ===", level: .info)
        let info = appInfoString()
        for line in info.components(separatedBy: "\n") where !line.isEmpty {
            log("  \(line)", level: .info)
        }
    }

    // MARK: - Private

    private func todayString() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    private func timeString() -> String {
        DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
    }

    private func todayLogDirectory() -> URL {
        logsDirectory.appendingPathComponent(todayString())
    }

    private func currentLogFilePath() -> URL {
        todayLogDirectory().appendingPathComponent("app.log")
    }

    private func ensureLogsDirectory() {
        let dir = todayLogDirectory()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    private func openLogFile() {
        let path = currentLogFilePath()
        if !FileManager.default.fileExists(atPath: path.path) {
            FileManager.default.createFile(atPath: path.path, contents: nil)
        }
        guard let fh = FileHandle(forWritingAtPath: path.path) else { return }
        try? fh.seekToEnd()
        logFileHandle = fh
    }

    private func closeLogFile() {
        try? logFileHandle?.close()
        logFileHandle = nil
    }

    private func writeEntry(_ entry: String) {
        guard let fh = logFileHandle, let data = entry.data(using: .utf8) else { return }
        do {
            try fh.write(contentsOf: data)
            DispatchQueue.global(qos: .utility).async { try? fh.synchronize() }
        } catch {}
    }

    private func appInfoString() -> String {
        var info = ""
        let bundle = Bundle.main
        info += "App: \(bundle.infoDictionary?["CFBundleName"] as? String ?? "ZoomNotesApp")\n"
        info += "Version: \(bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0")\n"
        info += "Bundle ID: \(bundle.bundleIdentifier ?? "com.zoom-notes-assistant")\n"
        let v = ProcessInfo.processInfo.operatingSystemVersion
        info += "macOS: \(v.majorVersion).\(v.minorVersion).\(v.patchVersion)\n"
        return info
    }
}

// MARK: - Log level

enum LogLevel: String {
    case debug   = "DEBUG"
    case info    = "INFO"
    case warning = "WARNING"
    case error   = "ERROR"
}

// MARK: - Global convenience

func log(_ message: String, level: LogLevel = .info,
         file: String = #file, function: String = #function, line: Int = #line) {
    ConsoleLogger.shared.log(message, level: level, file: file, function: function, line: line)
}

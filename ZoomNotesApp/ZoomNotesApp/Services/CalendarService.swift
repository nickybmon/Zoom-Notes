//
//  CalendarService.swift
//  ZoomNotesApp
//
//  Fetches today's Apple Calendar events via osascript and writes them to
//  ~/.local/share/zoom-notes/calendar_events.json so the Python engine can
//  resolve the real meeting title when Zoom's AI Notetaker returns a generic
//  label like "Google Calendar Meeting (not synced)".
//
//  Using osascript (via Process) rather than EventKit avoids the EventKit
//  permission prompt entirely — Calendar automation uses a lighter one-time
//  "allow automation" dialog that macOS handles transparently for GUI apps.
//

import Foundation

struct CalendarEvent: Codable {
    let title: String
    let startDate: String   // "HH:MM" 24-hour local time
    let endDate: String     // "HH:MM" 24-hour local time
}

class CalendarService {
    static let shared = CalendarService()

    private var refreshTimer: Timer?

    private let script = """
tell application "Calendar"
    set todayStart to current date
    set hours of todayStart to 0
    set minutes of todayStart to 0
    set seconds of todayStart to 0
    set tomorrowStart to todayStart + (24 * 60 * 60)
    set output to ""
    repeat with cal in every calendar
        try
            set evts to every event of cal whose start date >= todayStart and start date < tomorrowStart
            repeat with evt in evts
                set s to start date of evt
                set e to end date of evt
                set sh to hours of s as string
                set sm to minutes of s as string
                if length of sh < 2 then set sh to "0" & sh
                if length of sm < 2 then set sm to "0" & sm
                set eh to hours of e as string
                set em to minutes of e as string
                if length of eh < 2 then set eh to "0" & eh
                if length of em < 2 then set em to "0" & em
                set output to output & (summary of evt) & "|" & sh & ":" & sm & "|" & eh & ":" & em & "\\n"
            end repeat
        end try
    end repeat
    return output
end tell
"""

    // MARK: - Public interface

    func start() {
        fetchAndWrite()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.fetchAndWrite()
        }
    }

    func stop() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    // MARK: - Private

    private func fetchAndWrite() {
        // NSAppleScript must run on the main thread.
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            let output = self.runOsascript()
            guard let output = output, !output.isEmpty else {
                log("[CalendarService] No events returned from Calendar", level: .debug)
                return
            }

            var events: [CalendarEvent] = []
            for line in output.components(separatedBy: "\n") {
                let parts = line.components(separatedBy: "|")
                guard parts.count == 3 else { continue }
                let title = parts[0].trimmingCharacters(in: .whitespaces)
                let start = parts[1].trimmingCharacters(in: .whitespaces)
                let end = parts[2].trimmingCharacters(in: .whitespaces)
                guard !title.isEmpty, start.count == 5, end.count == 5 else { continue }
                events.append(CalendarEvent(title: title, startDate: start, endDate: end))
            }

            self.write(events)
            log("[CalendarService] Wrote \(events.count) events to sidecar", level: .info)
        }
    }

    private func runOsascript() -> String? {
        // NSAppleScript runs in the app's own process context, which is what
        // triggers the one-time "Allow Zoom Notes to control Calendar" prompt.
        // Process/osascript subprocess runs in a different security context and
        // hangs waiting for automation permission that never arrives.
        var error: NSDictionary?
        let appleScript = NSAppleScript(source: script)
        let result = appleScript?.executeAndReturnError(&error)
        if let error = error {
            log("[CalendarService] AppleScript error: \(error["NSAppleScriptErrorMessage"] ?? "unknown")", level: .warning)
            return nil
        }
        return result?.stringValue
    }

    private func write(_ events: [CalendarEvent]) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/zoom-notes", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: nil)
        let url = dir.appendingPathComponent("calendar_events.json")
        guard let data = try? JSONEncoder().encode(events) else { return }
        try? data.write(to: url, options: .atomic)
    }
}

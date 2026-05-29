//
//  CalendarService.swift
//  ZoomNotesApp
//
//  Fetches today's Apple Calendar events via NSAppleScript and writes them to
//  ~/.local/share/zoom-notes/calendar_events.json. Used for two purposes:
//    1. The Python engine reads it to resolve meeting titles when Zoom's WAL
//       returns a generic label like "Google Calendar Meeting (not synced)".
//    2. The menu bar reads upcomingEvents() to show the upcoming meetings list
//       and populate the "Join" shortcut.
//

import Foundation
import AppKit

// Sidecar format — written to JSON, read by both Swift and Python.
struct CalendarEvent: Codable {
    let title: String
    let startDate: String   // "HH:MM" 24-hour local time
    let endDate: String     // "HH:MM" 24-hour local time
    let zoomUrl: String?    // Zoom join URL if found in invite, else nil
}

// In-memory representation with real Date objects for UI use.
struct UpcomingEvent {
    let title: String
    let start: Date
    let end: Date
    let zoomUrl: String?

    var isNow: Bool { start <= Date() && Date() < end }

    var minutesUntilStart: Int {
        max(0, Int(start.timeIntervalSinceNow / 60))
    }

    var timeLabel: String {
        if isNow { return "Now" }
        let m = minutesUntilStart
        if m < 60 { return "in \(m)m" }
        let h = m / 60, rem = m % 60
        return rem == 0 ? "in \(h)h" : "in \(h)h \(rem)m"
    }

    // Formatted start time for display, e.g. "11:00 AM"
    var startTimeString: String {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        f.amSymbol = "AM"; f.pmSymbol = "PM"
        return f.string(from: start)
    }
}

class CalendarService {
    static let shared = CalendarService()

    private var refreshTimer: Timer?

    private let sidecarURL: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/zoom-notes", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: nil)
        return dir.appendingPathComponent("calendar_events.json")
    }()

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
                -- Extract Zoom join URL from description (descriptions contain newlines
                -- so we extract here rather than passing the raw text as a field)
                set zoomUrl to ""
                try
                    set d to description of evt as string
                    if d contains "zoom.us/j/" then
                        set AppleScript's text item delimiters to "https://"
                        set httpParts to text items of d
                        set AppleScript's text item delimiters to ""
                        repeat with p in httpParts
                            set ps to p as string
                            if ps contains "zoom.us/j/" then
                                set rawUrl to "https://" & ps
                                set cleanUrl to ""
                                repeat with i from 1 to length of rawUrl
                                    set c to character i of rawUrl
                                    if c is in {" ", return, linefeed, tab, "<", quote, ">"} then exit repeat
                                    set cleanUrl to cleanUrl & c
                                end repeat
                                set zoomUrl to cleanUrl
                                exit repeat
                            end if
                        end repeat
                    end if
                end try
                set output to output & (summary of evt) & "|" & sh & ":" & sm & "|" & eh & ":" & em & "|" & zoomUrl & "\\n"
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

    func refresh() {
        fetchAndWrite()
    }

    /// Returns events that are currently active or start within `withinHours` hours,
    /// sorted by start time. Reads from the on-disk sidecar (no AppleScript call).
    func upcomingEvents(withinHours hours: Int = 4) -> [UpcomingEvent] {
        guard let data = try? Data(contentsOf: sidecarURL),
              let events = try? JSONDecoder().decode([CalendarEvent].self, from: data)
        else { return [] }

        let now = Date()
        let cutoff = now.addingTimeInterval(Double(hours) * 3600)
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: now)

        return events.compactMap { e -> UpcomingEvent? in
            guard let start = timeToDate(e.startDate, relativeTo: today),
                  let end   = timeToDate(e.endDate,   relativeTo: today)
            else { return nil }
            // Include events currently running or starting within the window
            guard end > now && start <= cutoff else { return nil }
            return UpcomingEvent(
                title: e.title,
                start: start,
                end: end,
                zoomUrl: e.zoomUrl?.isEmpty == false ? e.zoomUrl : nil
            )
        }
        .sorted { $0.start < $1.start }
    }

    // MARK: - Private

    private func timeToDate(_ hhmm: String, relativeTo dayStart: Date) -> Date? {
        guard hhmm.count == 5,
              let h = Int(hhmm.prefix(2)),
              let m = Int(hhmm.suffix(2))
        else { return nil }
        return Calendar.current.date(byAdding: .init(hour: h, minute: m), to: dayStart)
    }

    private func fetchAndWrite() {
        // Run on a background thread — NSAppleScript can block while waiting
        // for the "Allow Zoom Notes to control Calendar" permission dialog,
        // and we must not freeze the main thread / menu bar while that happens.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let output = self.runAppleScript()
            guard let output = output, !output.isEmpty else {
                log("[CalendarService] No events returned from Calendar", level: .debug)
                return
            }

            var events: [CalendarEvent] = []
            for line in output.components(separatedBy: "\n") {
                let parts = line.components(separatedBy: "|")
                guard parts.count == 4 else { continue }
                let title   = parts[0].trimmingCharacters(in: .whitespaces)
                let start   = parts[1].trimmingCharacters(in: .whitespaces)
                let end     = parts[2].trimmingCharacters(in: .whitespaces)
                let zoomUrl = parts[3].trimmingCharacters(in: .whitespaces)
                guard !title.isEmpty, start.count == 5, end.count == 5 else { continue }
                events.append(CalendarEvent(
                    title: title,
                    startDate: start,
                    endDate: end,
                    zoomUrl: zoomUrl.isEmpty ? nil : zoomUrl
                ))
            }

            // Only write if we got actual events — a failed/empty refresh should
            // never clear the sidecar and wipe the menu's upcoming meetings list.
            guard !events.isEmpty else {
                log("[CalendarService] Script returned no parseable events — keeping existing sidecar", level: .debug)
                return
            }
            self.write(events)
            log("[CalendarService] Wrote \(events.count) events to sidecar", level: .info)
        }
    }

    private func runAppleScript() -> String? {
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
        guard let data = try? JSONEncoder().encode(events) else { return }
        try? data.write(to: sidecarURL, options: .atomic)
    }
}

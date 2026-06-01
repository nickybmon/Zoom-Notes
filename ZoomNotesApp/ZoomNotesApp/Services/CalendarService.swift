//
//  CalendarService.swift
//  ZoomNotesApp
//
//  Fetches today's calendar events via EventKit and writes them to
//  ~/.local/share/zoom-notes/calendar_events.json. Used for two purposes:
//    1. The Python engine reads it to resolve meeting titles when Zoom's WAL
//       returns a generic label like "Google Calendar Meeting (not synced)".
//    2. The menu bar reads upcomingEvents() to show the upcoming meetings list
//       and populate the "Join" shortcut.
//

import EventKit
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

    var startTimeString: String {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        f.amSymbol = "AM"; f.pmSymbol = "PM"
        return f.string(from: start)
    }
}

class CalendarService: ObservableObject {
    static let shared = CalendarService()

    private let store = EKEventStore()
    private var refreshTimer: Timer?

    @Published var authorizationStatus: EKAuthorizationStatus = EKEventStore.authorizationStatus(for: .event)

    private let sidecarURL: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/zoom-notes", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: nil)
        return dir.appendingPathComponent("calendar_events.json")
    }()

    // MARK: - Public interface

    func start() {
        let status = EKEventStore.authorizationStatus(for: .event)
        if status == .notDetermined {
            // First launch — request immediately so the system prompt fires
            // without requiring the user to find Settings → Calendar.
            requestAccess()
        } else {
            updateAuthorizationStatus()
            if isAuthorized { fetchAndWrite() }
        }
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.updateAuthorizationStatus()
            if self?.isAuthorized == true { self?.fetchAndWrite() }
        }
    }

    func stop() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    func refresh() {
        updateAuthorizationStatus()
        if isAuthorized { fetchAndWrite() }
    }

    /// Trigger the system permission prompt. If already denied, opens System Settings
    /// to the Calendars privacy pane so the user can re-enable it manually.
    func requestAccess(completion: @escaping (Bool) -> Void = { _ in }) {
        let status = EKEventStore.authorizationStatus(for: .event)
        if status == .denied || status == .restricted {
            openSystemSettings()
            completion(false)
            return
        }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents { [weak self] granted, _ in
                DispatchQueue.main.async {
                    self?.updateAuthorizationStatus()
                    if granted { self?.fetchAndWrite() }
                    completion(granted)
                }
            }
        } else {
            store.requestAccess(to: .event) { [weak self] granted, _ in
                DispatchQueue.main.async {
                    self?.updateAuthorizationStatus()
                    if granted { self?.fetchAndWrite() }
                    completion(granted)
                }
            }
        }
    }

    /// Returns events that are currently active or start within `withinHours` hours,
    /// sorted by start time. Reads from the on-disk sidecar (no EventKit call).
    func upcomingEvents(withinHours hours: Int = 4) -> [UpcomingEvent] {
        guard let data = try? Data(contentsOf: sidecarURL),
              let events = try? JSONDecoder().decode([CalendarEvent].self, from: data)
        else { return [] }

        let now = Date()
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: now)

        // Reject a sidecar written on a previous day — its HH:MM times would
        // be reconstructed relative to today, surfacing old events as upcoming.
        if let attrs = try? FileManager.default.attributesOfItem(atPath: sidecarURL.path),
           let mtime = attrs[.modificationDate] as? Date,
           calendar.startOfDay(for: mtime) < today {
            return []
        }

        let cutoff = now.addingTimeInterval(Double(hours) * 3600)

        return events.compactMap { e -> UpcomingEvent? in
            guard let start = timeToDate(e.startDate, relativeTo: today),
                  let end   = timeToDate(e.endDate,   relativeTo: today)
            else { return nil }
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

    var isAuthorized: Bool {
        if #available(macOS 14.0, *) {
            return authorizationStatus == .fullAccess
        } else {
            return authorizationStatus == .authorized
        }
    }

    private func updateAuthorizationStatus() {
        let status = EKEventStore.authorizationStatus(for: .event)
        DispatchQueue.main.async { self.authorizationStatus = status }
    }

    private func fetchAndWrite() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let events = self.fetchTodayEvents()
            guard !events.isEmpty else {
                log("[CalendarService] No events returned from EventKit", level: .debug)
                return
            }
            self.write(events)
            log("[CalendarService] Wrote \(events.count) events to sidecar", level: .info)
        }
    }

    private func fetchTodayEvents() -> [CalendarEvent] {
        let cal = Calendar.current
        let now = Date()
        let startOfDay = cal.startOfDay(for: now)
        guard let endOfDay = cal.date(byAdding: .day, value: 1, to: startOfDay) else { return [] }

        let predicate = store.predicateForEvents(withStart: startOfDay, end: endOfDay, calendars: nil)
        let ekEvents = store.events(matching: predicate)

        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm"

        return ekEvents.compactMap { event -> CalendarEvent? in
            guard !event.isAllDay else { return nil }
            let title = event.title ?? ""
            guard !title.isEmpty else { return nil }
            return CalendarEvent(
                title: title,
                startDate: fmt.string(from: event.startDate),
                endDate: fmt.string(from: event.endDate),
                zoomUrl: extractZoomUrl(from: event)
            )
        }
    }

    private func extractZoomUrl(from event: EKEvent) -> String? {
        // Check the event's URL field first (set by some calendar apps directly)
        if let url = event.url, url.absoluteString.contains("zoom.us/j/") {
            return url.absoluteString
        }
        // Fall back to scanning the notes/description
        guard let notes = event.notes, notes.contains("zoom.us/j/") else { return nil }
        let pattern = "https://[^\\s<>\"]+zoom\\.us/j/[^\\s<>\"]*"
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: notes, range: NSRange(notes.startIndex..., in: notes)),
              let range = Range(match.range, in: notes)
        else { return nil }
        return String(notes[range])
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,;)>\""))
    }

    private func timeToDate(_ hhmm: String, relativeTo dayStart: Date) -> Date? {
        guard hhmm.count == 5,
              let h = Int(hhmm.prefix(2)),
              let m = Int(hhmm.suffix(2))
        else { return nil }
        return Calendar.current.date(byAdding: .init(hour: h, minute: m), to: dayStart)
    }

    private func openSystemSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars") {
            NSWorkspace.shared.open(url)
        }
    }

    private func write(_ events: [CalendarEvent]) {
        guard let data = try? JSONEncoder().encode(events) else { return }
        try? data.write(to: sidecarURL, options: .atomic)
    }
}

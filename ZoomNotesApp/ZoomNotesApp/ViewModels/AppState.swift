//
//  AppState.swift
//  ZoomNotesApp
//
//  Published engine state consumed by AppDelegate and SettingsView.
//

import Foundation
import Combine
import UserNotifications

/// Snapshot of a meeting whose note generation failed. The transcript was
/// already saved successfully — this is just the metadata needed to retry
/// the LLM call without reprocessing the WAL.
struct FailedMeeting: Equatable {
    let meetingId: String
    let title: String
    let notePath: String?
    let transcriptPath: String?
    let message: String
}

/// A persisted in-progress accumulator found at engine startup — the
/// survivor of a prior crash or a previously-failed LLM call. The
/// transcript itself MAY already be on disk (for confirmed-failed
/// meetings, transcript_path is populated and the user has the file in
/// their Notes/Transcripts folder); for crashed-mid-meeting recoveries
/// it is not. The "recover" command runs the same pipeline as a normal
/// end-of-meeting finalize: derive title, save transcript if needed,
/// run LLM, save note.
struct RecoverableMeeting: Equatable, Identifiable {
    /// Where the snapshot lives on disk, which determines retention:
    ///   .root   — live or just-failed, 24h window
    ///   .failed — confirmed-failed (LLM error), 30-day window, has sidecar
    enum Location: String {
        case root
        case failed
    }

    let meetingId: String
    let entryCount: Int
    let lastUpdated: String
    let slugHint: String
    let location: Location
    /// Set when location == .failed and the sidecar carried the title.
    let title: String?
    /// ISO timestamp when the snapshot was promoted to failed/. Used by
    /// the menu bar to render "(failed N days ago)".
    let failedAt: String?
    let lastError: String?

    var id: String { meetingId }

    /// Best human-readable label for the menu bar item. Title from sidecar
    /// wins; otherwise fall back to the speaker/timestamp slug hint.
    var displayLabel: String {
        if let t = title, !t.isEmpty { return t }
        return slugHint
    }
}

@MainActor
class AppState: ObservableObject {
    @Published var engineState: EngineState = .idle
    @Published var engineError: String?
    @Published var isEngineRunning = false
    /// True only after the startup grace period — prevents false "Engine offline"
    /// warnings in Settings during the first few seconds of launch.
    @Published var engineStartupSettled = false

    // Last completed meeting (for menu bar "Last saved" item)
    @Published var lastSavedTitle: String?
    @Published var lastSavedPath: String?
    @Published var lastSavedTranscriptPath: String?

    // Most recent meeting whose note generation failed. Drives the menu bar
    // "Retry note generation" item. Cleared on successful retry.
    @Published var lastFailedMeeting: FailedMeeting?

    // Meetings whose in-progress accumulator survived a prior crash. Emitted
    // by the engine at startup as `recovery_available` events. Drives the
    // menu bar "Recover unfinished meeting" submenu. Entries are removed:
    //   - when the user successfully recovers them (via `done` event)
    //   - when an `active` state event arrives with the same meeting_id
    //     (the IDLE→ACTIVE seed-from-snapshot path will auto-resume them)
    @Published var recoverableMeetings: [RecoverableMeeting] = []

    let engineManager = EngineManager()
    private var cancellables = Set<AnyCancellable>()

    init() {
        engineManager.$isRunning
            .assign(to: \.isEngineRunning, on: self)
            .store(in: &cancellables)

        engineManager.$error
            .assign(to: \.engineError, on: self)
            .store(in: &cancellables)

        // Route engine events to AppState
        engineManager.eventPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] event in
                self?.handleEngineEvent(event)
            }
            .store(in: &cancellables)
    }

    func startEngine() {
        Task.detached(priority: .userInitiated) { [engineManager] in
            engineManager.startEngine()
        }
        // Give the engine 15 seconds to come up before showing "offline" warnings
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            engineStartupSettled = true
        }
    }

    func stopEngine() {
        engineManager.stopEngine()
    }

    // MARK: - Event handling

    private func handleEngineEvent(_ event: EngineEvent) {
        switch event.event {
        case "ready":
            engineStartupSettled = true
            if event.zoomInstalled == false {
                engineError = "Zoom not detected. Install the Zoom desktop app, or update WAL paths in Settings → Advanced."
            } else {
                engineError = nil
            }
        case "state":
            engineStartupSettled = true
            engineState = EngineState(event.value)
            // Reaching ACTIVE means the WAL was resolved and a meeting is in
            // progress — any prior setup error (e.g. "couldn't find Zoom's
            // transcript database") is by definition stale, so clear it.
            // This is what lets the user fix Notetaker / Zoom config without
            // restarting the app to dismiss the warning banner.
            if event.value == "active" {
                engineError = nil
            }
            // If the engine just transitioned to ACTIVE for a meeting that was
            // also surfaced as recoverable, drop it from the recovery list —
            // the IDLE→ACTIVE seed-from-snapshot path will auto-resume it and
            // showing a manual "Recover" item alongside is confusing.
            if event.value == "active",
               let mid = event.meetingId,
               !mid.isEmpty {
                recoverableMeetings.removeAll { $0.meetingId == mid }
            }
        case "recovery_available":
            guard let mid = event.meetingId, !mid.isEmpty else { break }
            // De-dupe — the engine doesn't emit duplicates today, but if the
            // engine restarts and the same meeting is still on disk we don't
            // want to show two menu entries.
            guard !recoverableMeetings.contains(where: { $0.meetingId == mid }) else { break }
            let location = RecoverableMeeting.Location(
                rawValue: event.location ?? "root"
            ) ?? .root
            recoverableMeetings.append(RecoverableMeeting(
                meetingId: mid,
                entryCount: event.entryCount ?? 0,
                lastUpdated: event.lastUpdated ?? "",
                slugHint: event.slugHint ?? "Recovered meeting",
                location: location,
                title: event.title,
                failedAt: event.failedAt,
                lastError: event.lastError
            ))
        case "done":
            engineState = .idle
            if let title = event.title {
                lastSavedTitle = title
                lastSavedPath = event.path
                lastSavedTranscriptPath = event.transcriptPath
                sendNoteMadeNotification(title: title, path: event.path)
            }
            // A successful "done" supersedes any prior failure for the same
            // meeting (this is what the retry path emits on success).
            lastFailedMeeting = nil
            // And clears it from the recovery list — the user-facing
            // outcome is the same whether the meeting was just recovered
            // from a prior crash or completed normally.
            if let mid = event.meetingId, !mid.isEmpty {
                recoverableMeetings.removeAll { $0.meetingId == mid }
            }
        case "note_failed":
            engineState = .idle
            let failure = FailedMeeting(
                meetingId: event.meetingId ?? "",
                title: event.title ?? "Untitled meeting",
                notePath: event.notePath,
                transcriptPath: event.transcriptPath,
                message: event.message ?? "Unknown error"
            )
            lastFailedMeeting = failure
            // Transcript is safe — surface the partial-success state in the
            // "Last saved" item so the user sees something landed on disk.
            if let path = failure.transcriptPath, !path.isEmpty {
                lastSavedTitle = failure.title
                lastSavedPath = failure.notePath
                lastSavedTranscriptPath = path
            }
            sendNoteFailedNotification(failure: failure)
        case "error":
            engineError = event.message
            engineState = .idle
        default:
            break
        }
    }

    // MARK: - Retry

    /// Send a retry command for the most recent failed meeting (or a specific one).
    func retryFailedMeeting(_ failure: FailedMeeting? = nil) {
        let target = failure ?? lastFailedMeeting
        guard let target else { return }
        engineManager.sendCommand(["cmd": "retry", "meeting_id": target.meetingId])
    }

    /// Recover a meeting whose in-progress accumulator survived a prior crash.
    /// Mechanically identical to retry — the engine routes both through
    /// `_trigger_retry`. The two are kept distinct as separate stdin commands
    /// so the protocol intent stays explicit and the engine can later add
    /// recovery-only telemetry without entangling it with retry.
    func recoverMeeting(_ meeting: RecoverableMeeting) {
        engineManager.sendCommand(["cmd": "recover", "meeting_id": meeting.meetingId])
    }

    // MARK: - Notifications

    private func sendNoteMadeNotification(title: String, path: String?) {
        let content = UNMutableNotificationContent()
        content.title = "Meeting Notes Saved"
        content.body = title
        content.sound = .default
        content.categoryIdentifier = "MEETING_SAVED"
        if let path {
            content.userInfo = ["notePath": path]
        }
        let request = UNNotificationRequest(
            identifier: "zoom-notes-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request) { _ in }
    }

    private func sendNoteFailedNotification(failure: FailedMeeting) {
        let content = UNMutableNotificationContent()
        content.title = "Note Generation Failed"
        content.body = "\(failure.title) — transcript saved. \(failure.message)"
        content.sound = .default
        content.categoryIdentifier = "NOTE_FAILED"
        content.userInfo = [
            "meetingId": failure.meetingId,
            "transcriptPath": failure.transcriptPath ?? "",
            "notePath": failure.notePath ?? "",
        ]
        let request = UNNotificationRequest(
            identifier: "zoom-notes-failed-\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request) { _ in }
    }
}

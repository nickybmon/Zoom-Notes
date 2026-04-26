//
//  AppState.swift
//  ZoomNotesApp
//
//  Published engine state consumed by AppDelegate and SettingsView.
//

import Foundation
import Combine
import UserNotifications

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
            // One-shot setup status from the engine. Surface a clear error if
            // Zoom isn't detected so the user knows what to fix.
            if event.zoomInstalled == false {
                engineError = "Zoom not detected. Install the Zoom desktop app, or update WAL paths in Settings → Advanced."
            } else {
                engineError = nil
            }
        case "state":
            engineStartupSettled = true
            engineState = EngineState(event.value)
        case "done":
            engineState = .idle
            if let title = event.title {
                lastSavedTitle = title
                lastSavedPath = event.path
                lastSavedTranscriptPath = event.transcriptPath
                sendNoteMadeNotification(title: title, path: event.path)
            }
        case "error":
            engineError = event.message
            engineState = .idle
        default:
            break
        }
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
}

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
    }

    func stopEngine() {
        engineManager.stopEngine()
    }

    // MARK: - Event handling

    private func handleEngineEvent(_ event: EngineEvent) {
        switch event.event {
        case "state":
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

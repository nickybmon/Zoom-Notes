//
//  AppDelegate.swift
//  ZoomNotesApp
//
//  Menu bar app delegate. Manages the NSStatusItem, menu, settings window,
//  and bridges AppState engine events to visual feedback.
//
//  States map to menu bar icon:
//    idle       → doc.plaintext (grey, template)
//    active     → doc.plaintext (blue accent)
//    generating → gear (spinning-ish — static icon with tint)
//

import Cocoa
import SwiftUI
import Combine
import UserNotifications

@MainActor
class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate {
    var statusBarItem: NSStatusItem?
    var settingsWindow: NSWindow?

    let appState = AppState()
    private var cancellables = Set<AnyCancellable>()
    private var menuUpdateTimer: Timer?

    // MARK: - Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        ConsoleLogger.shared.startLogging()
        log("ZoomNotesApp launched", level: .info)
        log("Log file: \(ConsoleLogger.shared.getCurrentLogPath())", level: .info)

        requestNotificationPermission()
        setupMenuBar()
        observeAppState()

        appState.startEngine()

        menuUpdateTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateMenuBar() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        log("ZoomNotesApp terminating", level: .info)
        menuUpdateTimer?.invalidate()
        appState.stopEngine()
        ConsoleLogger.shared.stopLogging()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    // MARK: - Notification permission

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().delegate = self

        let openAction = UNNotificationAction(identifier: "OPEN_IN_FINDER", title: "Show in Finder")
        let savedCategory = UNNotificationCategory(
            identifier: "MEETING_SAVED",
            actions: [openAction],
            intentIdentifiers: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([savedCategory])
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        if response.actionIdentifier == "OPEN_IN_FINDER" || response.actionIdentifier == UNNotificationDefaultActionIdentifier {
            if let path = userInfo["notePath"] as? String {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
        }
        completionHandler()
    }

    // MARK: - Menu bar setup

    func setupMenuBar() {
        statusBarItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        guard let button = statusBarItem?.button else { return }
        button.image = menuBarImage(for: .idle)
        button.toolTip = "Zoom Notes"
        updateMenuBar()
    }

    func updateMenuBar() {
        let menu = NSMenu()
        let state = appState.engineState

        // Status header
        let statusTitle: String
        switch state {
        case .idle:       statusTitle = "Idle — waiting for meeting"
        case .active:     statusTitle = "Meeting in progress…"
        case .generating: statusTitle = "Generating notes…"
        case .unknown:    statusTitle = "Engine starting…"
        }
        let statusItem = NSMenuItem(title: statusTitle, action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        menu.addItem(statusItem)

        menu.addItem(.separator())

        // Last saved note
        if let title = appState.lastSavedTitle, !title.isEmpty {
            let lastItem = NSMenuItem(
                title: "Last saved: \(title)",
                action: #selector(openLastNoteInFinder),
                keyEquivalent: ""
            )
            lastItem.target = self
            menu.addItem(lastItem)
            menu.addItem(.separator())
        }

        // Manual trigger when active
        if state == .active {
            let genItem = NSMenuItem(
                title: "Generate Notes Now",
                action: #selector(generateNow),
                keyEquivalent: "g"
            )
            genItem.keyEquivalentModifierMask = .command
            genItem.target = self
            menu.addItem(genItem)
            menu.addItem(.separator())
        }

        // Error display
        if let err = appState.engineError {
            let errItem = NSMenuItem(title: "⚠ \(err)", action: nil, keyEquivalent: "")
            errItem.isEnabled = false
            menu.addItem(errItem)
            menu.addItem(.separator())
        }

        menu.addItem(NSMenuItem(title: "Settings…", action: #selector(showSettings), keyEquivalent: ","))
        menu.addItem(NSMenuItem(title: "Open Logs…", action: #selector(openLogs), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Zoom Notes", action: #selector(quitApp), keyEquivalent: "q"))

        menu.items.forEach { $0.target = self }
        statusBarItem?.menu = menu

        // Update icon
        if let button = statusBarItem?.button {
            button.image = menuBarImage(for: state)
            switch state {
            case .idle:
                button.contentTintColor = nil  // default white/black template rendering
                button.toolTip = appState.isEngineRunning ? "Zoom Notes — Idle" : "Zoom Notes — Engine starting…"
            case .active:
                button.contentTintColor = .controlAccentColor
                button.toolTip = "Zoom Notes — Meeting in progress"
            case .generating:
                button.contentTintColor = .systemOrange
                button.toolTip = "Zoom Notes — Generating notes…"
            case .unknown:
                button.contentTintColor = nil
                button.toolTip = "Zoom Notes — Connecting…"
            }
        }
    }

    private func menuBarImage(for state: EngineState) -> NSImage? {
        let symbolName: String
        switch state {
        case .idle, .unknown: symbolName = "doc.text"
        case .active:          symbolName = "doc.text.fill"
        case .generating:      symbolName = "doc.text.fill"
        }
        let img = NSImage(systemSymbolName: symbolName, accessibilityDescription: "Zoom Notes")
        // Template = true makes macOS render it white/dark automatically (adapts to menu bar colour)
        img?.isTemplate = true
        return img
    }

    // MARK: - Menu actions

    @objc func generateNow() {
        ConsoleLogger.shared.logUserAction("Generate Notes Now")
        appState.engineManager.sendCommand(["cmd": "generate"])
    }

    @objc func openLastNoteInFinder() {
        guard let path = appState.lastSavedPath, !path.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    @objc func showSettings() {
        if settingsWindow == nil {
            // Use NSPanel with .nonactivatingPanel so showing Settings never
            // changes the app's activation state. On macOS 26, showing a regular
            // NSWindow in an .accessory app temporarily promotes it to a regular
            // app — closing the window then terminates the app.
            let panel = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 640, height: 600),
                styleMask: [.titled, .closable, .resizable, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            panel.title = "Zoom Notes — Settings"
            panel.animationBehavior = .none
            panel.isFloatingPanel = false
            panel.becomesKeyOnlyIfNeeded = false
            panel.delegate = self
            panel.contentView = NSHostingView(
                rootView: SettingsView().environmentObject(appState)
            )
            settingsWindow = panel
        }
        // Activate the app first so the panel can become key and receive input
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.center()
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func openLogs() {
        ConsoleLogger.shared.openTodayLogDirectory()
    }

    @objc func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    // MARK: - NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        if let w = notification.object as? NSWindow, w === settingsWindow {
            settingsWindow = nil
            // Restore accessory policy so the app disappears from the Dock
            NSApp.setActivationPolicy(.accessory)
            // Signal engine to reload settings
            appState.engineManager.reloadSettings()
        }
    }

    // MARK: - AppState observation

    private func observeAppState() {
        appState.$engineState
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)

        appState.$engineError
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)

        appState.$isEngineRunning
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)

        appState.$lastSavedTitle
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)
    }
}

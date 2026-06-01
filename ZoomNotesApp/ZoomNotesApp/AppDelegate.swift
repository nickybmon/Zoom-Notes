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
class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, @preconcurrency UNUserNotificationCenterDelegate {
    var statusBarItem: NSStatusItem?
    var settingsWindow: NSWindow?
    var onboardingWindow: NSWindow?

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
        CalendarService.shared.start()
        setupMenuBar()
        observeAppState()
        showOnboardingIfNeeded()

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

        let retryAction = UNNotificationAction(
            identifier: "RETRY_NOTE_GEN",
            title: "Retry",
            options: [.foreground]
        )
        let showTranscriptAction = UNNotificationAction(
            identifier: "SHOW_TRANSCRIPT",
            title: "Show transcript"
        )
        let failedCategory = UNNotificationCategory(
            identifier: "NOTE_FAILED",
            actions: [retryAction, showTranscriptAction],
            intentIdentifiers: []
        )

        UNUserNotificationCenter.current().setNotificationCategories([savedCategory, failedCategory])
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        switch response.actionIdentifier {
        case "OPEN_IN_FINDER", UNNotificationDefaultActionIdentifier:
            if let path = userInfo["notePath"] as? String, !path.isEmpty {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            } else if let path = userInfo["transcriptPath"] as? String, !path.isEmpty {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
        case "RETRY_NOTE_GEN":
            ConsoleLogger.shared.logUserAction("Retry note generation (from notification)")
            Task { @MainActor in self.appState.retryFailedMeeting() }
        case "SHOW_TRANSCRIPT":
            if let path = userInfo["transcriptPath"] as? String, !path.isEmpty {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
        default:
            break
        }
        completionHandler()
    }

    // MARK: - Menu bar setup

    func setupMenuBar() {
        statusBarItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        guard let button = statusBarItem?.button else { return }
        button.image = menuBarIcon()
        button.toolTip = "Zoom Notes"
        updateMenuBar()
    }

    /// Returns the menu bar template image — custom glyph asset, SF Symbol fallback.
    private func menuBarIcon() -> NSImage {
        // Custom glyph from asset catalog (black strokes on transparent, isTemplate
        // makes macOS invert to white on dark menu bars automatically)
        if let img = NSImage(named: "MenuBarIcon") {
            img.isTemplate = false  // white strokes — let macOS render as-is
            return img
        }
        // SF Symbol fallback
        if let sf = NSImage(systemSymbolName: "doc.text", accessibilityDescription: "Zoom Notes") {
            sf.isTemplate = true
            return sf
        }
        // Last-resort drawn fallback
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            NSColor.black.setFill()
            let path = NSBezierPath()
            let (w, h) = (rect.width, rect.height)
            let m: CGFloat = 2, fold: CGFloat = 5
            path.move(to: NSPoint(x: m, y: m))
            path.line(to: NSPoint(x: m, y: h - m))
            path.line(to: NSPoint(x: w - m - fold, y: h - m))
            path.line(to: NSPoint(x: w - m, y: h - m - fold))
            path.line(to: NSPoint(x: w - m, y: m))
            path.close()
            path.fill()
            return true
        }
        img.isTemplate = true
        return img
    }

    func updateMenuBar() {
        let menu = NSMenu()
        let state = appState.engineState
        let upcoming = CalendarService.shared.upcomingEvents(withinHours: 4)
        let nextMeeting = upcoming.first

        // ── Status header ────────────────────────────────────────────────────
        let statusTitle: String
        switch state {
        case .idle:
            if let next = nextMeeting {
                statusTitle = next.isNow
                    ? "\(next.title) — Now"
                    : "\(next.title) — \(next.startTimeString) (\(next.timeLabel))"
            } else {
                statusTitle = "Idle — waiting for meeting"
            }
        case .active:     statusTitle = "Meeting in progress…"
        case .generating: statusTitle = "Generating notes…"
        case .unknown:    statusTitle = "Engine starting…"
        }
        let statusItem = NSMenuItem(title: statusTitle, action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        menu.addItem(statusItem)

        // ── Upcoming meetings ────────────────────────────────────────────────
        if !upcoming.isEmpty && state == .idle {
            menu.addItem(.separator())

            // Show up to 4 events; bold/highlight the one happening now
            for event in upcoming.prefix(4) {
                let label = event.isNow
                    ? "\(event.title) — Now"
                    : "\(event.title) — \(event.startTimeString) (\(event.timeLabel))"
                let item = NSMenuItem(title: label, action: nil, keyEquivalent: "")
                item.isEnabled = false
                if event.isNow {
                    item.attributedTitle = NSAttributedString(
                        string: label,
                        attributes: [.font: NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)]
                    )
                }
                menu.addItem(item)
            }

            // Join shortcut — first upcoming event with a Zoom URL
            if let joinable = upcoming.first(where: { $0.zoomUrl != nil }),
               let urlStr = joinable.zoomUrl {
                menu.addItem(.separator())
                let joinItem = NSMenuItem(
                    title: "Join: \(joinable.title)",
                    action: #selector(joinMeeting(_:)),
                    keyEquivalent: ""
                )
                joinItem.representedObject = urlStr
                joinItem.target = self
                menu.addItem(joinItem)
            }

            let refreshItem = NSMenuItem(
                title: "Refresh Calendar",
                action: #selector(refreshCalendar),
                keyEquivalent: ""
            )
            refreshItem.target = self
            menu.addItem(refreshItem)
        }

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

        // Recoverable meetings — in-progress accumulators left on disk by a
        // prior crashed engine session OR by a previous LLM failure that's
        // still inside its 30-day failed/ retention window. Single recovery
        // shows inline; two or more collapse into a submenu.
        if !appState.recoverableMeetings.isEmpty {
            if appState.recoverableMeetings.count == 1 {
                let rec = appState.recoverableMeetings[0]
                let recoverItem = NSMenuItem(
                    title: recoverableMenuTitle(for: rec),
                    action: #selector(recoverFromMenu(_:)),
                    keyEquivalent: ""
                )
                recoverItem.representedObject = rec.meetingId
                recoverItem.target = self
                menu.addItem(recoverItem)

                let detailText = recoverableSubtitleDetail(for: rec)
                if !detailText.isEmpty {
                    let detail = NSMenuItem(
                        title: "    \(detailText)",
                        action: nil,
                        keyEquivalent: ""
                    )
                    detail.isEnabled = false
                    menu.addItem(detail)
                }

                let discardItem = NSMenuItem(
                    title: "Discard",
                    action: #selector(dismissFromMenu(_:)),
                    keyEquivalent: ""
                )
                discardItem.representedObject = rec.meetingId
                discardItem.target = self
                menu.addItem(discardItem)
            } else {
                let parent = NSMenuItem(
                    title: "Unfinished meetings (\(appState.recoverableMeetings.count))",
                    action: nil,
                    keyEquivalent: ""
                )
                let submenu = NSMenu()
                for rec in appState.recoverableMeetings {
                    let itemParent = NSMenuItem(title: rec.displayLabel, action: nil, keyEquivalent: "")
                    let itemSubmenu = NSMenu()

                    let recoverAction = NSMenuItem(
                        title: recoverableMenuTitle(for: rec),
                        action: #selector(recoverFromMenu(_:)),
                        keyEquivalent: ""
                    )
                    recoverAction.representedObject = rec.meetingId
                    recoverAction.target = self
                    itemSubmenu.addItem(recoverAction)

                    let discardAction = NSMenuItem(
                        title: "Discard",
                        action: #selector(dismissFromMenu(_:)),
                        keyEquivalent: ""
                    )
                    discardAction.representedObject = rec.meetingId
                    discardAction.target = self
                    itemSubmenu.addItem(discardAction)

                    itemParent.submenu = itemSubmenu
                    submenu.addItem(itemParent)
                }
                submenu.addItem(.separator())
                let discardAll = NSMenuItem(
                    title: "Discard All",
                    action: #selector(dismissAllFromMenu),
                    keyEquivalent: ""
                )
                discardAll.target = self
                submenu.addItem(discardAll)
                parent.submenu = submenu
                menu.addItem(parent)
            }
            menu.addItem(.separator())
        }

        // Retry note generation if a failed meeting is queued. The transcript
        // is already on disk; this just re-runs the LLM call.
        if let failure = appState.lastFailedMeeting {
            let retryItem = NSMenuItem(
                title: "Retry note generation: \(failure.title)",
                action: #selector(retryFailed),
                keyEquivalent: "r"
            )
            retryItem.keyEquivalentModifierMask = .command
            retryItem.target = self
            menu.addItem(retryItem)

            let detailItem = NSMenuItem(
                title: "    \(failure.message)",
                action: nil,
                keyEquivalent: ""
            )
            detailItem.isEnabled = false
            menu.addItem(detailItem)

            if let path = failure.transcriptPath, !path.isEmpty {
                let openTranscript = NSMenuItem(
                    title: "Show transcript in Finder",
                    action: #selector(openLastTranscriptInFinder),
                    keyEquivalent: ""
                )
                openTranscript.target = self
                menu.addItem(openTranscript)
            }
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

        // Error display — show errors that are actionable; suppress transient/auth noise
        if let err = appState.engineError,
           !err.contains("401"),
           !err.contains("API key"),
           !err.contains("quota exceeded") {
            let errItem = NSMenuItem(title: "⚠ \(err)", action: nil, keyEquivalent: "")
            errItem.isEnabled = false
            menu.addItem(errItem)
            menu.addItem(.separator())
        }

        menu.addItem(NSMenuItem(title: "Settings…", action: #selector(showSettings), keyEquivalent: ","))
        menu.addItem(NSMenuItem(title: "Open Logs…", action: #selector(openLogs), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Clear Meeting Cache", action: #selector(clearCache), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Zoom Notes", action: #selector(quitApp), keyEquivalent: "q"))

        menu.items.forEach { $0.target = self }
        statusBarItem?.menu = menu

        // Update status bar button — show next meeting name + countdown when idle
        if let button = statusBarItem?.button {
            button.image = menuBarIcon()
            switch state {
            case .idle, .unknown:
                button.contentTintColor = nil
                if let next = nextMeeting, state == .idle {
                    let label = next.isNow ? "\(next.title) — Now" : "\(next.title) · \(next.timeLabel)"
                    button.title = "  \(label)"
                    button.imagePosition = .imageLeft
                    button.toolTip = next.isNow ? "Zoom Notes — \(next.title)" : "Zoom Notes — Next: \(next.title)"
                } else {
                    button.title = ""
                    button.imagePosition = .imageOnly
                    button.toolTip = appState.isEngineRunning ? "Zoom Notes — Idle" : "Zoom Notes — Engine starting…"
                }
            case .active:
                button.title = ""
                button.imagePosition = .imageOnly
                button.contentTintColor = .controlAccentColor
                button.toolTip = "Zoom Notes — Meeting in progress"
            case .generating:
                button.title = ""
                button.imagePosition = .imageOnly
                button.contentTintColor = .systemOrange
                button.toolTip = "Zoom Notes — Generating notes…"
            }
        }
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

    @objc func openLastTranscriptInFinder() {
        let path = appState.lastFailedMeeting?.transcriptPath
            ?? appState.lastSavedTranscriptPath
        guard let path, !path.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    @objc func retryFailed() {
        ConsoleLogger.shared.logUserAction("Retry note generation")
        appState.retryFailedMeeting()
    }

    @objc func recoverFromMenu(_ sender: NSMenuItem) {
        guard let mid = sender.representedObject as? String,
              let rec = appState.recoverableMeetings.first(where: { $0.meetingId == mid }) else {
            return
        }
        ConsoleLogger.shared.logUserAction("Recover unfinished meeting (\(rec.entryCount) entries, \(rec.location.rawValue))")
        appState.recoverMeeting(rec)
    }

    @objc func dismissFromMenu(_ sender: NSMenuItem) {
        guard let mid = sender.representedObject as? String,
              let rec = appState.recoverableMeetings.first(where: { $0.meetingId == mid }) else {
            return
        }
        ConsoleLogger.shared.logUserAction("Discard unfinished meeting (\(rec.location.rawValue))")
        appState.dismissMeeting(rec)
    }

    @objc func dismissAllFromMenu() {
        ConsoleLogger.shared.logUserAction("Discard all unfinished meetings (\(appState.recoverableMeetings.count))")
        appState.dismissAllRecoverableMeetings()
    }

    @objc func clearCache() {
        ConsoleLogger.shared.logUserAction("Clear meeting cache")
        appState.clearMeetingCache()
    }

    /// The primary clickable label for a recoverable meeting in the menu.
    /// Failed-bucket entries get a "(failed N days ago)" suffix to communicate
    /// urgency / age; root entries (live crashes) get the entry count which
    /// is more useful for those since there's no failure history to surface.
    private func recoverableMenuTitle(for rec: RecoverableMeeting) -> String {
        switch rec.location {
        case .failed:
            let label = rec.displayLabel
            if let suffix = relativeAgeSuffix(rec.failedAt) {
                return "Recover failed meeting: \(label) (failed \(suffix))"
            }
            return "Recover failed meeting: \(label)"
        case .root:
            return "Recover unfinished meeting: \(rec.displayLabel) (\(rec.entryCount) entries)"
        }
    }

    /// Optional subtitle line shown beneath the recover item when there's
    /// only one recoverable meeting. For failed bucket entries we surface
    /// the LLM error so the user knows what to fix before retrying.
    private func recoverableSubtitleDetail(for rec: RecoverableMeeting) -> String {
        switch rec.location {
        case .failed:
            let reason = rec.lastError ?? rec.slugHint
            return reason.isEmpty ? rec.slugHint : "Previous error: \(reason)"
        case .root:
            return rec.slugHint
        }
    }

    /// Render an ISO8601 timestamp as "5 minutes ago", "3 days ago", etc.
    /// Returns nil if the input is missing or unparseable so the caller can
    /// fall back gracefully.
    private func relativeAgeSuffix(_ iso: String?) -> String? {
        guard let iso, !iso.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        var date = formatter.date(from: iso)
        if date == nil {
            // Engine emits with timespec='seconds' which omits the timezone
            // suffix — try the no-fractional, no-timezone variant too.
            let fallback = DateFormatter()
            fallback.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            fallback.timeZone = .current
            fallback.locale = Locale(identifier: "en_US_POSIX")
            date = fallback.date(from: iso)
        }
        guard let date else { return nil }
        let relative = RelativeDateTimeFormatter()
        relative.unitsStyle = .full
        return relative.localizedString(for: date, relativeTo: Date())
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
                rootView: SettingsView(onSave: { [weak self] in
                    self?.appState.engineManager.reloadSettings()
                }).environmentObject(appState)
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

    @objc func joinMeeting(_ sender: NSMenuItem) {
        guard let urlStr = sender.representedObject as? String,
              let url = URL(string: urlStr) else { return }
        ConsoleLogger.shared.logUserAction("Join meeting from calendar")
        NSWorkspace.shared.open(url)
    }

    @objc func refreshCalendar() {
        ConsoleLogger.shared.logUserAction("Refresh Calendar")
        CalendarService.shared.refresh()
        // Trigger a menu rebuild after a short delay to show updated events
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            self?.updateMenuBar()
        }
    }

    @objc func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    // MARK: - Onboarding

    private static let onboardingKey = "hasCompletedOnboarding.v1"

    private func showOnboardingIfNeeded() {
        guard !UserDefaults.standard.bool(forKey: Self.onboardingKey) else { return }

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 580),
            styleMask: [.titled, .closable, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.title = ""
        panel.titlebarAppearsTransparent = true
        panel.animationBehavior = .alertPanel
        panel.isFloatingPanel = false
        panel.delegate = self
        panel.contentView = NSHostingView(
            rootView: OnboardingView(
                onOpenSettings: { [weak self] in
                    self?.dismissOnboarding()
                    self?.showSettings()
                },
                onDismiss: { [weak self] in
                    self?.dismissOnboarding()
                }
            )
        )
        onboardingWindow = panel

        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        panel.center()
        panel.makeKeyAndOrderFront(nil)
    }

    private func dismissOnboarding() {
        UserDefaults.standard.set(true, forKey: Self.onboardingKey)
        onboardingWindow?.close()
        onboardingWindow = nil
        NSApp.setActivationPolicy(.accessory)
    }

    // MARK: - Privacy disclaimer

    private static let privacyDisclaimerKey = "hasSeenPrivacyDisclaimer.v1"

    private func showPrivacyDisclaimerIfNeeded() {
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: Self.privacyDisclaimerKey) else { return }

        let alert = NSAlert()
        alert.messageText = "Heads up: transcripts are sent to your LLM provider"
        alert.informativeText = """
        Zoom Notes generates meeting summaries by sending the full transcript \
        of each meeting to whichever LLM provider you configure (Claude, OpenAI, \
        or Gemini by default). Anything spoken in the meeting — including \
        sensitive or confidential content — is included.

        If you need local-only processing, choose Ollama in Settings → API / LLM. \
        Ollama runs entirely on your Mac and never sends data to a third party.
        """
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Got it")
        alert.addButton(withTitle: "Open Settings")

        // Make sure the alert is visible by briefly going active.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        NSApp.setActivationPolicy(.accessory)

        defaults.set(true, forKey: Self.privacyDisclaimerKey)

        if response == .alertSecondButtonReturn {
            showSettings()
        }
    }

    // MARK: - NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        guard let w = notification.object as? NSWindow else { return }
        if w === settingsWindow {
            settingsWindow = nil
            NSApp.setActivationPolicy(.accessory)
        } else if w === onboardingWindow {
            // Closed via red X — treat as dismiss
            UserDefaults.standard.set(true, forKey: Self.onboardingKey)
            onboardingWindow = nil
            NSApp.setActivationPolicy(.accessory)
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

        appState.$lastFailedMeeting
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)

        appState.$recoverableMeetings
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateMenuBar() }
            .store(in: &cancellables)
    }
}

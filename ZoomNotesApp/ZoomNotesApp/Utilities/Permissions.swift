//
//  Permissions.swift
//  ZoomNotesApp
//
//  Notification permission helpers for Zoom Notes Assistant.
//

import Foundation
import AppKit
import UserNotifications

enum PermissionStatus {
    case notDetermined
    case denied
    case granted
}

class Permissions {
    static func requestNotificationPermission(completion: @escaping (Bool) -> Void = { _ in }) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async { completion(granted) }
        }
    }

    static func openNotificationSettings() {
        let urlString: String
        if #available(macOS 13.0, *) {
            urlString = "x-apple.systemsettings:com.apple.preference.notifications"
        } else {
            urlString = "x-apple.systempreferences:com.apple.preference.notifications"
        }
        if let url = URL(string: urlString) {
            NSWorkspace.shared.open(url)
        }
    }
}

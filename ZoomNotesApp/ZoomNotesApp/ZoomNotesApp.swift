//
//  ZoomNotesApp.swift
//  ZoomNotesApp
//

import SwiftUI

@main
struct ZoomNotesApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // Menu bar only. Settings opened manually by AppDelegate.showSettings()
        // via NSPanel — avoids SwiftUI Settings scene lifecycle issues on macOS 26.
        Settings {
            EmptyView()
        }
    }
}

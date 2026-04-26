//
//  EngineEvent.swift
//  ZoomNotesApp
//
//  Codable structs for newline-delimited JSON events emitted by zoom_engine.py on stdout.
//

import Foundation

/// Top-level event envelope from the Python engine.
struct EngineEvent: Decodable {
    let event: String

    // Present on "state" events
    let value: String?
    let meetingId: String?

    // Present on "done" events
    let title: String?
    let path: String?
    let transcriptPath: String?
    let attendees: [String]?

    // Present on "error" events
    let message: String?

    // Present on "ready" events (one-shot at engine startup)
    let zoomInstalled: Bool?
    let walPath: String?

    enum CodingKeys: String, CodingKey {
        case event
        case value
        case meetingId = "meeting_id"
        case title
        case path
        case transcriptPath = "transcript_path"
        case attendees
        case message
        case zoomInstalled = "zoom_installed"
        case walPath = "wal_path"
    }
}

/// Engine state values carried in `{"event": "state", "value": "..."}` events.
enum EngineState: String {
    case idle
    case active
    case generating
    case unknown

    init(_ rawValue: String?) {
        switch rawValue {
        case "idle":       self = .idle
        case "active":     self = .active
        case "generating": self = .generating
        default:           self = .unknown
        }
    }
}

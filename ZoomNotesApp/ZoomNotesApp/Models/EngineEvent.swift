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

    // Present on "error" and "note_failed" events
    let message: String?

    // Present on "note_failed" events
    let notePath: String?

    // Present on "ready" events (one-shot at engine startup)
    let zoomInstalled: Bool?
    let walPath: String?

    // Present on "diag" events (Phase 6 diagnostics; ignored by most consumers)
    let kind: String?
    let count: Int?

    // Present on "recovery_available" events (one per persisted accumulator
    // found at engine startup — survivors of a prior crash).
    let entryCount: Int?
    let lastUpdated: String?
    let slugHint: String?

    enum CodingKeys: String, CodingKey {
        case event
        case value
        case meetingId = "meeting_id"
        case title
        case path
        case notePath = "note_path"
        case transcriptPath = "transcript_path"
        case attendees
        case message
        case zoomInstalled = "zoom_installed"
        case walPath = "wal_path"
        case kind
        case count
        case entryCount = "entry_count"
        case lastUpdated = "last_updated"
        case slugHint = "slug_hint"
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

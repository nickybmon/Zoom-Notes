//
//  SettingsView.swift
//  ZoomNotesApp
//
//  Native macOS settings window — sidebar + tabbed content.
//  Mirrors LMA's SettingsView structure, adapted for Zoom Notes config schema.
//

import SwiftUI
import AppKit
import EventKit

// MARK: - Navigation model

private struct NavItem: Identifiable {
    let id: String
    let label: String
    let icon: String
}

private let navItems: [NavItem] = [
    NavItem(id: "llm",      label: "API / LLM",  icon: "brain"),
    NavItem(id: "output",   label: "Output",      icon: "square.and.arrow.up"),
    NavItem(id: "prompt",   label: "Prompt",      icon: "text.quote"),
    NavItem(id: "calendar", label: "Calendar",    icon: "calendar"),
    NavItem(id: "advanced", label: "Advanced",    icon: "gearshape.2"),
]

// MARK: - Root view

struct SettingsView: View {
    var onSave: (() -> Void)? = nil
    @StateObject private var vm = SettingsViewModel()
    @EnvironmentObject var appState: AppState
    @State private var selectedTab: String = "llm"

    var body: some View {
        HStack(spacing: 0) {
            // ── Sidebar ──────────────────────────────────────────────────
            VStack(alignment: .leading, spacing: 2) {
                Text("Settings")
                    .font(.headline)
                    .padding(.horizontal, 16)
                    .padding(.top, 20)
                    .padding(.bottom, 10)

                ForEach(navItems) { item in
                    SidebarRow(item: item, isSelected: selectedTab == item.id)
                        .onTapGesture { selectedTab = item.id }
                }

                Spacer()

                if appState.engineStartupSettled && !appState.isEngineRunning {
                    HStack(spacing: 6) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.orange)
                        Text("Engine offline").font(.caption2).foregroundColor(.secondary)
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 14)
                }
            }
            .frame(width: 168)
            .background(.background.opacity(0.6))

            Divider()

            // ── Content ──────────────────────────────────────────────────
            VStack(spacing: 0) {
                ScrollView {
                    Group {
                        switch selectedTab {
                        case "llm":      LLMTab(vm: vm)
                        case "output":   OutputTab(vm: vm)
                        case "prompt":   PromptTab(vm: vm)
                        case "calendar": CalendarTab()
                        default:         AdvancedTab(vm: vm)
                        }
                    }
                    .padding(.horizontal, 4)
                    .padding(.top, 4)
                }

                Divider()

                // ── Footer save bar ───────────────────────────────────────
                HStack {
                    if let e = vm.error {
                        Image(systemName: "xmark.circle.fill").foregroundColor(.red)
                        Text(e).font(.caption).foregroundColor(.red).lineLimit(1)
                    }
                    Spacer()
                    if vm.saveSuccess {
                        Text("Saved ✓").foregroundColor(.green).font(.callout)
                    }
                    Button(vm.isSaving ? "Saving…" : "Save") {
                        Task { await vm.saveConfig() }
                    }
                    .disabled(vm.isSaving)
                    .buttonStyle(.borderedProminent)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
        }
        .frame(minWidth: 640, idealWidth: 640, minHeight: 560, idealHeight: 600)
        .task {
            vm.onSave = onSave
            await vm.loadConfig()
        }
    }
}

// MARK: - Sidebar row

private struct SidebarRow: View {
    let item: NavItem
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.icon)
                .frame(width: 20, height: 20)
                .foregroundColor(isSelected ? .accentColor : .secondary)
            Text(item.label)
                .font(.system(size: 13, weight: isSelected ? .semibold : .regular))
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 7)
                .fill(isSelected ? Color.accentColor.opacity(0.12) : Color.clear)
        )
        .padding(.horizontal, 6)
        .contentShape(Rectangle())
        .onHover { inside in
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
    }
}

// MARK: - LLM Tab

private struct LLMTab: View {
    @ObservedObject var vm: SettingsViewModel

    var body: some View {
        Form {
            Section("Provider") {
                Picker("LLM Provider", selection: Binding(
                    get: { vm.config.llmProvider },
                    set: { newVal in
                        vm.config.llmProvider = newVal
                        vm.config.llmModel = defaultModel(for: newVal)
                        if newVal == "ollama" { Task { await vm.loadOllamaModels() } }
                    }
                )) {
                    Text("Claude (Anthropic)").tag("claude")
                    Text("OpenAI").tag("openai")
                    Text("Gemini (Google)").tag("gemini")
                    Text("Ollama (local)").tag("ollama")
                }
                .pickerStyle(.radioGroup)
            }

            Section("Model") {
                switch vm.config.llmProvider {
                case "claude":
                    ModelPicker(
                        selection: $vm.config.llmModel,
                        knownModels: [
                            ("claude-opus-4-5", "Claude Opus 4.5"),
                            ("claude-sonnet-4-6", "Claude Sonnet 4.6 (recommended)"),
                            ("claude-haiku-4-5", "Claude Haiku 4.5 (fastest)"),
                        ]
                    )
                case "openai":
                    ModelPicker(
                        selection: $vm.config.llmModel,
                        knownModels: [
                            ("gpt-4o", "GPT-4o (recommended)"),
                            ("gpt-4o-mini", "GPT-4o mini"),
                            ("o1", "o1"),
                            ("o3-mini", "o3-mini"),
                        ]
                    )
                case "gemini":
                    ModelPicker(
                        selection: $vm.config.llmModel,
                        knownModels: [
                            ("gemini-2.0-flash", "Gemini 2.0 Flash (recommended)"),
                            ("gemini-1.5-pro", "Gemini 1.5 Pro"),
                        ]
                    )
                case "ollama":
                    if vm.ollamaModels.isEmpty {
                        HStack {
                            Text(vm.ollamaModelsError ?? "No models found").font(.caption).foregroundColor(.secondary)
                            Spacer()
                            Button("Refresh") { Task { await vm.loadOllamaModels() } }.font(.caption)
                        }
                        TextField("Model name", text: $vm.config.llmModel)
                    } else {
                        Picker("Model", selection: $vm.config.llmModel) {
                            ForEach(vm.ollamaModels, id: \.self) { Text($0).tag($0) }
                        }
                        HStack {
                            Button("Refresh Models") { Task { await vm.loadOllamaModels() } }
                            if let err = vm.ollamaModelsError {
                                Text(err).font(.caption).foregroundColor(.secondary)
                            }
                        }
                    }
                default:
                    TextField("Model name", text: $vm.config.llmModel)
                }
            }

            Section("API Key") {
                switch vm.config.llmProvider {
                case "claude":
                    SecureField("Anthropic API Key", text: $vm.claudeApiKey)
                        .help("Get your key at console.anthropic.com")
                    Text("Stored securely in macOS Keychain.")
                        .font(.caption).foregroundColor(.secondary)
                case "openai":
                    SecureField("OpenAI API Key", text: $vm.openaiApiKey)
                        .help("Get your key at platform.openai.com")
                    Text("Stored securely in macOS Keychain.")
                        .font(.caption).foregroundColor(.secondary)
                case "gemini":
                    SecureField("Gemini API Key", text: $vm.geminiApiKey)
                        .help("Get your key at aistudio.google.com/apikey")
                    Text("Stored securely in macOS Keychain.")
                        .font(.caption).foregroundColor(.secondary)
                case "ollama":
                    HStack(spacing: 6) {
                        Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
                        Text("Ollama runs locally — no API key required.")
                            .font(.caption).foregroundColor(.secondary)
                    }
                default:
                    EmptyView()
                }
            }

            Section("Connection") {
                HStack {
                    Button(vm.isTesting ? "Testing…" : "Test Connection") {
                        Task { await vm.testConnection() }
                    }
                    .disabled(vm.isTesting)
                    if vm.isTesting {
                        ProgressView().scaleEffect(0.7)
                    }
                    if let result = vm.testResult {
                        Text(result)
                            .font(.caption)
                            .foregroundColor(result.hasPrefix("✓") ? .green : .red)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private func defaultModel(for provider: String) -> String {
        switch provider {
        case "claude":  return "claude-sonnet-4-6"
        case "openai":  return "gpt-4o"
        case "gemini":  return "gemini-2.0-flash"
        case "ollama":  return ""
        default:        return ""
        }
    }
}

/// A model picker that falls back to a free-text field when the user wants to
/// type a model id we don't know about — avoids requiring an app release every
/// time a provider ships a new model.
private struct ModelPicker: View {
    @Binding var selection: String
    let knownModels: [(id: String, label: String)]

    @State private var useCustom = false
    @State private var customValue: String = ""

    var body: some View {
        Group {
            if useCustom {
                HStack {
                    TextField("Model id", text: Binding(
                        get: { customValue },
                        set: {
                            customValue = $0
                            selection = $0
                        }
                    ))
                    .font(.system(.body, design: .monospaced))
                    Button("Use list") {
                        useCustom = false
                        if let first = knownModels.first {
                            selection = first.id
                        }
                    }
                    .font(.caption)
                }
            } else {
                HStack {
                    Picker("Model", selection: $selection) {
                        ForEach(knownModels, id: \.id) { entry in
                            Text(entry.label).tag(entry.id)
                        }
                    }
                    Button("Custom…") {
                        customValue = selection
                        useCustom = true
                    }
                    .font(.caption)
                }
            }
        }
        .onAppear {
            // If the saved model id isn't in our list, start in custom mode.
            if !knownModels.contains(where: { $0.id == selection }) && !selection.isEmpty {
                customValue = selection
                useCustom = true
            }
        }
    }
}

// MARK: - Output Tab

private struct OutputTab: View {
    @ObservedObject var vm: SettingsViewModel

    var body: some View {
        Form {
            Section("Save Location") {
                HStack {
                    TextField("Notes folder", text: $vm.config.notesDir)
                    Button("Browse…") { browse { path in
                        if let path { vm.config.notesDir = path }
                    }}
                }
                .help("Absolute path where meeting notes (.md) are saved")

                HStack {
                    TextField("Transcripts folder", text: $vm.config.transcriptsDir)
                    Button("Browse…") { browse { path in
                        if let path { vm.config.transcriptsDir = path }
                    }}
                }
                .help("Absolute path where transcript files are saved")
            }

            Section("Folder Structure") {
                Picker("Date subfolders", selection: $vm.config.subfolderPattern) {
                    Text("None — all files in one folder").tag("none")
                    Text("By day — YYYY-MM-DD/").tag("day")
                    Text("By month — YYYY-MM/").tag("month")
                }
            }

            Section("Filename Patterns") {
                TextField("Notes filename", text: $vm.config.filenamePattern)
                    .help("Tokens: {title} = meeting title   {date} = YYYY-MM-DD")
                TextField("Transcript filename", text: $vm.config.transcriptFilenamePattern)
                    .help("Tokens: {title} = meeting title   {date} = YYYY-MM-DD")
                Text("{title} = meeting title   {date} = YYYY-MM-DD")
                    .font(.caption).foregroundColor(.secondary)
            }

            Section("Frontmatter") {
                FrontmatterPropertiesSection(vm: vm)
            }
        }
        .formStyle(.grouped)
    }

    private func browse(completion: @escaping (String?) -> Void) {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        p.prompt = "Choose"
        completion(p.runModal() == .OK ? p.url?.path : nil)
    }
}

// MARK: - Prompt Tab

private struct PromptTab: View {
    @ObservedObject var vm: SettingsViewModel
    @State private var showingResetAlert = false

    private static let defaultPrompt = """
You are a meticulous meeting notetaker. Produce detailed, well-structured meeting notes from the transcript. Be thorough — a reader who wasn't in the meeting should come away with a complete picture of what was discussed, decided, and committed to.

Use this structure exactly. Include every section even if brief.

## Overview
2-4 sentences capturing the purpose and outcome of the meeting. Who was involved, what was the core focus, what was resolved or left open.

## Attendees
Bullet list of attendee names (use the speaker names from the transcript).

## Topics Discussed
A sequenced list of the main topics covered. For each topic, 1-3 sentences on what was said — include specific details, numbers, names, and context. Don't collapse important nuance into vague summaries.

Format:
- **[Topic name]** — [What was discussed. Be specific.]

## Key Decisions
Decisions that were explicitly made or agreed upon. If none, write "No explicit decisions recorded."

Format:
- [Decision] — [Who made it or who it affects, if clear]

## Action Items
A table of all commitments, tasks, and follow-ups. Include owner, task description, and due date if mentioned.

| Owner | Task | Due Date |
|-------|------|----------|
| [name] | [what they committed to] | [date or null] |

## Open Questions
Unresolved questions, decisions deferred, or topics that need follow-up. Omit this section entirely if none.

## Notes
Any additional context, background, or detail worth capturing that didn't fit above. Omit if nothing relevant.

---

Output only the meeting notes. No preamble, no explanation, no meta-commentary.
"""

    private var promptBinding: Binding<String> {
        Binding(
            get: { vm.config.systemPrompt ?? Self.defaultPrompt },
            set: {
                vm.config.systemPrompt = ($0 == Self.defaultPrompt || $0.isEmpty) ? nil : $0
            }
        )
    }

    var body: some View {
        Form {
            Section {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "lock.shield")
                        .foregroundColor(.secondary)
                        .font(.system(size: 14))
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Where your transcripts go")
                            .font(.system(size: 12, weight: .semibold))
                        Text("Full meeting transcripts are sent to your configured LLM provider (Claude, OpenAI, or Gemini) for summarization. For local-only processing, switch to Ollama in the API / LLM tab.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 2)
            } header: {
                Text("Privacy")
            }

            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Customize the instructions Claude/OpenAI/Gemini/Ollama receives when generating notes. Leave blank to use the built-in default prompt.")
                        .font(.caption).foregroundColor(.secondary)

                    TextEditor(text: promptBinding)
                        .font(.system(size: 12, design: .monospaced))
                        .frame(minHeight: 320)
                        .overlay(
                            RoundedRectangle(cornerRadius: 6)
                                .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                        )

                    HStack {
                        Spacer()
                        Button("Reset to Default") { showingResetAlert = true }
                            .font(.caption)
                            .buttonStyle(.link)
                    }
                }
                .padding(.vertical, 4)
            } header: {
                Text("System Prompt")
            }
        }
        .formStyle(.grouped)
        .alert("Reset to Default Prompt?", isPresented: $showingResetAlert) {
            Button("Reset", role: .destructive) {
                vm.config.systemPrompt = nil  // nil = use Python default
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This will replace your custom prompt with the built-in default.")
        }
    }
}

// MARK: - Advanced Tab

private struct AdvancedTab: View {
    @ObservedObject var vm: SettingsViewModel

    var body: some View {
        Form {
            Section("Polling") {
                Picker("Poll interval", selection: $vm.config.pollIntervalSecs) {
                    Text("2 seconds").tag(2)
                    Text("5 seconds (default)").tag(5)
                    Text("10 seconds").tag(10)
                    Text("15 seconds").tag(15)
                }
                Picker("Idle threshold", selection: $vm.config.idleThresholdSecs) {
                    Text("30 seconds").tag(30)
                    Text("60 seconds").tag(60)
                    Text("90 seconds (default)").tag(90)
                    Text("120 seconds").tag(120)
                    Text("180 seconds").tag(180)
                }
                Text("Notes are generated after the transcript WAL is idle for this long.")
                    .font(.caption).foregroundColor(.secondary)
            }

            Section("WAL Prefixes") {
                TextField("Transcript DB prefix", text: $vm.config.transcriptDbPrefix)
                    .font(.system(.body, design: .monospaced))
                    .help("Folder prefix for the transcript IndexedDB (default: 1CB477F679D6)")
                TextField("Blocks DB prefix", text: $vm.config.blocksDbPrefix)
                    .font(.system(.body, design: .monospaced))
                    .help("Folder prefix for the blocks/title IndexedDB (default: DDEC8414E29A)")
                Text("Update these only if Zoom changes its IndexedDB folder names.")
                    .font(.caption).foregroundColor(.secondary)
            }

            Section("Blocked Meeting IDs") {
                VStack(alignment: .leading, spacing: 6) {
                    TextEditor(text: Binding(
                        get: { vm.config.blockedMeetingIds.joined(separator: "\n") },
                        set: { raw in
                            vm.config.blockedMeetingIds = raw
                                .components(separatedBy: "\n")
                                .map { $0.trimmingCharacters(in: .whitespaces) }
                                .filter { !$0.isEmpty }
                        }
                    ))
                    .font(.system(size: 11, design: .monospaced))
                    .frame(height: 80)
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                    )
                    Text("One meeting ID per line. Copy IDs from the `meeting_id:` field in any note.")
                        .font(.caption).foregroundColor(.secondary)
                }
                .padding(.vertical, 4)
            }

            Section("API Base URLs (optional overrides)") {
                if vm.config.llmProvider == "ollama" {
                    TextField("Ollama base URL", text: $vm.config.ollamaBaseUrl)
                        .help("e.g. http://localhost:11434")
                }
                if vm.config.llmProvider == "openai" {
                    TextField("OpenAI base URL", text: $vm.config.openaiBaseUrl)
                        .help("Override to use an OpenAI-compatible proxy")
                }
                if vm.config.llmProvider == "gemini" {
                    TextField("Gemini base URL", text: $vm.config.geminiBaseUrl)
                        .help("Override to use a proxy or different endpoint")
                }
                Text("Leave blank to use the provider's default endpoint.")
                    .font(.caption).foregroundColor(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Calendar Tab

private struct CalendarTab: View {
    @ObservedObject private var calendarService = CalendarService.shared

    private var statusLabel: String {
        switch calendarService.authorizationStatus {
        case .authorized:      return "Access granted"
        case .denied:          return "Access denied"
        case .restricted:      return "Restricted by system policy"
        case .notDetermined:   return "Not requested yet"
        @unknown default:
            if #available(macOS 14.0, *), calendarService.authorizationStatus == .fullAccess {
                return "Access granted"
            }
            return "Unknown"
        }
    }

    private var statusColor: Color {
        calendarService.isAuthorized ? .green : .orange
    }

    private var buttonLabel: String {
        switch calendarService.authorizationStatus {
        case .denied, .restricted: return "Open System Settings"
        case .notDetermined:       return "Grant Calendar Access"
        default:                   return "Refresh Calendar"
        }
    }

    var body: some View {
        Form {
            Section("Calendar Access") {
                HStack {
                    Image(systemName: calendarService.isAuthorized ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                        .foregroundColor(statusColor)
                    Text(statusLabel)
                    Spacer()
                    Button(buttonLabel) {
                        if calendarService.isAuthorized {
                            calendarService.refresh()
                        } else {
                            calendarService.requestAccess()
                        }
                    }
                }
                Text("Zoom Notes reads Apple Calendar to show upcoming meetings in the menu bar and to name notes correctly when Zoom can't identify the meeting title.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            if calendarService.isAuthorized {
                Section("Upcoming Meetings Preview") {
                    let events = calendarService.upcomingEvents(withinHours: 8)
                    if events.isEmpty {
                        Text("No meetings in the next 8 hours.")
                            .foregroundColor(.secondary)
                    } else {
                        ForEach(events, id: \.title) { event in
                            HStack {
                                Text(event.title)
                                Spacer()
                                Text("\(event.startTimeString) (\(event.timeLabel))")
                                    .foregroundColor(.secondary)
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Frontmatter Properties Section

private struct FrontmatterPropertyRow: Identifiable {
    let id: UUID
    var key: String
    var value: String

    init(key: String = "", value: String = "") {
        self.id = UUID()
        self.key = key
        self.value = value
    }

    init(from dict: [String: String]) {
        self.id = UUID()
        self.key = dict["key"] ?? ""
        self.value = dict["value"] ?? ""
    }

    var asDict: [String: String] { ["key": key, "value": value] }
}

private struct FrontmatterPropertiesSection: View {
    @ObservedObject var vm: SettingsViewModel
    @State private var rows: [FrontmatterPropertyRow] = []
    @State private var showRawYaml = false

    private var extraYaml: Binding<String> {
        Binding(
            get: { vm.config.extraFrontmatterYaml },
            set: { vm.config.extraFrontmatterYaml = $0 }
        )
    }

    private func syncToConfig() {
        vm.config.customFrontmatterProperties = rows.map(\.asDict)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Custom properties added to every note's YAML frontmatter.")
                .font(.caption).foregroundColor(.secondary)

            ForEach($rows) { $row in
                HStack(spacing: 6) {
                    TextField("key", text: $row.key)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12))
                        .frame(width: 120)
                        .onChange(of: row.key) { _ in syncToConfig() }

                    Text(":").foregroundColor(.secondary).font(.system(size: 12))

                    TextField("value", text: $row.value)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12))
                        .onChange(of: row.value) { _ in syncToConfig() }

                    Menu {
                        Button("{title}") { row.value += "{title}"; syncToConfig() }
                        Button("{date}")  { row.value += "{date}";  syncToConfig() }
                    } label: {
                        Image(systemName: "chevron.down.circle")
                            .font(.system(size: 12))
                            .foregroundColor(.secondary)
                    }
                    .menuStyle(.borderlessButton)
                    .frame(width: 20)
                    .help("Insert token")

                    Button {
                        rows.removeAll { $0.id == row.id }
                        syncToConfig()
                    } label: {
                        Image(systemName: "minus.circle.fill")
                            .foregroundColor(.red)
                            .font(.system(size: 13))
                    }
                    .buttonStyle(.plain)
                }
            }

            Button {
                rows.append(FrontmatterPropertyRow())
                syncToConfig()
            } label: {
                Label("Add property", systemImage: "plus.circle")
                    .font(.system(size: 12))
            }
            .buttonStyle(.plain)
            .foregroundColor(.accentColor)

            Text("Tokens: {title} = meeting title   {date} = YYYY-MM-DD")
                .font(.system(size: 10)).foregroundColor(.secondary)

            Divider()

            Toggle("Include raw YAML block (advanced)", isOn: $showRawYaml)
                .font(.system(size: 12))

            if showRawYaml {
                TextEditor(text: extraYaml)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(minHeight: 80)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                Text("Appended after the structured properties above. Tokens work here too.")
                    .font(.system(size: 10)).foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 4)
        .onAppear {
            rows = vm.config.customFrontmatterProperties.map { FrontmatterPropertyRow(from: $0) }
            showRawYaml = !vm.config.extraFrontmatterYaml.isEmpty
        }
        .onChange(of: vm.config.customFrontmatterProperties) { newProps in
            let currentDicts = rows.map(\.asDict)
            if currentDicts != newProps {
                rows = newProps.map { FrontmatterPropertyRow(from: $0) }
            }
        }
    }
}

// MARK: - Preview

#Preview {
    SettingsView().environmentObject(AppState())
}

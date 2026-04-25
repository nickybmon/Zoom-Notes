//
//  SettingsView.swift
//  ZoomNotesApp
//
//  Native macOS settings window — sidebar + tabbed content.
//  Mirrors LMA's SettingsView structure, adapted for Zoom Notes config schema.
//

import SwiftUI
import AppKit

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
    NavItem(id: "advanced", label: "Advanced",    icon: "gearshape.2"),
]

// MARK: - Root view

struct SettingsView: View {
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

                if !appState.isEngineRunning {
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
        .task { await vm.loadConfig() }
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
                    Picker("Model", selection: $vm.config.llmModel) {
                        Text("Claude Opus 4.5").tag("claude-opus-4-5")
                        Text("Claude Sonnet 4.6 (recommended)").tag("claude-sonnet-4-6")
                        Text("Claude Haiku 4.5 (fastest)").tag("claude-haiku-4-5")
                    }
                case "openai":
                    Picker("Model", selection: $vm.config.llmModel) {
                        Text("GPT-4o (recommended)").tag("gpt-4o")
                        Text("GPT-4o mini").tag("gpt-4o-mini")
                        Text("o1").tag("o1")
                        Text("o3-mini").tag("o3-mini")
                    }
                case "gemini":
                    Picker("Model", selection: $vm.config.llmModel) {
                        Text("Gemini 2.0 Flash (recommended)").tag("gemini-2.0-flash")
                        Text("Gemini 1.5 Pro").tag("gemini-1.5-pro")
                    }
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
                    Text("Stored securely in macOS Keychain. Leave blank to use ANTHROPIC_API_KEY env var.")
                        .font(.caption).foregroundColor(.secondary)
                case "openai":
                    SecureField("OpenAI API Key", text: $vm.openaiApiKey)
                        .help("Get your key at platform.openai.com")
                    Text("Stored securely in macOS Keychain.")
                        .font(.caption).foregroundColor(.secondary)
                case "gemini":
                    SecureField("Gemini API Key", text: $vm.geminiApiKey)
                        .help("Get your key at aistudio.google.com/apikey")
                    Text("Stored securely in macOS Keychain. Leave blank to use GEMINI_API_KEY env var.")
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
            get: { vm.config.systemPrompt ?? "" },
            set: { vm.config.systemPrompt = $0.isEmpty ? nil : $0 }
        )
    }

    var body: some View {
        Form {
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
            Button("Reset", role: .destructive) { vm.config.systemPrompt = nil }
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
                    Text("15 seconds").tag(15)
                    Text("30 seconds (default)").tag(30)
                    Text("60 seconds").tag(60)
                    Text("90 seconds").tag(90)
                    Text("120 seconds").tag(120)
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

// MARK: - Preview

#Preview {
    SettingsView().environmentObject(AppState())
}

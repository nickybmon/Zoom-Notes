//
//  SettingsViewModel.swift
//  ZoomNotesApp
//
//  Loads and saves ZoomNotesConfig (settings.json + Keychain).
//  Also fetches available Ollama models for the model picker.
//

import Foundation
import Combine

@MainActor
class SettingsViewModel: ObservableObject {
    @Published var config: ZoomNotesConfig = ZoomNotesConfig()
    @Published var isLoading = false
    @Published var isSaving = false
    @Published var error: String?
    @Published var saveSuccess = false

    /// Called after a successful save so the caller can signal the engine.
    var onSave: (() -> Void)?

    // API key fields (loaded from Keychain, written back on save)
    @Published var claudeApiKey: String = ""
    @Published var openaiApiKey: String = ""
    @Published var geminiApiKey: String = ""

    // Available Ollama models
    @Published var ollamaModels: [String] = []
    @Published var ollamaModelsError: String?

    // Connection test
    @Published var testResult: String?
    @Published var isTesting = false

    func loadConfig() async {
        isLoading = true
        error = nil
        let cfg = loadConfig_()
        let claude = getApiKey(provider: "claude")
        let openai = getApiKey(provider: "openai")
        let gemini = getApiKey(provider: "gemini")
        config = cfg
        claudeApiKey = claude
        openaiApiKey = openai
        geminiApiKey = gemini
        isLoading = false

        if cfg.llmProvider == "ollama" {
            await loadOllamaModels()
        }
    }

    func saveConfig() async {
        guard !isSaving else { return }
        isSaving = true
        error = nil
        saveSuccess = false

        do {
            // Write API keys to Keychain
            setApiKey(provider: "claude", value: claudeApiKey)
            setApiKey(provider: "openai", value: openaiApiKey)
            setApiKey(provider: "gemini", value: geminiApiKey)

            try saveConfig_(config)
            saveSuccess = true
            onSave?()
            Task {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { self.saveSuccess = false }
            }
        } catch {
            self.error = "Save failed: \(error.localizedDescription)"
        }
        isSaving = false
    }

    func loadOllamaModels() async {
        ollamaModelsError = nil
        let baseURL = config.ollamaBaseUrl.isEmpty ? "http://localhost:11434" : config.ollamaBaseUrl
        guard let url = URL(string: "\(baseURL)/api/tags") else { return }

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct TagsResponse: Decodable {
                struct ModelEntry: Decodable { let name: String }
                let models: [ModelEntry]
            }
            let decoded = try JSONDecoder().decode(TagsResponse.self, from: data)
            ollamaModels = decoded.models.map(\.name)
            if ollamaModels.isEmpty { ollamaModelsError = "No models installed in Ollama." }
        } catch {
            ollamaModels = []
            ollamaModelsError = "Could not reach Ollama at \(baseURL)"
        }
    }

    func testConnection() async {
        isTesting = true
        testResult = nil
        do {
            let result = try await performConnectionTest()
            testResult = "✓ \(result)"
        } catch {
            testResult = "✗ \(error.localizedDescription)"
        }
        isTesting = false
    }

    private func performConnectionTest() async throws -> String {
        switch config.llmProvider {
        case "claude":
            let key = claudeApiKey.isEmpty ? getApiKey(provider: "claude") : claudeApiKey
            guard !key.isEmpty else { throw NSError(domain: "Settings", code: 0, userInfo: [NSLocalizedDescriptionKey: "No API key set"]) }
            let url = URL(string: "https://api.anthropic.com/v1/models")!
            var req = URLRequest(url: url)
            req.setValue(key, forHTTPHeaderField: "x-api-key")
            req.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
            req.timeoutInterval = 10
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw NSError(domain: "Settings", code: code, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"]) }
            return "Claude API reachable"

        case "openai":
            let key = openaiApiKey.isEmpty ? getApiKey(provider: "openai") : openaiApiKey
            guard !key.isEmpty else { throw NSError(domain: "Settings", code: 0, userInfo: [NSLocalizedDescriptionKey: "No API key set"]) }
            let url = URL(string: "https://api.openai.com/v1/models")!
            var req = URLRequest(url: url)
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            req.timeoutInterval = 10
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw NSError(domain: "Settings", code: code, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"]) }
            return "OpenAI API reachable"

        case "gemini":
            let key = geminiApiKey.isEmpty ? getApiKey(provider: "gemini") : geminiApiKey
            guard !key.isEmpty else { throw NSError(domain: "Settings", code: 0, userInfo: [NSLocalizedDescriptionKey: "No API key set"]) }
            let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models?key=\(key)")!
            var req = URLRequest(url: url)
            req.timeoutInterval = 10
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw NSError(domain: "Settings", code: code, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"]) }
            return "Gemini API reachable"

        case "ollama":
            let baseURL = config.ollamaBaseUrl.isEmpty ? "http://localhost:11434" : config.ollamaBaseUrl
            let url = URL(string: "\(baseURL)/api/tags")!
            var req = URLRequest(url: url)
            req.timeoutInterval = 5
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw NSError(domain: "Settings", code: code, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"]) }
            return "Ollama reachable at \(baseURL)"

        default:
            throw NSError(domain: "Settings", code: 0, userInfo: [NSLocalizedDescriptionKey: "Unknown provider"])
        }
    }
}

// Avoid name collision with the free functions in ZoomNotesConfig.swift
private func loadConfig_() -> ZoomNotesConfig { loadConfig() }
private func saveConfig_(_ cfg: ZoomNotesConfig) throws { try saveConfig(cfg) }

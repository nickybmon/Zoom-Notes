//
//  ZoomNotesConfig.swift
//  ZoomNotesApp
//
//  Swift mirror of zoom_config.py's ZoomNotesConfig dataclass.
//  Reads/writes ~/Library/Application Support/zoom-notes/settings.json
//  and macOS Keychain (service "zoom-notes-assistant") directly.
//

import Foundation
import Security

// MARK: - Config struct

struct ZoomNotesConfig: Codable {
    var llmProvider: String = "claude"
    var llmModel: String = "claude-sonnet-4-6"

    var notesDir: String = "\(NSHomeDirectory())/Desktop/Meeting Notes/Notes"
    var transcriptsDir: String = "\(NSHomeDirectory())/Desktop/Meeting Notes/Transcripts"

    var subfolderPattern: String = "day"
    var filenamePattern: String = "{title}"
    var transcriptFilenamePattern: String = "{title} \u{2014} transcript"

    var systemPrompt: String? = nil

    var customFrontmatterProperties: [[String: String]] = []
    var extraFrontmatterYaml: String = ""

    var pollIntervalSecs: Int = 5
    var idleThresholdSecs: Int = 90

    var transcriptDbPrefix: String = "1CB477F679D6"
    var blocksDbPrefix: String = "DDEC8414E29A"

    var ollamaBaseUrl: String = "http://localhost:11434"
    var openaiBaseUrl: String = "https://api.openai.com/v1/chat/completions"
    var geminiBaseUrl: String = "https://generativelanguage.googleapis.com/v1beta/models"

    enum CodingKeys: String, CodingKey {
        case llmProvider = "llm_provider"
        case llmModel = "llm_model"
        case notesDir = "notes_dir"
        case transcriptsDir = "transcripts_dir"
        case subfolderPattern = "subfolder_pattern"
        case filenamePattern = "filename_pattern"
        case transcriptFilenamePattern = "transcript_filename_pattern"
        case systemPrompt = "system_prompt"
        case customFrontmatterProperties = "custom_frontmatter_properties"
        case extraFrontmatterYaml = "extra_frontmatter_yaml"
        case pollIntervalSecs = "poll_interval_secs"
        case idleThresholdSecs = "idle_threshold_secs"
        case transcriptDbPrefix = "transcript_db_prefix"
        case blocksDbPrefix = "blocks_db_prefix"
        case ollamaBaseUrl = "ollama_base_url"
        case openaiBaseUrl = "openai_base_url"
        case geminiBaseUrl = "gemini_base_url"
    }
}

// MARK: - File paths

private let configDir: URL = {
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    return appSupport.appendingPathComponent("zoom-notes")
}()

private let configFile: URL = configDir.appendingPathComponent("settings.json")

// MARK: - Load / Save

func loadConfig() -> ZoomNotesConfig {
    guard FileManager.default.fileExists(atPath: configFile.path),
          let data = try? Data(contentsOf: configFile) else {
        return ZoomNotesConfig()
    }
    let decoder = JSONDecoder()
    return (try? decoder.decode(ZoomNotesConfig.self, from: data)) ?? ZoomNotesConfig()
}

func saveConfig(_ cfg: ZoomNotesConfig) throws {
    try FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    let data = try encoder.encode(cfg)
    // Atomic write via temp file
    let tmp = configFile.appendingPathExtension("tmp")
    try data.write(to: tmp, options: .atomic)
    _ = try FileManager.default.replaceItemAt(configFile, withItemAt: tmp)
}

// MARK: - Keychain

private let keychainService = "zoom-notes-assistant"

func keychainGet(account: String) -> String? {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: keychainService,
        kSecAttrAccount as String: account,
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne
    ]
    var result: AnyObject?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess,
          let data = result as? Data,
          let str = String(data: data, encoding: .utf8),
          !str.isEmpty else { return nil }
    return str
}

func keychainSet(account: String, value: String) -> Bool {
    let data = value.data(using: .utf8)!
    // Try update first
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: keychainService,
        kSecAttrAccount as String: account
    ]
    let update: [String: Any] = [kSecValueData as String: data]
    var status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
    if status == errSecItemNotFound {
        var newItem = query
        newItem[kSecValueData as String] = data
        status = SecItemAdd(newItem as CFDictionary, nil)
    }
    return status == errSecSuccess
}

func keychainDelete(account: String) -> Bool {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: keychainService,
        kSecAttrAccount as String: account
    ]
    let status = SecItemDelete(query as CFDictionary)
    return status == errSecSuccess || status == errSecItemNotFound
}

// MARK: - API key helpers

func apiKeyAccount(for provider: String) -> String? {
    switch provider {
    case "claude":  return "anthropic_api_key"
    case "openai":  return "openai_api_key"
    case "gemini":  return "gemini_api_key"
    default:        return nil
    }
}

func getApiKey(provider: String) -> String {
    guard let account = apiKeyAccount(for: provider) else { return "" }
    return keychainGet(account: account) ?? ""
}

func setApiKey(provider: String, value: String) {
    guard let account = apiKeyAccount(for: provider) else { return }
    if value.isEmpty {
        _ = keychainDelete(account: account)
    } else {
        _ = keychainSet(account: account, value: value)
    }
}

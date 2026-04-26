//
//  OnboardingView.swift
//  ZoomNotesApp
//
//  First-launch welcome window. Shown once, dismissed to Settings or the menu bar.
//

import SwiftUI
import AppKit

struct OnboardingView: View {
    var onOpenSettings: () -> Void
    var onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // ── Header ────────────────────────────────────────────────────────
            VStack(spacing: 16) {
                if let appIcon = NSImage(named: "AppIcon") {
                    Image(nsImage: appIcon)
                        .resizable()
                        .frame(width: 80, height: 80)
                        .cornerRadius(18)
                } else {
                    Image(systemName: "doc.text.fill")
                        .font(.system(size: 56))
                        .foregroundColor(.accentColor)
                }

                Text("Welcome to Zoom Notes")
                    .font(.largeTitle)
                    .fontWeight(.semibold)

                Text("Automatic meeting notes, delivered to your Desktop.")
                    .font(.title3)
                    .foregroundColor(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.top, 48)
            .padding(.horizontal, 40)

            // ── Steps ─────────────────────────────────────────────────────────
            VStack(alignment: .leading, spacing: 20) {
                OnboardingStep(
                    number: "1",
                    title: "Lives in your menu bar",
                    detail: "Zoom Notes runs quietly in the background. Look for the icon in your menu bar — it turns blue when a meeting is in progress.",
                    icon: "menubar.rectangle"
                )
                OnboardingStep(
                    number: "2",
                    title: "Reads your Zoom transcript",
                    detail: "When your meeting ends, it detects the transcript Zoom saves locally on your Mac and sends it to your chosen AI to summarize.",
                    icon: "text.bubble"
                )
                OnboardingStep(
                    number: "3",
                    title: "Saves notes to your Desktop",
                    detail: "Structured meeting notes appear in ~/Desktop/Meeting Notes/ automatically. You can change the location and format in Settings.",
                    icon: "square.and.arrow.down"
                )
            }
            .padding(.top, 32)
            .padding(.horizontal, 48)

            // ── Privacy note ──────────────────────────────────────────────────
            HStack(spacing: 8) {
                Image(systemName: "lock.shield")
                    .foregroundColor(.secondary)
                    .font(.caption)
                Text("Transcripts are sent to your configured AI provider. Use Ollama in Settings for fully local, private processing.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            .padding(.horizontal, 48)
            .padding(.top, 24)

            // ── Buttons ───────────────────────────────────────────────────────
            HStack(spacing: 12) {
                Button("Not Now") {
                    onDismiss()
                }
                .buttonStyle(.plain)
                .foregroundColor(.secondary)

                Spacer()

                Button("Open Settings") {
                    onOpenSettings()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 40)
            .padding(.top, 28)
            .padding(.bottom, 36)
        }
        .frame(width: 520)
        .background(Color(NSColor.windowBackgroundColor))
    }
}

// MARK: - Step row

private struct OnboardingStep: View {
    let number: String
    let title: String
    let detail: String
    let icon: String

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(0.12))
                    .frame(width: 36, height: 36)
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundColor(.accentColor)
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .fontWeight(.medium)
                Text(detail)
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

#!/usr/bin/env swift
// Renders the DMG background HTML to PNG using WebKit offscreen rendering.
import WebKit
import AppKit

let htmlPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let outPath1x = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "background.png"
let outPath2x = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "background@2x.png"

guard !htmlPath.isEmpty else { print("Usage: render-html-background.swift <input.html> <out1x.png> <out2x.png>"); exit(1) }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

class Renderer: NSObject, WKNavigationDelegate {
    let htmlURL: URL
    let out1x: String
    let out2x: String
    var webView: WKWebView!

    init(html: String, out1x: String, out2x: String) {
        self.htmlURL = URL(fileURLWithPath: html)
        self.out1x = out1x
        self.out2x = out2x
        super.init()

        let config = WKWebViewConfiguration()
        // Offscreen — no window needed
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 1320, height: 800), configuration: config)
        webView.navigationDelegate = self
        // Add static + clean classes so toolbar hides and animation freezes
        webView.loadFileURL(htmlURL.appendingPathComponent("").deletingLastPathComponent()
            .appendingPathComponent(htmlURL.lastPathComponent),
            allowingReadAccessTo: htmlURL.deletingLastPathComponent())
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Inject static/clean classes then wait one tick for paint
        webView.evaluateJavaScript("document.body.classList.add('static','clean')") { _, _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.snapshot(scale: 1, path: self.out1x) {
                    self.snapshot(scale: 2, path: self.out2x) {
                        NSApp.terminate(nil)
                    }
                }
            }
        }
    }

    func snapshot(scale: CGFloat, path: String, completion: @escaping () -> Void) {
        let W: CGFloat = 1320, H: CGFloat = 800
        let config = WKSnapshotConfiguration()
        config.rect = CGRect(x: 0, y: 0, width: W, height: H)
        config.snapshotWidth = NSNumber(value: Double(W * scale / NSScreen.main!.backingScaleFactor))

        webView.takeSnapshot(with: config) { image, error in
            if let error = error { print("Snapshot error: \(error)"); completion(); return }
            guard let image = image else { completion(); return }

            // Re-render at exact pixel size
            let target = NSSize(width: W * scale, height: H * scale)
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                pixelsWide: Int(target.width), pixelsHigh: Int(target.height),
                bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                isPlanar: false, colorSpaceName: .deviceRGB,
                bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            image.draw(in: NSRect(origin: .zero, size: target),
                       from: NSRect(origin: .zero, size: image.size),
                       operation: .copy, fraction: 1.0)
            NSGraphicsContext.restoreGraphicsState()

            if let data = rep.representation(using: .png, properties: [:]) {
                try? data.write(to: URL(fileURLWithPath: path))
                print("Written \(Int(target.width))×\(Int(target.height)): \(path)")
            }
            completion()
        }
    }
}

let renderer = Renderer(
    html: htmlPath,
    out1x: outPath1x,
    out2x: outPath2x
)

app.run()

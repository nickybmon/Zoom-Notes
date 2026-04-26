#!/usr/bin/env swift
// Generates a 1320x800 (@2x) DMG background image.
// App icon position: x=360 (1x), Applications position: x=960 (1x) — doubled for @2x.

import AppKit
import CoreGraphics

let width: CGFloat = 660
let height: CGFloat = 400

let outputPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "background.png"

// Create bitmap context
let colorSpace = CGColorSpaceCreateDeviceRGB()
guard let ctx = CGContext(
    data: nil,
    width: Int(width), height: Int(height),
    bitsPerComponent: 8,
    bytesPerRow: 0,
    space: colorSpace,
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else { fatalError("Could not create CGContext") }

NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)

// ── Background gradient ───────────────────────────────────────────────────────
let bgRect = CGRect(x: 0, y: 0, width: width, height: height)
let gradient = NSGradient(
    colors: [
        NSColor(calibratedWhite: 0.13, alpha: 1.0),
        NSColor(calibratedWhite: 0.10, alpha: 1.0),
    ],
    atLocations: [0.0, 1.0],
    colorSpace: .deviceGray
)!
gradient.draw(in: bgRect, angle: 90)

// ── Arrow body ────────────────────────────────────────────────────────────────
// Window is 660x400. App icon at x=180, Applications at x=480.
// Arrow sits between them: x=255 to x=410, vertically centered at y=200.
let arrowColor = NSColor(calibratedWhite: 0.32, alpha: 1.0)
arrowColor.setFill()

// Shaft
let shaftY: CGFloat = 194
let shaft = NSBezierPath(
    roundedRect: CGRect(x: 258, y: shaftY, width: 120, height: 12),
    xRadius: 6, yRadius: 6
)
shaft.fill()

// Arrowhead triangle
let head = NSBezierPath()
head.move(to: CGPoint(x: 368, y: 165))
head.line(to: CGPoint(x: 415, y: 200))
head.line(to: CGPoint(x: 368, y: 235))
head.close()
head.fill()

// ── "Drag to install" label ───────────────────────────────────────────────────
let labelAttrs: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: 11, weight: .regular),
    .foregroundColor: NSColor(calibratedWhite: 0.38, alpha: 1.0),
    .kern: 0.8,
]
let label = NSAttributedString(string: "drag to install", attributes: labelAttrs)
let labelSize = label.size()
// Center under the arrow (midpoint between 258 and 415 = 336)
let labelX = 336 - labelSize.width / 2
label.draw(at: CGPoint(x: labelX, y: 155))

// ── Export ────────────────────────────────────────────────────────────────────
guard let image = ctx.makeImage() else { fatalError("Could not make image") }
let nsImage = NSImage(cgImage: image, size: NSSize(width: width, height: height))
guard let tiffData = nsImage.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiffData),
      let pngData = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("Could not encode PNG")
}
try! pngData.write(to: URL(fileURLWithPath: outputPath))
print("Written: \(outputPath)")
EOF
// Draws VALR for Mac's app icon master, mac/VALRRecorderSync/Resources/AppIcon-1024.png
// (mac/build.sh turns it into AppIcon.icns: the Finder icon, the DMG's volume icon and the
// icon on VALR's notifications).
//
//   swift mac/dmg/make_app_icon.swift
//
// VALR's mark, the Signal V (scripts/make_icons.py holds the geometry; the numbers below
// match it): three pen-like strokes stepping down into a V, ink with a muted-indigo centre
// stroke, on a warm porcelain tile. macOS's icon grid: an 824 pt rounded tile (185 pt
// corners) centred on a 1024 canvas, lit from above (a hairline highlight on the top edge),
// a crisp hairline edge so it holds up on light windows, and the standard soft drop shadow.
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let S: CGFloat = 1024, TILE: CGFloat = 824, R: CGFloat = 185
let tileRect = CGRect(x: (S - TILE) / 2, y: (S - TILE) / 2 - 10, width: TILE, height: TILE)  // top-left origin
let space = CGColorSpace(name: CGColorSpace.sRGB)!
func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: a)
}
func gradient(_ stops: [(UInt32, CGFloat, CGFloat)]) -> CGGradient {
    CGGradient(colorsSpace: space, colors: stops.map { rgb($0.0, $0.1) } as CFArray, locations: stops.map { $0.2 })!
}

/// One stroke of the mark in the 100-unit box (scripts/make_icons.py bar_segments).
func stroke(_ cx: CGFloat, _ y0: CGFloat, _ y1: CGFloat, w: CGFloat = 10) -> CGPath {
    let h = y1 - y0, k = w / 2, t = w * 0.2, e = h * 0.2
    let p = CGMutablePath()
    p.move(to: CGPoint(x: cx - t, y: y0 + 0.8))
    p.addQuadCurve(to: CGPoint(x: cx + t, y: y0 + 0.8), control: CGPoint(x: cx, y: y0 - 0.8))
    p.addCurve(to: CGPoint(x: cx + k, y: y0 + e), control1: CGPoint(x: cx + k * 0.8, y: y0 + e * 0.35), control2: CGPoint(x: cx + k, y: y0 + e * 0.7))
    p.addLine(to: CGPoint(x: cx + k, y: y1 - e))
    p.addCurve(to: CGPoint(x: cx + t, y: y1 - 0.8), control1: CGPoint(x: cx + k, y: y1 - e * 0.7), control2: CGPoint(x: cx + k * 0.8, y: y1 - e * 0.35))
    p.addQuadCurve(to: CGPoint(x: cx - t, y: y1 - 0.8), control: CGPoint(x: cx, y: y1 + 0.8))
    p.addCurve(to: CGPoint(x: cx - k, y: y1 - e), control1: CGPoint(x: cx - k * 0.8, y: y1 - e * 0.35), control2: CGPoint(x: cx - k, y: y1 - e * 0.7))
    p.addLine(to: CGPoint(x: cx - k, y: y0 + e))
    p.addCurve(to: CGPoint(x: cx - t, y: y0 + 0.8), control1: CGPoint(x: cx - k, y: y0 + e * 0.7), control2: CGPoint(x: cx - k * 0.8, y: y0 + e * 0.35))
    p.closeSubpath()
    return p
}

let ctx = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 16, bytesPerRow: 0,
                    space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.translateBy(x: 0, y: S); ctx.scaleBy(x: 1, y: -1)
ctx.setShouldAntialias(true)
let tile = CGPath(roundedRect: tileRect, cornerWidth: R, cornerHeight: R, transform: nil)

// Drop shadow (two layers: a tight contact shadow and a soft ambient one), warm-tinted.
for (blur, dy, a) in [(CGFloat(36), CGFloat(18), CGFloat(0.20)), (6, 4, 0.16)] {
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -dy), blur: blur, color: rgb(0x1E1812, a))
    ctx.addPath(tile); ctx.setFillColor(rgb(0xEEE9DF)); ctx.fillPath()
    ctx.restoreGState()
}

// The tile: porcelain, lighter at the top, a whisper of shade at the bottom.
ctx.saveGState()
ctx.addPath(tile); ctx.clip()
ctx.drawLinearGradient(gradient([(0xF8F5EF, 1, 0), (0xF1ECE3, 1, 0.55), (0xE6DFD1, 1, 1)]),
                       start: CGPoint(x: 0, y: tileRect.minY), end: CGPoint(x: 0, y: tileRect.maxY), options: [])

// The mark: 54 % of the tile, centred.
let scale = TILE * 0.54 / 100
var place = CGAffineTransform(translationX: tileRect.midX - 50 * scale, y: tileRect.midY - 50 * scale).scaledBy(x: scale, y: scale)
let outer = CGMutablePath()
outer.addPath(stroke(30, 9, 61), transform: place)
outer.addPath(stroke(70, 9, 61), transform: place)
let centre = stroke(50, 39, 91).copy(using: &place)!
// A soft contact shadow under the strokes, as if inked onto the glaze.
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -3), blur: 6, color: rgb(0x15172C, 0.18))
ctx.addPath(outer); ctx.setFillColor(rgb(0x15172C)); ctx.fillPath()
ctx.addPath(centre); ctx.setFillColor(rgb(0x4B4C9E)); ctx.fillPath()
ctx.restoreGState()
ctx.restoreGState()

// Edge: a hairline highlight on the top half, a fine warm hairline all round (crisp on light windows).
ctx.saveGState()
ctx.addPath(tile); ctx.clip()
ctx.addPath(tile); ctx.setLineWidth(6); ctx.replacePathWithStrokedPath(); ctx.clip()
ctx.drawLinearGradient(gradient([(0xFFFFFF, 0.90, 0), (0xFFFFFF, 0.30, 0.30), (0xFFFFFF, 0, 0.6)]),
                       start: CGPoint(x: 0, y: tileRect.minY), end: CGPoint(x: 0, y: tileRect.maxY), options: [])
ctx.restoreGState()
ctx.addPath(tile); ctx.setStrokeColor(rgb(0x5A4E3C, 0.22)); ctx.setLineWidth(1.5); ctx.strokePath()

let out = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
    .appendingPathComponent("../VALRRecorderSync/Resources/AppIcon-1024.png").standardized
// 8-bit PNG output.
let img8 = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 8, bytesPerRow: 0, space: space,
                     bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
img8.draw(ctx.makeImage()!, in: CGRect(x: 0, y: 0, width: S, height: S))
let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(dest, img8.makeImage()!, nil)
CGImageDestinationFinalize(dest)
print("wrote \(out.path)")

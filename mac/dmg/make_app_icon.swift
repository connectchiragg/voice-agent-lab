// Draws VALR for Mac's app icon master, mac/VALRRecorderSync/Resources/AppIcon-1024.png
// (mac/build.sh turns it into AppIcon.icns, which is also the DMG's volume icon).
//
//   swift mac/dmg/make_app_icon.swift
//
// VALR's orb (static/icons/valr-mark.svg) on the macOS app-icon grid: an 824 pt rounded
// tile (185 pt corners) centred on a 1024 canvas, lit from above (a hairline highlight on the top edge, a soft
// inner shade at the bottom), a crisp hairline edge so it holds up on light windows, and the
// standard soft drop shadow. Replaces the --mac output of scripts/make_icons.py.
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

let ctx = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 16, bytesPerRow: 0,
                    space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.translateBy(x: 0, y: S); ctx.scaleBy(x: 1, y: -1)
ctx.setShouldAntialias(true)
let tile = CGPath(roundedRect: tileRect, cornerWidth: R, cornerHeight: R, transform: nil)

// Drop shadow (two layers: a tight contact shadow and a soft ambient one).
for (blur, dy, a) in [(CGFloat(36), CGFloat(18), CGFloat(0.22)), (6, 4, 0.18)] {
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -dy), blur: blur, color: rgb(0x000000, a))
    ctx.addPath(tile); ctx.setFillColor(rgb(0x0b0e22)); ctx.fillPath()
    ctx.restoreGState()
}

// The tile: night indigo, lighter at the top.
ctx.saveGState()
ctx.addPath(tile); ctx.clip()
ctx.drawLinearGradient(gradient([(0x1b2152, 1, 0), (0x0d1030, 1, 0.5), (0x05060f, 1, 1)]),
                       start: CGPoint(x: 0, y: tileRect.minY), end: CGPoint(x: 0, y: tileRect.maxY), options: [])
let c = CGPoint(x: tileRect.midX, y: tileRect.midY)
// The orb's glow.
ctx.drawRadialGradient(gradient([(0x6366f1, 0.70, 0), (0x6366f1, 0.20, 0.40), (0x6366f1, 0, 1)]),
                       startCenter: c, startRadius: 180, endCenter: c, endRadius: 380, options: [])
// Inner shade along the bottom edge.
ctx.drawLinearGradient(gradient([(0x000000, 0, 0), (0x000000, 0.28, 1)]),
                       start: CGPoint(x: 0, y: tileRect.maxY - 160), end: CGPoint(x: 0, y: tileRect.maxY), options: [])

// The orb: radial body lit from the upper left, a rim of light lower right, a soft specular.
let orbR: CGFloat = 196
ctx.saveGState()
ctx.addEllipse(in: CGRect(x: c.x - orbR, y: c.y - orbR, width: 2 * orbR, height: 2 * orbR)); ctx.clip()
let lit = CGPoint(x: c.x - orbR * 0.32, y: c.y - orbR * 0.38)
ctx.drawRadialGradient(gradient([(0xd9daff, 1, 0), (0x8587f7, 1, 0.32), (0x5153de, 1, 0.68), (0x262886, 1, 1)]),
                       startCenter: lit, startRadius: 0, endCenter: c, endRadius: orbR * 1.05,
                       options: [.drawsAfterEndLocation])
let rim = CGPoint(x: c.x + orbR * 0.55, y: c.y + orbR * 0.62)
ctx.drawRadialGradient(gradient([(0xa5a7ff, 0.45, 0), (0xa5a7ff, 0, 1)]),
                       startCenter: rim, startRadius: 0, endCenter: rim, endRadius: orbR * 0.7, options: [])
let spec = CGPoint(x: c.x - orbR * 0.36, y: c.y - orbR * 0.44)
ctx.drawRadialGradient(gradient([(0xFFFFFF, 0.55, 0), (0xFFFFFF, 0, 1)]),
                       startCenter: spec, startRadius: 0, endCenter: spec, endRadius: orbR * 0.32, options: [])
ctx.restoreGState()
ctx.restoreGState()

// Edge: a hairline highlight on the top half, a dark hairline all round (crisp on light windows).
ctx.saveGState()
ctx.addPath(tile); ctx.clip()
ctx.addPath(tile); ctx.setLineWidth(6); ctx.replacePathWithStrokedPath(); ctx.clip()
ctx.drawLinearGradient(gradient([(0xFFFFFF, 0.45, 0), (0xFFFFFF, 0.10, 0.30), (0xFFFFFF, 0, 0.6)]),
                       start: CGPoint(x: 0, y: tileRect.minY), end: CGPoint(x: 0, y: tileRect.maxY), options: [])
ctx.restoreGState()
ctx.addPath(tile); ctx.setStrokeColor(rgb(0x000000, 0.35)); ctx.setLineWidth(1.5); ctx.strokePath()

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

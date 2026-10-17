// Draws the VALR for Mac DMG window background (660x400 pt, @1x + @2x in one TIFF).
//
//   swift mac/dmg/make_background.swift                 # the shipped design -> mac/dmg/background.tiff
//   swift mac/dmg/make_background.swift b /tmp/b.tiff   # another variant, elsewhere
//
// Variants, both light (VALR's warm porcelain base from static/valr-ds.css), DEFAULT ships:
//   a  a hairline arrow between the icons
//   b  Applications sits in a faint dashed drop zone, a quiet chevron points at it
//
// Only the Command Line Tools are needed (CoreGraphics + CoreText). background.tiff is
// committed, so mac/build.sh never runs this. The geometry must match mac/dmg/style_dmg.sh:
// icons 128 pt centred at APP and APPS, Finder's name label centred on LABEL_Y.
//
// Finder draws the icon names itself, in dark text whenever the window has a background
// picture, so the design stays light and they read cleanly.
//
// Brand: the lockup top left, the Signal V (scripts/make_icons.py geometry) and "valr" in
// Instrument Serif, the wordmark's face; the instruction in Source Sans 3, the site's UI face.
// Both from mac/dmg/fonts (SIL OFL), falling back to New York and SF Pro.
import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

let DEFAULT = "b"
let W: CGFloat = 660, H: CGFloat = 400
let APP = CGPoint(x: 170, y: 190), APPS = CGPoint(x: 490, y: 190)
let ICON: CGFloat = 128
let LABEL_Y: CGFloat = 273

let args = Array(CommandLine.arguments.dropFirst())
let variant = args.first ?? DEFAULT
let here = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
let outURL = args.count > 1 ? URL(fileURLWithPath: args[1]) : here.appendingPathComponent("background.tiff")

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: a)
}
// The site's faces ship in mac/dmg/fonts (SIL OFL); register them for this process only.
for name in ["InstrumentSerif-Regular.ttf", "SourceSans3-Regular.ttf"] {
    CTFontManagerRegisterFontsForURL(here.appendingPathComponent("fonts/\(name)") as CFURL, .process, nil)
}
let space = CGColorSpace(name: CGColorSpace.sRGB)!
func gradient(_ stops: [(UInt32, CGFloat, CGFloat)]) -> CGGradient {
    CGGradient(colorsSpace: space, colors: stops.map { rgb($0.0, $0.1) } as CFArray, locations: stops.map { $0.2 })!
}

func font(_ names: [String], _ size: CGFloat, weight: CGFloat = 0) -> CTFont {
    for n in names {
        let f = CTFontCreateWithName(n as CFString, size, nil)
        if (CTFontCopyPostScriptName(f) as String) == n { return f }
    }
    let attrs: [CFString: Any] = [kCTFontFamilyNameAttribute: ".AppleSystemUIFont",
                                  kCTFontTraitsAttribute: [kCTFontWeightTrait: weight]]
    return CTFontCreateWithFontDescriptor(CTFontDescriptorCreateWithAttributes(attrs as CFDictionary), size, nil)
}
func serifFont(_ size: CGFloat) -> CTFont {
    let f = CTFontCreateWithName("InstrumentSerif-Regular" as CFString, size, nil)
    if (CTFontCopyPostScriptName(f) as String) == "InstrumentSerif-Regular" { return f }
    // New York ships with macOS but isn't addressable by name; load it from its file.
    let url = URL(fileURLWithPath: "/System/Library/Fonts/NewYork.ttf") as CFURL
    if let ds = CTFontManagerCreateFontDescriptorsFromURL(url) as? [CTFontDescriptor], let d = ds.first {
        return CTFontCreateWithFontDescriptor(d, size, nil)
    }
    return CTFontCreateWithName("Georgia" as CFString, size, nil)
}
let serif = serifFont(26)
let sans = font(["SourceSans3-Regular"], 13.5, weight: 0)

func line(_ text: String, _ f: CTFont, _ color: CGColor, kern: CGFloat) -> CTLine {
    CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: [
        NSAttributedString.Key(kCTFontAttributeName as String): f,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
        NSAttributedString.Key(kCTKernAttributeName as String): kern,
    ]))
}
func text(_ ctx: CGContext, _ s: String, _ f: CTFont, _ color: CGColor, x: CGFloat, baseline: CGFloat,
          centred: Bool = true, kern: CGFloat = 0) {
    let l = line(s, f, color, kern: kern)
    let w = CGFloat(CTLineGetTypographicBounds(l, nil, nil, nil))
    ctx.saveGState()
    ctx.textMatrix = CGAffineTransform(scaleX: 1, y: -1)        // the context is flipped
    ctx.textPosition = CGPoint(x: centred ? x - w / 2 : x, y: baseline)
    CTLineDraw(l, ctx)
    ctx.restoreGState()
}

func stroke(_ ctx: CGContext, _ path: CGPath, _ color: CGColor, _ width: CGFloat, dash: [CGFloat] = []) {
    ctx.saveGState()
    ctx.addPath(path); ctx.setStrokeColor(color); ctx.setLineWidth(width)
    ctx.setLineCap(.round); ctx.setLineJoin(.round)
    if !dash.isEmpty { ctx.setLineDash(phase: 0, lengths: dash) }
    ctx.strokePath()
    ctx.restoreGState()
}

/// One stroke of VALR's mark in its 100-unit box (scripts/make_icons.py bar_segments).
func markStroke(_ cx: CGFloat, _ y0: CGFloat, _ y1: CGFloat, w: CGFloat = 10) -> CGPath {
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

func render(scale: CGFloat) -> CGImage {
    let ctx = CGContext(data: nil, width: Int(W * scale), height: Int(H * scale), bitsPerComponent: 16,
                        bytesPerRow: 0, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.translateBy(x: 0, y: H * scale); ctx.scaleBy(x: scale, y: -scale)   // top-left origin, points
    ctx.setShouldAntialias(true); ctx.setAllowsFontSmoothing(true); ctx.interpolationQuality = .high

    // 1. Porcelain (--surface #FBF9F4 to --bg #F4F0E8 to --surface-2 #EEE9DF), and a
    //    barely-there muted-indigo glow (--accent #4B4C9E) behind the app.
    ctx.drawLinearGradient(gradient([(0xFBF9F4, 1, 0), (0xF4F0E8, 1, 0.6), (0xEEE9DF, 1, 1)]),
                           start: .zero, end: CGPoint(x: 0, y: H), options: [])
    ctx.drawRadialGradient(gradient([(0x4B4C9E, 0.08, 0), (0x4B4C9E, 0.025, 0.55), (0x4B4C9E, 0, 1)]),
                           startCenter: APP, startRadius: 0, endCenter: APP, endRadius: 170, options: [])

    let ink: UInt32 = 0x15172C    // --text, Ink

    // 2. The lockup, top left: the mark (strokes 22 pt tall) and "valr" on one baseline.
    let base: CGFloat = 46, k: CGFloat = 22 / 82
    var place = CGAffineTransform(translationX: 30 - 25 * k, y: base - 91 * k).scaledBy(x: k, y: k)
    let outer = CGMutablePath()
    outer.addPath(markStroke(30, 9, 61), transform: place); outer.addPath(markStroke(70, 9, 61), transform: place)
    ctx.addPath(outer); ctx.setFillColor(rgb(ink)); ctx.fillPath()
    ctx.addPath(markStroke(50, 39, 91).copy(using: &place)!); ctx.setFillColor(rgb(0x4B4C9E)); ctx.fillPath()
    text(ctx, "valr", serif, rgb(ink), x: 30 + 50 * k + 7, baseline: base, centred: false, kern: 0.26)

    // 3. The gesture between the icons, in mid-grey (about 40 % black).
    let gapL = APP.x + ICON / 2 + 30, gapR = APPS.x - ICON / 2 - 30, y = APP.y
    switch variant {
    case "b":
        let zone = CGRect(x: APPS.x - 82, y: APPS.y - 82, width: 164, height: 164 + 24)
        let zp = CGPath(roundedRect: zone, cornerWidth: 24, cornerHeight: 24, transform: nil)
        ctx.addPath(zp); ctx.setFillColor(rgb(0x4B4C9E, 0.03)); ctx.fillPath()
        stroke(ctx, zp, rgb(ink, 0.16), 1, dash: [4, 4])
        for (i, a) in [(0, 0.14), (1, 0.26), (2, 0.40)] as [(CGFloat, CGFloat)] {
            let cx = W / 2 - 12 + i * 12
            let p = CGMutablePath()
            p.move(to: CGPoint(x: cx - 3.5, y: y - 7)); p.addLine(to: CGPoint(x: cx + 3.5, y: y)); p.addLine(to: CGPoint(x: cx - 3.5, y: y + 7))
            stroke(ctx, p, rgb(ink, a), 1.25)
        }
    default:
        let shaft = CGMutablePath(); shaft.move(to: CGPoint(x: gapL, y: y)); shaft.addLine(to: CGPoint(x: gapR, y: y))
        ctx.saveGState()
        ctx.addPath(shaft); ctx.setLineWidth(1); ctx.setLineCap(.round); ctx.replacePathWithStrokedPath(); ctx.clip()
        ctx.drawLinearGradient(gradient([(ink, 0, 0), (ink, 0.40, 0.5), (ink, 0.40, 1)]),
                               start: CGPoint(x: gapL, y: 0), end: CGPoint(x: gapR, y: 0), options: [])
        ctx.restoreGState()
        let head = CGMutablePath()
        head.move(to: CGPoint(x: gapR - 6, y: y - 6)); head.addLine(to: CGPoint(x: gapR, y: y)); head.addLine(to: CGPoint(x: gapR - 6, y: y + 6))
        stroke(ctx, head, rgb(ink, 0.40), 1)
    }

    // 4. The one line of instruction, centred below the icons, in --text-3.
    text(ctx, "Drag VALR into Applications", sans, rgb(0x5F6072), x: W / 2, baseline: 336, kern: 0.1)

    // Drawn at 16 bits per channel (smooth gradients), stored at 8 (a small TIFF).
    let out = CGContext(data: nil, width: Int(W * scale), height: Int(H * scale), bitsPerComponent: 8, bytesPerRow: 0,
                        space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    out.draw(ctx.makeImage()!, in: CGRect(x: 0, y: 0, width: W * scale, height: H * scale))
    return out.makeImage()!
}

let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("valr-dmg-bg-\(getpid())")
try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
func writePNG(_ img: CGImage, _ name: String, dpi: CGFloat) -> String {
    let url = tmp.appendingPathComponent(name)
    let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, img, [kCGImagePropertyDPIWidth: dpi, kCGImagePropertyDPIHeight: dpi] as CFDictionary)
    CGImageDestinationFinalize(dest)
    return url.path
}
let p1 = writePNG(render(scale: 1), "background.png", dpi: 72)
let p2 = writePNG(render(scale: 2), "background@2x.png", dpi: 144)
let tiff = Process()
tiff.executableURL = URL(fileURLWithPath: "/usr/bin/tiffutil")
tiff.arguments = ["-cathidpicheck", p1, p2, "-out", outURL.path]
try tiff.run(); tiff.waitUntilExit()
if ProcessInfo.processInfo.environment["KEEP_PNG"] != nil { print("PNGs in \(tmp.path)") } else { try? FileManager.default.removeItem(at: tmp) }
print("variant \(variant) -> \(outURL.path)")

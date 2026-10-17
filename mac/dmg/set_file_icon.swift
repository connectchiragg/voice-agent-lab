// Gives a file (the released .dmg) VALR's app icon in Finder:
//
//   swift mac/dmg/set_file_icon.swift ICON.icns FILE
//
// Finder keeps a custom file icon in the file's resource fork and FinderInfo, not in its
// data, so the SHA-256 in downloads/mac.json is unchanged. Browsers drop both on download:
// a DMG fetched over HTTP shows the generic disk-image icon until it's opened; the mounted
// volume and the app inside always show VALR's icon (style_dmg.sh's .VolumeIcon.icns).
import AppKit

let args = CommandLine.arguments
guard args.count == 3, let icon = NSImage(contentsOfFile: args[1]) else {
    FileHandle.standardError.write("usage: set_file_icon.swift ICON.icns FILE\n".data(using: .utf8)!); exit(2)
}
exit(NSWorkspace.shared.setIcon(icon, forFile: args[2], options: []) ? 0 : 1)

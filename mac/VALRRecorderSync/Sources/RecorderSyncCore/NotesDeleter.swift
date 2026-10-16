import Foundation

/// Moves a note to Notes' "Recently Deleted" (kept there 30 days) with AppleScript.
/// Needs the Automation permission for Notes (macOS asks the first time).
public struct AppleScriptNoteDeleter: NoteDeleting {
    public init() {}

    /// The script refuses to delete when the note's name is not the expected title.
    public static func script(noteID: String, expectedTitle: String) -> String {
        func quote(_ s: String) -> String {
            "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
        }
        return """
        tell application "Notes"
            set theNote to note id \(quote(noteID))
            if (name of theNote) is not \(quote(expectedTitle)) then error "The note title changed; not deleted." number 1001
            delete theNote
        end tell
        """
    }

    public struct ScriptError: Error, CustomStringConvertible {
        public var number: Int
        public var description: String { "AppleScript error \(number)" }
    }

    public func moveToRecentlyDeleted(noteID: String, expectedTitle: String) async throws {
        let source = Self.script(noteID: noteID, expectedTitle: expectedTitle)
        try await MainActor.run {
            var error: NSDictionary?
            guard let script = NSAppleScript(source: source) else { throw ScriptError(number: -1) }
            script.executeAndReturnError(&error)
            if let error { throw ScriptError(number: (error[NSAppleScript.errorNumber] as? Int) ?? -1) }
        }
    }
}

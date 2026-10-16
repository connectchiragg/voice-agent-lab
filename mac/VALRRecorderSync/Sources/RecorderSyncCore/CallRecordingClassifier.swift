import Foundation

/// Decides whether a Notes audio attachment is an iPhone call recording.
///
/// Ground truth observed on macOS 26.6 with a real synced call: the note is titled
/// "Call with <Contact>", sits in the account's ordinary "Notes" folder (no "Call Recordings"
/// folder on the Mac), and has one m4a attachment named "Call with <Contact>.m4a".
/// So the title / attachment name is the main signal; a "Call Recordings" folder (as on the
/// iPhone, also localised) is a second signal. The localised title prefixes are best effort.
public enum CallRecordingClassifier {
    public enum Kind: Equatable {
        /// A call recording. `contact` is the other party from the title, nil when the title
        /// holds a phone number or nothing usable.
        case callRecording(contact: String?)
        case otherAudio
        /// Not something VALR accepts (wrong format or file not on disk yet).
        case unsupported(String)
    }

    /// File types the VALR upload endpoint accepts.
    public static let acceptedExtensions: Set<String> = ["m4a", "mp3", "wav", "aac"]

    public static let titlePrefixes: [String] = [
        "Call with ",       // English (observed)
        "Anruf mit ",       // German
        "Appel avec ",      // French
        "Llamada con ",     // Spanish
        "Chiamata con ",    // Italian
        "Chamada com ",     // Portuguese
        "Gesprek met ",     // Dutch
        "Samtal med ",      // Swedish
    ]

    public static let folderNames: Set<String> = [
        "call recordings", "anrufaufnahmen", "enregistrements d’appels", "enregistrements d'appels",
        "grabaciones de llamadas", "registrazioni delle chiamate", "registrazioni chiamate",
        "gravações de chamadas", "gespreksopnamen", "samtalsinspelningar", "通話録音", "通话录音", "通話錄音",
    ]

    public static func classify(_ a: AudioAttachment) -> Kind {
        let ext = (a.fileURL?.pathExtension ?? (a.mediaFilename as NSString?)?.pathExtension ?? "").lowercased()
        guard acceptedExtensions.contains(ext) else { return .unsupported("format \(ext.isEmpty ? "unknown" : ext)") }
        guard a.fileURL != nil else { return .unsupported("audio not downloaded yet") }
        if let contact = contactFromTitle(a.noteTitle) { return .callRecording(contact: contact.isEmpty ? nil : contact) }
        if let name = a.mediaFilename, let contact = contactFromTitle((name as NSString).deletingPathExtension) {
            return .callRecording(contact: contact.isEmpty ? nil : contact)
        }
        if let folder = a.folderTitle?.trimmingCharacters(in: .whitespaces).lowercased(), folderNames.contains(folder) {
            return .callRecording(contact: nil)
        }
        return .otherAudio
    }

    /// "Call with Jane Doe" -> "Jane Doe"; "Call with +1 555…" -> "" (digits are never sent);
    /// not a call title -> nil.
    public static func contactFromTitle(_ title: String) -> String? {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        for prefix in titlePrefixes where trimmed.lowercased().hasPrefix(prefix.lowercased()) {
            let rest = String(trimmed.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            if rest.isEmpty || rest.rangeOfCharacter(from: .decimalDigits) != nil { return "" }
            return String(rest.prefix(80))
        }
        return nil
    }
}

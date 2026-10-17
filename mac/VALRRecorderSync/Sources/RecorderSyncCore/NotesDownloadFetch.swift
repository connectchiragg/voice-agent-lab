import Foundation

/// When a call-recording note has synced to the Mac but its audio has not, Notes only downloads the
/// audio when something asks for it (on this Mac: when the note is opened). These types decide when to
/// ask and how; the app does the asking with AppleScript (see the app's NotesDownloadFetcher).
///
/// Two ways to ask, least intrusive first:
/// - `.silent`: `save attachment … in file <temp>`. No window, no focus change (checked on macOS 26).
///   The temporary copy is deleted straight away; the upload always reads Notes' own file.
/// - `.show`: `show attachment …`, which opens the note in Notes (the step that reliably makes Notes
///   download it), then the previously active app is brought back and Notes is hidden again if it was.
///   Notes is in front for about a second.

/// One attachment to fetch. `scriptID` is the AppleScript id, "x-coredata://<store>/ICAttachment/p<Z_PK>".
public struct DownloadRequest: Equatable, Sendable {
    public var attachmentID: String
    public var scriptID: String

    public init(attachmentID: String, scriptID: String) {
        self.attachmentID = attachmentID
        self.scriptID = scriptID
    }

    /// nil when the store id or the attachment's Z_PK is unknown.
    public static func make(for a: AudioAttachment, storeUUID: String?) -> DownloadRequest? {
        guard let storeUUID, !storeUUID.isEmpty, let pk = a.attachmentPK else { return nil }
        return DownloadRequest(attachmentID: a.attachmentID, scriptID: "x-coredata://\(storeUUID)/ICAttachment/p\(pk)")
    }
}

public enum DownloadFetchMethod: Equatable, Sendable { case silent, show }

public enum DownloadFetchError: Error, Equatable, CustomStringConvertible {
    /// macOS refused the Apple Event: the Automation permission for Notes is off (or was declined).
    case notAllowed
    case failed(Int)

    public var description: String {
        switch self {
        case .notAllowed: return "VALR is not allowed to control Notes"
        case .failed(let n): return "AppleScript error \(n)"
        }
    }

    /// errAEEventNotPermitted (-1743) and errAEEventWouldRequireUserConsent (-1744).
    public static func from(appleScriptError number: Int) -> DownloadFetchError {
        number == -1743 || number == -1744 ? .notAllowed : .failed(number)
    }
}

/// Asks Notes to download attachments. The app uses AppleScript; tests use a fake.
public protocol DownloadFetching: Sendable {
    func fetch(_ requests: [DownloadRequest], method: DownloadFetchMethod) async throws
}

public struct DownloadFetchPolicy: Equatable, Sendable {
    /// At most one attempt per attachment in this many seconds.
    public var minInterval: TimeInterval
    /// Attempts per attachment before giving up (until "Fetch now").
    public var maxAttempts: Int
    /// The first attempts use the silent method; later ones open the note.
    public var silentAttempts: Int

    public init(minInterval: TimeInterval = 30, maxAttempts: Int = 5, silentAttempts: Int = 1) {
        self.minInterval = minInterval
        self.maxAttempts = maxAttempts
        self.silentAttempts = silentAttempts
    }
}

/// Per-attachment attempt bookkeeping. In memory only: after a restart the app simply tries again.
public struct DownloadFetchTracker: Equatable, Sendable {
    public struct Entry: Equatable, Sendable {
        public var attempts: Int
        public var last: Date
    }

    public var policy: DownloadFetchPolicy
    public private(set) var entries: [String: Entry] = [:]

    public init(policy: DownloadFetchPolicy = DownloadFetchPolicy()) {
        self.policy = policy
    }

    /// Forgets attachments that are no longer waiting (downloaded, deleted, or sent).
    public mutating func keepOnly(_ waiting: Set<String>) {
        entries = entries.filter { waiting.contains($0.key) }
    }

    public mutating func reset() { entries.removeAll() }

    public func hasGivenUp(_ id: String) -> Bool { (entries[id]?.attempts ?? 0) >= policy.maxAttempts }

    /// What to try now for each waiting attachment: `.silent` first, then `.show`; nothing when it was
    /// tried less than `minInterval` ago or has used up its attempts. `now: true` ("Fetch now") skips
    /// the wait and the limit and goes straight to `.show`.
    public func plan(_ ids: [String], at date: Date, now: Bool = false) -> [String: DownloadFetchMethod] {
        var out: [String: DownloadFetchMethod] = [:]
        for id in ids {
            if now { out[id] = .show; continue }
            let e = entries[id]
            let attempts = e?.attempts ?? 0
            if attempts >= policy.maxAttempts { continue }
            if let last = e?.last, date.timeIntervalSince(last) < policy.minInterval { continue }
            out[id] = attempts < policy.silentAttempts ? .silent : .show
        }
        return out
    }

    public mutating func recordAttempt(_ ids: [String], at date: Date) {
        for id in ids {
            entries[id] = Entry(attempts: (entries[id]?.attempts ?? 0) + 1, last: date)
        }
    }

    /// The earliest time another attempt is due, for scheduling the next pass.
    public func nextDue(for ids: [String]) -> Date? {
        ids.compactMap { id -> Date? in
            guard let e = entries[id] else { return nil }
            return e.attempts >= policy.maxAttempts ? nil : e.last.addingTimeInterval(policy.minInterval)
        }.min()
    }
}

/// The AppleScript sources. Ids and paths are quoted; nothing else is interpolated.
public enum NotesFetchScripts {
    static func quote(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    /// Asks Notes for the attachment's data by saving a copy to `path` (a private temp file).
    public static func save(scriptID: String, to path: String) -> String {
        """
        with timeout of 20 seconds
            tell application "Notes"
                save attachment id \(quote(scriptID)) in (POSIX file \(quote(path)))
            end tell
        end timeout
        """
    }

    /// Opens the note that holds the attachment in Notes' window.
    public static func show(scriptID: String) -> String {
        """
        with timeout of 20 seconds
            tell application "Notes"
                show attachment id \(quote(scriptID))
            end tell
        end timeout
        """
    }
}

import Foundation

/// What has been sent, kept in ~/Library/Application Support/VALR/state.json.
/// Holds attachment ids, SHA-256 digests and VALR ids only: no titles, names or audio.
public struct SyncState: Codable, Equatable, Sendable {
    public struct Done: Codable, Equatable, Sendable {
        public var sha256: String
        public var valrID: String?
        public var duplicate: Bool
        public var at: Date
        public var notePK: Int64?
        public var noteDeleted: Bool?
    }
    public struct Rejected: Codable, Equatable, Sendable {
        public var reason: String
        public var at: Date
    }

    public var version = 1
    public var done: [String: Done] = [:]
    public var rejected: [String: Rejected] = [:]
    /// "yyyy-MM-dd" (local) -> recordings sent that day.
    public var sentByDay: [String: Int] = [:]
    public var lastSync: Date?

    public init() {}

    public func isDone(sha: String) -> Bool { done.values.contains { $0.sha256 == sha } }

    public static func dayKey(_ date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    public func sentToday(now: Date = Date()) -> Int { sentByDay[Self.dayKey(now)] ?? 0 }
}

public final class StateStore: @unchecked Sendable {
    public let url: URL
    private let lock = NSLock()
    private var cached: SyncState

    public init(directory: URL = Paths.supportDirectory) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        url = directory.appendingPathComponent("state.json")
        cached = (try? Data(contentsOf: url)).flatMap { try? Self.decoder.decode(SyncState.self, from: $0) } ?? SyncState()
    }

    static let decoder: JSONDecoder = { let d = JSONDecoder(); d.dateDecodingStrategy = .iso8601; return d }()
    static let encoder: JSONEncoder = {
        let e = JSONEncoder(); e.dateEncodingStrategy = .iso8601; e.outputFormatting = [.prettyPrinted, .sortedKeys]; return e
    }()

    public var state: SyncState { lock.lock(); defer { lock.unlock() }; return cached }

    /// Changes the state and writes it atomically.
    public func update(_ change: (inout SyncState) -> Void) {
        lock.lock()
        change(&cached)
        let snapshot = cached
        lock.unlock()
        if let data = try? Self.encoder.encode(snapshot) {
            try? data.write(to: url, options: [.atomic])
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        }
    }
}

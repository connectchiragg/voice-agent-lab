import Foundation

/// Where an older VALR kept the token (the login Keychain). Read at most once, then deleted.
public protocol LegacyTokenSource: Sendable {
    func load(for server: URL) -> String?
    func delete(for server: URL)
}

extension TokenKeychain: LegacyTokenSource {}

/// The upload token, kept in ~/Library/Application Support/VALR/upload-token: folder 0700, file 0600,
/// written atomically, readable only by this user account. Not the Keychain: VALR for Mac is ad-hoc
/// signed, so every new build is a "different app" to the Keychain and macOS asked again and again.
///
/// The file is read once per launch and cached; only saving or forgetting a token touches it again.
/// One token per VALR address (keyed by host, as the Keychain item was).
public final class TokenStore: @unchecked Sendable {
    public let file: URL
    private let legacy: LegacyTokenSource?
    private let lock = NSLock()
    private var cache: Contents?

    struct Contents: Codable, Equatable {
        var tokens: [String: String] = [:]
        /// Hosts whose Keychain item was already looked for: the Keychain is asked once, ever.
        var keychainChecked: [String] = []
    }

    public init(directory: URL = Paths.supportDirectory, legacy: LegacyTokenSource? = TokenKeychain()) {
        file = directory.appendingPathComponent("upload-token")
        self.legacy = legacy
    }

    static func key(_ server: URL) -> String { server.host ?? server.absoluteString }

    /// The token for this VALR address, or nil. The first call for an address that has no token in
    /// the file asks the old Keychain item once, moves it into the file and deletes the Keychain item.
    public func token(for server: URL) -> String? {
        lock.lock(); defer { lock.unlock() }
        var c = loaded()
        let key = Self.key(server)
        if let t = c.tokens[key] { return t }
        guard let legacy, !c.keychainChecked.contains(key) else { return nil }
        c.keychainChecked.append(key)
        let found = legacy.load(for: server).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.flatMap { $0.isEmpty ? nil : $0 }
        if let found { c.tokens[key] = found }
        if (try? write(c)) != nil {
            cache = c
            if found != nil { legacy.delete(for: server) } // never prompts again
        } else {
            cache = c // still answer from memory this launch
        }
        return found
    }

    public func save(_ token: String, for server: URL) throws {
        lock.lock(); defer { lock.unlock() }
        var c = loaded()
        c.tokens[Self.key(server)] = token
        try write(c)
        cache = c
    }

    public func delete(for server: URL) {
        lock.lock(); defer { lock.unlock() }
        var c = loaded()
        c.tokens[Self.key(server)] = nil
        try? write(c)
        cache = c
    }

    private func loaded() -> Contents {
        if let cache { return cache }
        let c = (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(Contents.self, from: $0) } ?? Contents()
        cache = c
        return c
    }

    private func write(_ c: Contents) throws {
        let dir = file.deletingLastPathComponent()
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
        let data = try JSONEncoder().encode(c)
        // Atomic and never readable by others: a 0600 temp file in the same folder, then rename.
        let tmp = dir.appendingPathComponent(".upload-token.\(UUID().uuidString)")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        guard fd >= 0 else { throw StoreError(errno: errno) }
        let ok = data.withUnsafeBytes { buf in write_all(fd, buf) } && fsync(fd) == 0
        close(fd)
        guard ok, rename(tmp.path, file.path) == 0 else {
            let e = errno
            unlink(tmp.path)
            throw StoreError(errno: e)
        }
    }

    private func write_all(_ fd: Int32, _ buf: UnsafeRawBufferPointer) -> Bool {
        var off = 0
        while off < buf.count {
            let n = Foundation.write(fd, buf.baseAddress! + off, buf.count - off)
            if n <= 0 { return false }
            off += n
        }
        return true
    }

    public struct StoreError: Error, CustomStringConvertible {
        public var errno: Int32
        public var description: String { "Could not save the token (\(String(cString: strerror(errno))))" }
    }
}

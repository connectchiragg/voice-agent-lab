import Foundation
import Testing
@testable import RecorderSyncCore

final class FakeKeychain: LegacyTokenSource, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String: String]
    private(set) var loads = 0
    private(set) var deletes = 0
    init(_ items: [String: String] = [:]) { self.items = items }
    func load(for server: URL) -> String? {
        lock.lock(); defer { lock.unlock() }
        loads += 1
        return items[server.host ?? ""]
    }
    func delete(for server: URL) {
        lock.lock(); defer { lock.unlock() }
        deletes += 1
        items[server.host ?? ""] = nil
    }
}

@Suite("Upload token file (replaces the Keychain)")
struct TokenStoreTests {
    let server = URL(string: "https://valr.example.test")!

    private func mode(_ url: URL) throws -> Int {
        (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as! NSNumber).intValue
    }

    @Test func roundTripWithOwnerOnlyPermissions() throws {
        let dir = tempDir().appendingPathComponent("VALR")
        let store = TokenStore(directory: dir, legacy: nil)
        #expect(store.token(for: server) == nil)
        try store.save(testToken, for: server)
        #expect(store.token(for: server) == testToken)
        #expect(try mode(store.file) == 0o600)
        #expect(try mode(dir) == 0o700)
        // A new launch reads it back from disk.
        #expect(TokenStore(directory: dir, legacy: nil).token(for: server) == testToken)
        // No temp files left behind.
        #expect(try FileManager.default.contentsOfDirectory(atPath: dir.path) == ["upload-token"])
        store.delete(for: server)
        #expect(TokenStore(directory: dir, legacy: nil).token(for: server) == nil)
    }

    @Test func tightensAnExistingFolder() throws {
        let dir = tempDir().appendingPathComponent("VALR")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        try TokenStore(directory: dir, legacy: nil).save(testToken, for: server)
        #expect(try mode(dir) == 0o700)
    }

    @Test func readsTheFileOncePerLaunch() throws {
        let dir = tempDir()
        let store = TokenStore(directory: dir, legacy: nil)
        try store.save(testToken, for: server)
        try FileManager.default.removeItem(at: store.file) // later reads come from memory
        #expect(store.token(for: server) == testToken)
    }

    @Test func movesTheKeychainTokenOnceAndDeletesIt() throws {
        let dir = tempDir()
        let keychain = FakeKeychain(["valr.example.test": testToken])
        let store = TokenStore(directory: dir, legacy: keychain)
        #expect(store.token(for: server) == testToken)
        #expect(store.token(for: server) == testToken)
        #expect(keychain.loads == 1 && keychain.deletes == 1)
        #expect(try mode(store.file) == 0o600)
        // Next launch: the file answers, the Keychain is never asked again.
        let next = TokenStore(directory: dir, legacy: keychain)
        #expect(next.token(for: server) == testToken && keychain.loads == 1)
    }

    @Test func asksTheKeychainOnlyOnceEvenWhenItHasNothingOrWasDenied() throws {
        let dir = tempDir()
        let keychain = FakeKeychain()
        #expect(TokenStore(directory: dir, legacy: keychain).token(for: server) == nil)
        #expect(TokenStore(directory: dir, legacy: keychain).token(for: server) == nil)
        #expect(keychain.loads == 1 && keychain.deletes == 0)
        // The user pastes a token in onboarding: it is saved in the file.
        let store = TokenStore(directory: dir, legacy: keychain)
        try store.save(testToken, for: server)
        #expect(TokenStore(directory: dir, legacy: keychain).token(for: server) == testToken && keychain.loads == 1)
    }

    @Test func keepsOneTokenPerAddress() throws {
        let store = TokenStore(directory: tempDir(), legacy: nil)
        let other = URL(string: "https://other.example.test")!
        try store.save("a", for: server)
        try store.save("b", for: other)
        #expect(store.token(for: server) == "a" && store.token(for: other) == "b")
    }
}

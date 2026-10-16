import Foundation
import Security

/// The upload token lives only in the login Keychain (generic password).
public struct TokenKeychain: Sendable {
    public var service: String

    /// The service name predates the rename to "VALR" and stays the same on purpose: an existing
    /// install keeps its token (macOS may ask once to let the renamed app read it: Always Allow).
    public init(service: String = "com.haciensus.valr.recordersync.upload-token") {
        self.service = service
    }

    /// One token per VALR address (the account is the host).
    private func query(for server: URL) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: server.host ?? server.absoluteString]
    }

    public func save(_ token: String, for server: URL) throws {
        let data = Data(token.utf8)
        let base = query(for: server)
        let status = SecItemUpdate(base as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var add = base
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            add[kSecAttrLabel as String] = "VALR upload token"
            let rc = SecItemAdd(add as CFDictionary, nil)
            guard rc == errSecSuccess else { throw KeychainError(status: rc) }
        } else if status != errSecSuccess {
            throw KeychainError(status: status)
        }
    }

    public func load(for server: URL) -> String? {
        var q = query(for: server)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func delete(for server: URL) {
        SecItemDelete(query(for: server) as CFDictionary)
    }

    public struct KeychainError: Error, CustomStringConvertible {
        public var status: OSStatus
        public var description: String { "Keychain error \(status)" }
    }
}

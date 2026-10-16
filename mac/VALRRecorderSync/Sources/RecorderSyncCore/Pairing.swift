import Foundation

/// App side of a future "Connect a Mac" flow (see README, "Multi-user design note").
///
/// Proposed VALR endpoint (not built yet): POST <server>/hooks/device-pair
///   JSON {"code": "ABCD-1234", "device": "VALR Recorder Sync on <Mac model>"}
///   200 {"ok": true, "token": "<per-device upload token>", "account": "<display name>"}
///   400/404 for a wrong or expired code. The code is short-lived (10 min) and single-use,
///   created by the signed-in user in VALR Settings.
/// Until the server has it, the app answers "not supported" and the user pastes the token.
public struct PairingClient: Sendable {
    public var server: URL
    public var session: URLSession

    public init(server: URL, session: URLSession = .shared) {
        self.server = server
        self.session = session
    }

    public enum PairingError: Error, Equatable, CustomStringConvertible {
        case notSupported, badCode, network
        public var description: String {
            switch self {
            case .notSupported: return "This VALR does not offer pairing codes yet. Paste the upload token instead."
            case .badCode: return "That code is wrong or has expired. Make a new one in VALR Settings."
            case .network: return "Could not reach VALR."
            }
        }
    }

    public struct Paired: Decodable, Equatable, Sendable {
        public var token: String
        public var account: String?
    }

    public static func normalise(_ code: String) -> String {
        code.uppercased().filter { $0.isLetter || $0.isNumber }
    }

    public func exchange(code: String, deviceName: String) async throws -> Paired {
        var request = URLRequest(url: server.appendingPathComponent("hooks/device-pair"), timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["code": Self.normalise(code), "device": deviceName])
        let (data, response): (Data, URLResponse)
        do { (data, response) = try await session.data(for: request) } catch { throw PairingError.network }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        switch status {
        case 200:
            guard let paired = try? JSONDecoder().decode(Paired.self, from: data), !paired.token.isEmpty else {
                throw PairingError.notSupported
            }
            return paired
        case 400, 410, 422: throw PairingError.badCode
        // 404/405, or the sign-in page Cloudflare Access shows for an unknown path.
        default: throw PairingError.notSupported
        }
    }
}

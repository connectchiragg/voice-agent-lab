import CryptoKit
import Foundation

/// The minimal hints sent with a recording. Never the note text.
public struct UploadHints: Equatable, Sendable {
    public var filename: String
    public var title: String?
    public var name: String?
    public var recordedAt: Date?

    public init(filename: String, title: String?, name: String?, recordedAt: Date?) {
        self.filename = filename
        self.title = title
        self.name = name
        self.recordedAt = recordedAt
    }

    /// Builds hints from an attachment. A title or file name holding digits (a phone number)
    /// is replaced, so no number leaves the Mac.
    public static func from(_ a: AudioAttachment, contact: String?) -> UploadHints {
        let ext = (a.fileURL?.pathExtension).flatMap { $0.isEmpty ? nil : $0.lowercased() } ?? "m4a"
        let hasDigits = { (s: String) in s.rangeOfCharacter(from: .decimalDigits) != nil }
        let rawTitle = a.noteTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = rawTitle.isEmpty || hasDigits(rawTitle) ? (contact.map { "Call with \($0)" } ?? "Phone call") : String(rawTitle.prefix(200))
        var filename = a.mediaFilename ?? "\(title).\(ext)"
        if hasDigits((filename as NSString).deletingPathExtension) { filename = "\(title).\(ext)" }
        return UploadHints(filename: filename, title: title, name: contact, recordedAt: a.noteCreated)
    }
}

public struct UploadResponse: Decodable, Equatable, Sendable {
    public var ok: Bool?
    public var result: String?
    public var duplicate: Bool?
    public var id: String?
    public var isOK: Bool { ok == true || result == "ok" }
}

public enum UploadError: Error, Equatable, CustomStringConvertible {
    /// Worth retrying: network trouble, 5xx, 429.
    case transient(String)
    /// The token is wrong or was rotated (401/403). Stop and ask the user.
    case unauthorized
    /// This file will never be accepted (413, 415, 400 …). Do not retry it.
    case rejected(Int, String)
    case badResponse(String)

    public var description: String {
        switch self {
        case .transient(let m): return "temporary problem: \(m)"
        case .unauthorized: return "the upload token was refused"
        case .rejected(let code, let m): return "VALR refused the file (\(code)): \(m)"
        case .badResponse(let m): return "unexpected answer from VALR: \(m)"
        }
    }

    public var isRetryable: Bool { if case .transient = self { return true }; return false }
}

public enum FileHasher {
    /// SHA-256 of a file, streamed in 1 MB pieces.
    public static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// True when the first bytes look like audio VALR accepts (MP4 "ftyp", ID3/MPEG, RIFF/WAVE, ADTS).
    public static func looksLikeAudio(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        guard let head = try? handle.read(upToCount: 12), head.count >= 4 else { return false }
        let b = [UInt8](head)
        if b.count >= 8, b[4...7] == [0x66, 0x74, 0x79, 0x70] { return true }        // ftyp
        if b[0...2] == [0x49, 0x44, 0x33] { return true }                              // ID3
        if b[0] == 0xFF, (b[1] & 0xE0) == 0xE0 { return true }                         // MPEG / ADTS sync
        if b[0...3] == [0x52, 0x49, 0x46, 0x46] { return true }                        // RIFF
        return false
    }
}

/// Sends one recording to POST <server>/hooks/recording-upload as multipart/form-data.
public struct Uploader: Sendable {
    public var server: URL
    public var token: String
    public var session: URLSession

    public init(server: URL, token: String, session: URLSession = .shared) {
        self.server = server
        self.token = token
        self.session = session
    }

    public var endpoint: URL { server.appendingPathComponent("hooks/recording-upload") }

    static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func mimeType(for ext: String) -> String {
        switch ext.lowercased() {
        case "mp3": return "audio/mpeg"
        case "wav": return "audio/wav"
        case "aac": return "audio/aac"
        default: return "audio/mp4"
        }
    }

    /// Writes the multipart body to a temp file (the audio is streamed, never fully in memory).
    static func writeBody(file: URL, hints: UploadHints, boundary: String, to out: URL) throws {
        FileManager.default.createFile(atPath: out.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let handle = try FileHandle(forWritingTo: out)
        defer { try? handle.close() }
        func field(_ name: String, _ value: String) throws {
            let clean = value.replacingOccurrences(of: "\r", with: " ").replacingOccurrences(of: "\n", with: " ")
            try handle.write(contentsOf: Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(clean)\r\n".utf8))
        }
        try field("filename", hints.filename)
        if let t = hints.title { try field("title", t) }
        if let n = hints.name, !n.isEmpty { try field("name", n) }
        if let d = hints.recordedAt { try field("recorded_at", iso.string(from: d)) }
        let quoted = hints.filename.replacingOccurrences(of: "\"", with: "'")
        let ext = (hints.filename as NSString).pathExtension
        try handle.write(contentsOf: Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(quoted)\"\r\nContent-Type: \(mimeType(for: ext))\r\n\r\n".utf8))
        let input = try FileHandle(forReadingFrom: file)
        defer { try? input.close() }
        while let chunk = try input.read(upToCount: 1 << 20), !chunk.isEmpty { try handle.write(contentsOf: chunk) }
        try handle.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
    }

    public func upload(file: URL, hints: UploadHints) async throws -> UploadResponse {
        let boundary = "valr-\(UUID().uuidString)"
        let body = FileManager.default.temporaryDirectory.appendingPathComponent("valr-upload-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: body) }
        try Self.writeBody(file: file, hints: hints, boundary: boundary, to: body)

        var request = URLRequest(url: endpoint, timeoutInterval: 300)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue("VALRRecorderSync/1.0 (macOS)", forHTTPHeaderField: "User-Agent")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.upload(for: request, fromFile: body)
        } catch let error as URLError {
            throw UploadError.transient("network (\(error.code.rawValue))")
        }
        guard let http = response as? HTTPURLResponse else { throw UploadError.badResponse("not HTTP") }
        let detail = Self.detail(from: data)
        switch http.statusCode {
        case 200..<300:
            guard let decoded = try? JSONDecoder().decode(UploadResponse.self, from: data) else {
                throw UploadError.badResponse("not JSON")
            }
            guard decoded.isOK else { throw UploadError.badResponse("ok was not true") }
            return decoded
        case 401, 403: throw UploadError.unauthorized
        case 408, 429, 500...599: throw UploadError.transient("HTTP \(http.statusCode)")
        default: throw UploadError.rejected(http.statusCode, detail)
        }
    }

    public enum TokenCheck: Equatable, Sendable { case valid, invalid, notConfigured, unreachable(String) }

    /// Checks the token without sending audio: VALR checks the token before it reads the form,
    /// so an empty non-form POST answers 415 for a good token and 401 for a bad one.
    /// Nothing is stored on the server. Counts as one request against the hourly limit.
    public func checkToken() async -> TokenCheck {
        var request = URLRequest(url: endpoint, timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("text/plain", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data()
        guard let (_, response) = try? await session.data(for: request), let http = response as? HTTPURLResponse else {
            return .unreachable("network")
        }
        switch http.statusCode {
        case 400, 415: return .valid
        case 401, 403: return .invalid
        case 503: return .notConfigured
        default: return .unreachable("HTTP \(http.statusCode)")
        }
    }

    /// FastAPI's {"detail": "..."} message, shortened. Never echoes request content.
    static func detail(from data: Data) -> String {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let d = obj["detail"] as? String {
            return String(d.prefix(160))
        }
        return "no detail"
    }
}

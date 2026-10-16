import Foundation
import Network

/// A tiny local HTTP/1.1 server on 127.0.0.1 standing in for VALR. Reads one request per
/// connection (headers + Content-Length body), asks `handler` for the answer, closes.
final class MockServer: @unchecked Sendable {
    struct Request {
        var method: String
        var path: String
        var headers: [String: String]
        var body: Data

        /// Very small multipart reader: field name -> (value data, filename?).
        func multipart() -> [String: (Data, String?)] {
            guard let ct = headers["content-type"], let r = ct.range(of: "boundary=") else { return [:] }
            let boundary = Data(("--" + ct[r.upperBound...]).utf8)
            var out: [String: (Data, String?)] = [:]
            var parts: [Data] = []
            var cursor = body.startIndex
            while let found = body.range(of: boundary, in: cursor..<body.endIndex) {
                if cursor != body.startIndex { parts.append(body[cursor..<found.lowerBound]) }
                cursor = found.upperBound
            }
            for part in parts {
                guard let sep = part.range(of: Data("\r\n\r\n".utf8)) else { continue }
                let head = String(decoding: part[part.startIndex..<sep.lowerBound], as: UTF8.self)
                var value = part[sep.upperBound..<part.endIndex]
                if value.suffix(2) == Data("\r\n".utf8) { value = value.dropLast(2) }
                guard let n = head.range(of: "name=\"") else { continue }
                let name = String(head[n.upperBound...].prefix { $0 != "\"" })
                var filename: String?
                if let f = head.range(of: "filename=\"") { filename = String(head[f.upperBound...].prefix { $0 != "\"" }) }
                out[name] = (Data(value), filename)
            }
            return out
        }
    }

    private let listener: NWListener
    private let queue = DispatchQueue(label: "mock-server")
    private let lock = NSLock()
    private var _requests: [Request] = []
    var handler: @Sendable (Request) -> (Int, String)

    var requests: [Request] { lock.lock(); defer { lock.unlock() }; return _requests }
    var port: UInt16 { listener.port?.rawValue ?? 0 }
    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

    init(handler: @escaping @Sendable (Request) -> (Int, String)) throws {
        self.handler = handler
        let params = NWParameters.tcp
        params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: params)
        let ready = DispatchSemaphore(value: 0)
        listener.stateUpdateHandler = { state in if case .ready = state { ready.signal() } }
        listener.newConnectionHandler = { [weak self] conn in self?.serve(conn) }
        listener.start(queue: queue)
        _ = ready.wait(timeout: .now() + 5)
    }

    func stop() { listener.cancel() }

    private func serve(_ conn: NWConnection) {
        conn.start(queue: queue)
        var buffer = Data()
        func receive() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { [weak self] data, _, done, error in
                guard let self else { return }
                if let data { buffer.append(data) }
                if let request = Self.parse(buffer) {
                    self.lock.lock(); self._requests.append(request); self.lock.unlock()
                    let (status, body) = self.handler(request)
                    let payload = Data(body.utf8)
                    let head = "HTTP/1.1 \(status) X\r\nContent-Type: application/json\r\nContent-Length: \(payload.count)\r\nConnection: close\r\n\r\n"
                    conn.send(content: Data(head.utf8) + payload, completion: .contentProcessed { _ in conn.cancel() })
                } else if done || error != nil {
                    conn.cancel()
                } else {
                    receive()
                }
            }
        }
        receive()
    }

    static func parse(_ buffer: Data) -> Request? {
        guard let end = buffer.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let head = String(decoding: buffer[buffer.startIndex..<end.lowerBound], as: UTF8.self)
        var lines = head.components(separatedBy: "\r\n")
        let first = lines.removeFirst().split(separator: " ")
        var headers: [String: String] = [:]
        for line in lines {
            guard let c = line.firstIndex(of: ":") else { continue }
            headers[line[..<c].lowercased()] = line[line.index(after: c)...].trimmingCharacters(in: .whitespaces)
        }
        let length = Int(headers["content-length"] ?? "0") ?? 0
        let body = buffer[end.upperBound...]
        guard body.count >= length else { return nil }
        return Request(method: String(first.first ?? ""), path: String(first.dropFirst().first ?? ""),
                       headers: headers, body: Data(body.prefix(length)))
    }
}

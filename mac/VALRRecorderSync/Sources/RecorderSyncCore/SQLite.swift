import Foundation
import SQLite3

/// A very small SQLite wrapper: open, query rows as dictionaries, close.
final class SQLiteDB {
    enum Failure: Error, CustomStringConvertible {
        case open(String), prepare(String), step(String)
        var description: String {
            switch self {
            case .open(let m): return "open: \(m)"
            case .prepare(let m): return "prepare: \(m)"
            case .step(let m): return "step: \(m)"
            }
        }
    }

    enum Value: Equatable {
        case int(Int64), double(Double), text(String), null
        var int: Int64? { if case .int(let v) = self { return v }; if case .double(let d) = self { return Int64(d) }; return nil }
        var double: Double? { if case .double(let v) = self { return v }; if case .int(let i) = self { return Double(i) }; return nil }
        var text: String? { if case .text(let v) = self { return v }; return nil }
    }

    private var handle: OpaquePointer?

    init(path: String, readOnly: Bool) throws {
        let flags = readOnly ? SQLITE_OPEN_READONLY : (SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE)
        if sqlite3_open_v2(path, &handle, flags, nil) != SQLITE_OK {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            sqlite3_close(handle)
            handle = nil
            throw Failure.open(message)
        }
        sqlite3_busy_timeout(handle, 2000)
    }

    deinit { sqlite3_close(handle) }

    func execute(_ sql: String) throws {
        var error: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(handle, sql, nil, nil, &error) != SQLITE_OK {
            let message = error.map { String(cString: $0) } ?? "unknown"
            sqlite3_free(error)
            throw Failure.step(message)
        }
    }

    func query(_ sql: String, _ params: [Value] = []) throws -> [[String: Value]] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw Failure.prepare(String(cString: sqlite3_errmsg(handle)))
        }
        defer { sqlite3_finalize(stmt) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, p) in params.enumerated() {
            let idx = Int32(i + 1)
            switch p {
            case .int(let v): sqlite3_bind_int64(stmt, idx, v)
            case .double(let v): sqlite3_bind_double(stmt, idx, v)
            case .text(let v): sqlite3_bind_text(stmt, idx, v, -1, transient)
            case .null: sqlite3_bind_null(stmt, idx)
            }
        }
        var rows: [[String: Value]] = []
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { throw Failure.step(String(cString: sqlite3_errmsg(handle))) }
            var row: [String: Value] = [:]
            for c in 0..<sqlite3_column_count(stmt) {
                let name = String(cString: sqlite3_column_name(stmt, c))
                switch sqlite3_column_type(stmt, c) {
                case SQLITE_INTEGER: row[name] = .int(sqlite3_column_int64(stmt, c))
                case SQLITE_FLOAT: row[name] = .double(sqlite3_column_double(stmt, c))
                case SQLITE_TEXT: row[name] = .text(String(cString: sqlite3_column_text(stmt, c)))
                default: row[name] = .null // BLOBs are never read.
                }
            }
            rows.append(row)
        }
        return rows
    }

    func columns(of table: String) throws -> Set<String> {
        Set(try query("PRAGMA table_info(\(table))").compactMap { $0["name"]?.text })
    }
}

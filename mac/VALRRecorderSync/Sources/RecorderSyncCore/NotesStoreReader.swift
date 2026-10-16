import Foundation

/// Reads audio attachments from the Apple Notes store, read-only.
///
/// How (see docs in README "How it reads the recordings"):
/// 1. Copy NoteStore.sqlite (+ -wal, -shm) to a private temp folder, so Notes' own file is never
///    opened for writing and never locked by us. The copy is deleted after reading.
/// 2. In ZICCLOUDSYNCINGOBJECT, an attachment row has ZTYPEUTI (e.g. com.apple.m4a-audio),
///    ZNOTE (the note's Z_PK) and ZMEDIA (the media row's Z_PK). The media row has ZIDENTIFIER
///    and ZFILENAME, and on iOS 17+/macOS 14+ a generation (ZGENERATION1 / ZGENERATION).
/// 3. The file is at <container>/Accounts/<account ZIDENTIFIER>/Media/<media ZIDENTIFIER>/[<generation>/]<ZFILENAME>.
///    Column names move between OS versions, so they are looked up with PRAGMA table_info.
///
/// Source: apple_cloud_notes_parser (threeplanetssoftware), lib/AppleNotesEmbeddedObject.rb,
/// lib/AppleNotesEmbeddedPublicAudio.rb, lib/AppleNoteStore.rb, lib/AppleUniformTypeIdentifier.rb.
public struct NotesStoreReader {
    public let container: URL

    public init(container: URL = Paths.notesContainer) {
        self.container = container
    }

    public var databaseURL: URL { container.appendingPathComponent("NoteStore.sqlite") }

    public enum ReadError: Error, CustomStringConvertible {
        case notFound, noAccess, unexpectedSchema(String), sqlite(String)
        public var description: String {
            switch self {
            case .notFound: return "The Notes database was not found. Open Notes once and turn on iCloud Notes."
            case .noAccess: return "Full Disk Access is needed to read the Notes database."
            case .unexpectedSchema(let m): return "The Notes database looks different than expected (\(m))."
            case .sqlite(let m): return "Could not read the Notes database (\(m))."
            }
        }
    }

    /// The UTIs Notes uses for audio. From AppleUniformTypeIdentifier.rb (conforms_to_audio /
    /// conforms_to_audiovisual), limited to the audio ones.
    public static let audioUTIs: Set<String> = [
        "com.apple.m4a-audio", "public.mpeg-4-audio", "com.apple.protected-mpeg-4-audio",
        "public.aac-audio", "public.mp3", "public.audio", "com.microsoft.waveform-audio",
        "public.aiff-audio", "com.apple.coreaudio-format",
    ]

    public struct Snapshot {
        public var attachments: [AudioAttachment]
        /// Z_METADATA.Z_UUID: the store id used in AppleScript note ids.
        public var storeUUID: String?
        /// Columns we fell back on, for diagnostics.
        public var schemaNotes: [String]
    }

    /// Full Disk Access check without reading content: can we open the file for reading?
    public func accessState() -> AccessState {
        let path = databaseURL.path
        let fd = open(path, O_RDONLY)
        if fd >= 0 { close(fd); return .granted }
        switch errno {
        case ENOENT:
            // The container itself may be unreadable without FDA, which also looks like "missing".
            let dirFD = open(container.path, O_RDONLY)
            if dirFD >= 0 { close(dirFD); return .notesMissing }
            return errno == ENOENT ? .notesMissing : .denied
        default:
            return .denied
        }
    }

    public enum AccessState: Equatable { case granted, denied, notesMissing }

    public func read() throws -> Snapshot {
        switch accessState() {
        case .notesMissing: throw ReadError.notFound
        case .denied: throw ReadError.noAccess
        case .granted: break
        }
        let temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("valr-notes-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: temp) }
        let copy = temp.appendingPathComponent("NoteStore.sqlite")
        for suffix in ["", "-wal", "-shm"] {
            let src = URL(fileURLWithPath: databaseURL.path + suffix)
            if FileManager.default.fileExists(atPath: src.path) {
                do {
                    try FileManager.default.copyItem(at: src, to: URL(fileURLWithPath: copy.path + suffix))
                } catch {
                    if suffix.isEmpty { throw ReadError.noAccess }
                }
            }
        }
        // Open the COPY read-write so SQLite can apply the copied WAL. The original is untouched.
        let db: SQLiteDB
        do { db = try SQLiteDB(path: copy.path, readOnly: false) } catch { throw ReadError.sqlite("\(error)") }
        return try Self.read(db: db, container: container)
    }

    static func first(_ names: [String], in cols: Set<String>) -> String? {
        names.first { cols.contains($0) }
    }

    static func read(db: SQLiteDB, container: URL) throws -> Snapshot {
        let cols: Set<String>
        do { cols = try db.columns(of: "ZICCLOUDSYNCINGOBJECT") } catch { throw ReadError.sqlite("\(error)") }
        for needed in ["Z_PK", "ZIDENTIFIER", "ZNOTE", "ZMEDIA", "ZTYPEUTI"] where !cols.contains(needed) {
            throw ReadError.unexpectedSchema("no \(needed)")
        }
        var notes: [String] = []
        let uti = cols.contains("ZTYPEUTI1") ? "COALESCE(a.ZTYPEUTI1, a.ZTYPEUTI)" : "a.ZTYPEUTI"
        let title = cols.contains("ZTITLE1") ? "n.ZTITLE1" : "NULL"
        let folderTitle = cols.contains("ZTITLE2") && cols.contains("ZFOLDER") ? "f.ZTITLE2" : "NULL"
        let folderJoin = cols.contains("ZFOLDER") ? "LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER" : "LEFT JOIN (SELECT NULL AS Z_PK) f ON 0"
        let created = first(["ZCREATIONDATE3", "ZCREATIONDATE1", "ZCREATIONDATE"], in: cols).map { "n.\($0)" } ?? "NULL"
        let account = first(["ZACCOUNT7", "ZACCOUNT4", "ZACCOUNT3", "ZACCOUNT2"], in: cols)
        if account == nil { notes.append("no account column: searching all accounts for media") }
        let accountJoin = account.map { "LEFT JOIN ZICCLOUDSYNCINGOBJECT acct ON acct.Z_PK = n.\($0)" }
            ?? "LEFT JOIN (SELECT NULL AS ZIDENTIFIER) acct ON 0"
        let gens = ["ZGENERATION1", "ZGENERATION"].filter(cols.contains).map { "m.\($0)" }
        let generation = gens.isEmpty ? "NULL" : (gens.count == 1 ? gens[0] : "COALESCE(\(gens.joined(separator: ", ")))")
        let filename = cols.contains("ZFILENAME") ? "m.ZFILENAME" : "NULL"
        let duration = cols.contains("ZDURATION") ? "a.ZDURATION" : "NULL"
        var filters = ["a.ZMEDIA IS NOT NULL", "a.ZNOTE IS NOT NULL", "\(uti) IS NOT NULL"]
        if cols.contains("ZMARKEDFORDELETION") {
            filters.append("COALESCE(a.ZMARKEDFORDELETION, 0) = 0")
            filters.append("COALESCE(n.ZMARKEDFORDELETION, 0) = 0")
        }
        if cols.contains("ZISPASSWORDPROTECTED") {
            // Locked notes keep their media encrypted; they are skipped (never decrypted).
            filters.append("COALESCE(n.ZISPASSWORDPROTECTED, 0) = 0")
        }
        if cols.contains("ZFOLDERTYPE") && cols.contains("ZFOLDER") {
            // Folder type 1 is "Recently Deleted".
            filters.append("COALESCE(f.ZFOLDERTYPE, 0) != 1")
        }
        let sql = """
        SELECT a.ZIDENTIFIER AS att_id, \(uti) AS uti, n.Z_PK AS note_pk, \(title) AS note_title,
               \(created) AS created, \(folderTitle) AS folder_title, m.ZIDENTIFIER AS media_id,
               \(filename) AS media_filename, \(generation) AS generation,
               acct.ZIDENTIFIER AS account_id, \(duration) AS duration
        FROM ZICCLOUDSYNCINGOBJECT a
        JOIN ZICCLOUDSYNCINGOBJECT n ON n.Z_PK = a.ZNOTE
        JOIN ZICCLOUDSYNCINGOBJECT m ON m.Z_PK = a.ZMEDIA
        \(folderJoin)
        \(accountJoin)
        WHERE \(filters.joined(separator: " AND "))
        """
        let rows: [[String: SQLiteDB.Value]]
        do { rows = try db.query(sql) } catch { throw ReadError.sqlite("\(error)") }

        var out: [AudioAttachment] = []
        for row in rows {
            guard let type = row["uti"]?.text, audioUTIs.contains(type),
                  let attID = row["att_id"]?.text, let mediaID = row["media_id"]?.text,
                  let notePK = row["note_pk"]?.int else { continue }
            let fname = row["media_filename"]?.text
            let file = locateMedia(container: container, accountID: row["account_id"]?.text, mediaID: mediaID,
                                   generation: row["generation"]?.text, filename: fname)
            out.append(AudioAttachment(
                attachmentID: attID, mediaID: mediaID, uti: type, notePK: notePK,
                noteTitle: row["note_title"]?.text ?? "",
                noteCreated: row["created"]?.double.map { Date(timeIntervalSinceReferenceDate: $0) },
                folderTitle: row["folder_title"]?.text, mediaFilename: fname, fileURL: file,
                durationSeconds: row["duration"]?.double))
        }
        let store = (try? db.query("SELECT Z_UUID FROM Z_METADATA LIMIT 1"))?.first?["Z_UUID"]?.text
        return Snapshot(attachments: out, storeUUID: store, schemaNotes: notes)
    }

    static let audioExtensions: Set<String> = ["m4a", "mp3", "wav", "aac", "caf", "aiff", "aif"]

    /// The documented path first, then a search of every account's Media/<mediaID>/ folder
    /// (covers a missing or renamed generation column and the "LocalAccount" folder).
    static func locateMedia(container: URL, accountID: String?, mediaID: String, generation: String?,
                            filename: String?) -> URL? {
        let fm = FileManager.default
        if let accountID, let filename {
            var dir = container.appendingPathComponent("Accounts/\(accountID)/Media/\(mediaID)", isDirectory: true)
            if let generation, !generation.isEmpty { dir.appendPathComponent(generation, isDirectory: true) }
            let candidate = dir.appendingPathComponent(filename)
            if fm.fileExists(atPath: candidate.path) { return candidate }
        }
        var roots: [URL] = []
        let accounts = container.appendingPathComponent("Accounts", isDirectory: true)
        if let ids = try? fm.contentsOfDirectory(atPath: accounts.path) {
            roots += ids.map { accounts.appendingPathComponent("\($0)/Media/\(mediaID)", isDirectory: true) }
        }
        roots.append(container.appendingPathComponent("Media/\(mediaID)", isDirectory: true)) // older layout
        for root in roots where fm.fileExists(atPath: root.path) {
            guard let walker = fm.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey]) else { continue }
            var found: [URL] = []
            for case let url as URL in walker where audioExtensions.contains(url.pathExtension.lowercased()) {
                found.append(url)
            }
            if let filename, let exact = found.first(where: { $0.lastPathComponent == filename }) { return exact }
            if let any = found.sorted(by: { $0.path < $1.path }).last { return any }
        }
        return nil
    }
}

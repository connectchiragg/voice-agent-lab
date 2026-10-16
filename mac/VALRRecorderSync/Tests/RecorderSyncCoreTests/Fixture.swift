import Foundation
@testable import RecorderSyncCore

/// Builds a fake Notes group container: NoteStore.sqlite with the documented columns, and
/// media files under Accounts/<account>/Media/<media>/[<generation>/]<file>.
/// Nothing here touches the real Notes data.
final class NotesFixture {
    enum Layout { case modern, iOS15 }

    let root: URL
    let layout: Layout
    let db: SQLiteDB
    private var nextPK: Int64 = 100
    let accountID = "ACCOUNT-FIXTURE-0001"
    let accountPK: Int64 = 1
    let notesFolderPK: Int64 = 2
    let trashFolderPK: Int64 = 3
    let callFolderPK: Int64 = 4
    static let storeUUID = "STORE-UUID-FIXTURE"

    init(layout: Layout = .modern) throws {
        self.layout = layout
        root = FileManager.default.temporaryDirectory.appendingPathComponent("notes-fixture-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        db = try SQLiteDB(path: root.appendingPathComponent("NoteStore.sqlite").path, readOnly: false)
        let common = """
        Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZIDENTIFIER VARCHAR, ZTITLE1 VARCHAR, ZTITLE2 VARCHAR,
        ZCREATIONDATE1 TIMESTAMP, ZNOTE INTEGER, ZMEDIA INTEGER, ZTYPEUTI VARCHAR, ZFILENAME VARCHAR,
        ZFOLDER INTEGER, ZACCOUNT2 INTEGER, ZACCOUNT3 INTEGER, ZMARKEDFORDELETION INTEGER, ZISPASSWORDPROTECTED INTEGER,
        ZDURATION FLOAT, ZSERVERRECORDDATA BLOB, ZMERGEABLEDATA1 BLOB
        """
        let extra: String
        switch layout {
        case .modern:
            // iOS/macOS 18+ (ZUNAPPLIEDENCRYPTEDRECORDDATA) and 26 (ZATTRIBUTEDSNIPPET) markers, ZACCOUNT7,
            // ZCREATIONDATE3 for notes, ZGENERATION1 for media, ZTYPEUTI1, ZFOLDERTYPE.
            extra = """
            , ZACCOUNT4 INTEGER, ZACCOUNT7 INTEGER, ZCREATIONDATE3 TIMESTAMP, ZGENERATION VARCHAR, ZGENERATION1 VARCHAR,
            ZTYPEUTI1 VARCHAR, ZFOLDERTYPE INTEGER, ZUNAPPLIEDENCRYPTEDRECORDDATA BLOB, ZATTRIBUTEDSNIPPET BLOB
            """
        case .iOS15:
            extra = ", ZACCOUNT4 INTEGER, ZCREATIONDATE3 TIMESTAMP"
        }
        try db.execute("CREATE TABLE ZICCLOUDSYNCINGOBJECT (\(common)\(extra))")
        try db.execute("CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZNOTE INTEGER, ZDATA BLOB)")
        try db.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB)")
        try db.execute("INSERT INTO Z_METADATA VALUES (1, '\(Self.storeUUID)', NULL)")
        try insert(["Z_PK": .int(accountPK), "ZIDENTIFIER": .text(accountID), "ZNAME_IGNORED": .null])
        try insert(["Z_PK": .int(notesFolderPK), "ZIDENTIFIER": .text("FOLDER-NOTES"), "ZTITLE2": .text("Notes")]
                   .merging(layout == .modern ? ["ZFOLDERTYPE": .int(0)] : [:]) { a, _ in a })
        try insert(["Z_PK": .int(trashFolderPK), "ZIDENTIFIER": .text("FOLDER-TRASH"), "ZTITLE2": .text("Recently Deleted")]
                   .merging(layout == .modern ? ["ZFOLDERTYPE": .int(1)] : [:]) { a, _ in a })
        try insert(["Z_PK": .int(callFolderPK), "ZIDENTIFIER": .text("FOLDER-CALLS"), "ZTITLE2": .text("Call Recordings")])
    }

    deinit { try? FileManager.default.removeItem(at: root) }

    private func insert(_ values: [String: SQLiteDB.Value]) throws {
        let cols = try db.columns(of: "ZICCLOUDSYNCINGOBJECT")
        let kept = values.filter { cols.contains($0.key) }
        let names = kept.keys.sorted()
        let sql = "INSERT INTO ZICCLOUDSYNCINGOBJECT (\(names.joined(separator: ","))) VALUES (\(names.map { _ in "?" }.joined(separator: ",")))"
        _ = try db.query(sql, names.map { kept[$0]! })
    }

    /// Audio bytes: a fixture m4a, or a tiny fake MP4 header + random bytes (unique SHA-256).
    static func audioData(sample: String? = nil) throws -> Data {
        if let sample, let url = Bundle.module.url(forResource: sample, withExtension: "m4a", subdirectory: "Fixtures") {
            return try Data(contentsOf: url)
        }
        var d = Data([0, 0, 0, 0x18]) + Data("ftypM4A ".utf8)
        d += Data((0..<2048).map { _ in UInt8.random(in: 0...255) })
        return d
    }

    struct Added { let notePK: Int64; let attachmentID: String; let mediaID: String; let file: URL? }

    /// Adds a note with one attachment and its media row and (optionally) the file on disk.
    @discardableResult
    func addNote(title: String, uti: String = "com.apple.m4a-audio", filename: String? = nil,
                 folder: Int64? = nil, created: Date = Date(timeIntervalSince1970: 1_790_000_000),
                 generation: String? = "GEN-1", writeFile: Bool = true, fileSubdir: String? = nil,
                 sample: String? = nil, data: Data? = nil, markedDeleted: Bool = false, locked: Bool = false) throws -> Added {
        let notePK = nextPK; let attPK = nextPK + 1; let mediaPK = nextPK + 2
        nextPK += 10
        let attID = "ATT-\(notePK)", mediaID = "MEDIA-\(notePK)"
        let fname = filename ?? "\(title).m4a"
        let date = SQLiteDB.Value.double(created.timeIntervalSinceReferenceDate)
        var note: [String: SQLiteDB.Value] = ["Z_PK": .int(notePK), "ZIDENTIFIER": .text("NOTE-\(notePK)"), "ZTITLE1": .text(title),
                                              "ZFOLDER": .int(folder ?? notesFolderPK), "ZCREATIONDATE3": date,
                                              "ZMARKEDFORDELETION": .int(markedDeleted ? 1 : 0), "ZISPASSWORDPROTECTED": .int(locked ? 1 : 0)]
        note[layout == .modern ? "ZACCOUNT7" : "ZACCOUNT4"] = .int(accountPK)
        try insert(note)
        try insert(["Z_PK": .int(attPK), "ZIDENTIFIER": .text(attID), "ZNOTE": .int(notePK), "ZMEDIA": .int(mediaPK),
                    "ZTYPEUTI": .text(uti), "ZDURATION": .double(42)])
        var media: [String: SQLiteDB.Value] = ["Z_PK": .int(mediaPK), "ZIDENTIFIER": .text(mediaID), "ZFILENAME": .text(fname)]
        if let generation { media["ZGENERATION1"] = .text(generation) }
        try insert(media)
        var file: URL?
        if writeFile {
            var dir = root.appendingPathComponent("Accounts/\(accountID)/Media/\(mediaID)", isDirectory: true)
            let sub = fileSubdir ?? (layout == .modern ? generation : nil)
            if let sub { dir.appendPathComponent(sub, isDirectory: true) }
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appendingPathComponent(fname)
            try (data ?? Self.audioData(sample: sample)).write(to: url)
            file = url
        }
        return Added(notePK: notePK, attachmentID: attID, mediaID: mediaID, file: file)
    }

    var reader: NotesStoreReader { NotesStoreReader(container: root) }
}

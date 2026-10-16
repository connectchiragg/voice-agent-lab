import Foundation
import Testing
@testable import RecorderSyncCore

@Suite("Notes store reader (fixture database only)")
struct NotesReaderTests {
    @Test func findsCallRecordingWithDocumentedPath() throws {
        let fx = try NotesFixture()
        let added = try fx.addNote(title: "Call with Jane Doe", sample: "sample-a")
        let snap = try fx.reader.read()
        #expect(snap.storeUUID == NotesFixture.storeUUID)
        #expect(snap.attachments.count == 1)
        let a = try #require(snap.attachments.first)
        #expect(a.attachmentID == added.attachmentID)
        #expect(a.mediaID == added.mediaID)
        #expect(a.uti == "com.apple.m4a-audio")
        #expect(a.noteTitle == "Call with Jane Doe")
        #expect(a.folderTitle == "Notes")
        #expect(a.mediaFilename == "Call with Jane Doe.m4a")
        #expect(a.fileURL?.standardizedFileURL.path == added.file?.standardizedFileURL.path)
        #expect(a.fileURL?.path.contains("/Accounts/\(fx.accountID)/Media/\(added.mediaID)/GEN-1/") == true)
        #expect(abs((a.noteCreated?.timeIntervalSince1970 ?? 0) - 1_790_000_000) < 1)
        #expect(a.durationSeconds == 42)
        #expect(CallRecordingClassifier.classify(a) == .callRecording(contact: "Jane Doe"))
    }

    @Test func skipsNonAudioDeletedLockedAndTrashed() throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Groceries", uti: "public.jpeg", filename: "photo.jpeg")
        try fx.addNote(title: "Call with Marked", markedDeleted: true)
        try fx.addNote(title: "Call with Locked", locked: true)
        try fx.addNote(title: "Call with Trashed", folder: fx.trashFolderPK)
        let keep = try fx.addNote(title: "Call with Kept")
        let snap = try fx.reader.read()
        #expect(snap.attachments.map(\.attachmentID) == [keep.attachmentID])
    }

    @Test func findsMediaWhenGenerationColumnIsMissingOrWrong() throws {
        let fx = try NotesFixture()
        // DB says no generation, but the file sits in a generation folder: found by searching Media/<id>/.
        let added = try fx.addNote(title: "Call with Sam", generation: nil, fileSubdir: "SOME-OTHER-GEN")
        let a = try #require(try fx.reader.read().attachments.first)
        #expect(a.fileURL?.lastPathComponent == "Call with Sam.m4a")
        #expect(a.fileURL?.path.contains(added.mediaID) == true)
    }

    @Test func reportsMissingMediaFile() throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with Not Yet", writeFile: false)
        let a = try #require(try fx.reader.read().attachments.first)
        #expect(a.fileURL == nil)
        #expect(CallRecordingClassifier.classify(a) == .unsupported("audio not downloaded yet"))
    }

    @Test func readsOlderSchemaLayout() throws {
        let fx = try NotesFixture(layout: .iOS15)
        let added = try fx.addNote(title: "Call with Older Layout", generation: nil)
        let a = try #require(try fx.reader.read().attachments.first)
        #expect(a.attachmentID == added.attachmentID)
        #expect(a.fileURL?.path.hasSuffix("Media/\(added.mediaID)/Call with Older Layout.m4a") == true)
    }

    @Test func neverModifiesTheOriginalDatabase() throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with Jane Doe")
        let dbURL = fx.root.appendingPathComponent("NoteStore.sqlite")
        let before = try FileHasher.sha256(of: dbURL)
        let mtime = try FileManager.default.attributesOfItem(atPath: dbURL.path)[.modificationDate] as? Date
        _ = try fx.reader.read()
        _ = try fx.reader.read()
        #expect(try FileHasher.sha256(of: dbURL) == before)
        #expect(try FileManager.default.attributesOfItem(atPath: dbURL.path)[.modificationDate] as? Date == mtime)
    }

    @Test func missingContainerIsReported() {
        let reader = NotesStoreReader(container: URL(fileURLWithPath: "/nonexistent-\(UUID().uuidString)"))
        #expect(reader.accessState() == .notesMissing)
        #expect(throws: NotesStoreReader.ReadError.self) { try reader.read() }
    }
}

@Suite("Call recording classifier")
struct ClassifierTests {
    func att(_ title: String, folder: String? = "Notes", file: String? = nil, ext: String = "m4a") -> AudioAttachment {
        let name = file ?? "\(title).\(ext)"
        return AudioAttachment(attachmentID: "A", mediaID: "M", uti: "com.apple.m4a-audio", notePK: 1, noteTitle: title,
                               noteCreated: nil, folderTitle: folder, mediaFilename: name,
                               fileURL: URL(fileURLWithPath: "/tmp/\(name)"))
    }

    @Test func titleIsTheMainSignal() {
        #expect(CallRecordingClassifier.classify(att("Call with Jane Doe")) == .callRecording(contact: "Jane Doe"))
        #expect(CallRecordingClassifier.classify(att("call with  Jane ")) == .callRecording(contact: "Jane"))
        #expect(CallRecordingClassifier.classify(att("Anruf mit Max")) == .callRecording(contact: "Max"))
    }

    @Test func phoneNumbersAreNeverAContactName() {
        #expect(CallRecordingClassifier.classify(att("Call with +1 555 0100")) == .callRecording(contact: nil))
    }

    @Test func folderIsASecondSignal() {
        #expect(CallRecordingClassifier.classify(att("New Recording", folder: "Call Recordings")) == .callRecording(contact: nil))
        #expect(CallRecordingClassifier.classify(att("New Recording", folder: "Notes")) == .otherAudio)
    }

    @Test func attachmentNameIsAThirdSignal() {
        #expect(CallRecordingClassifier.classify(att("Renamed note", file: "Call with Ann.m4a")) == .callRecording(contact: "Ann"))
    }

    @Test func formatsVALRDoesNotTakeAreUnsupported() {
        #expect(CallRecordingClassifier.classify(att("Call with Jo", ext: "caf")) == .unsupported("format caf"))
    }

    @Test func hintsCarryNoDigits() {
        var a = att("Call with +1 555 0100")
        a.noteCreated = Date(timeIntervalSince1970: 1_790_000_000)
        let h = UploadHints.from(a, contact: nil)
        #expect(h.title == "Phone call")
        #expect(h.filename == "Phone call.m4a")
        #expect(h.name == nil)
        let named = UploadHints.from(att("Call with Jane Doe"), contact: "Jane Doe")
        #expect(named.title == "Call with Jane Doe")
        #expect(named.filename == "Call with Jane Doe.m4a")
        #expect(named.name == "Jane Doe")
    }
}

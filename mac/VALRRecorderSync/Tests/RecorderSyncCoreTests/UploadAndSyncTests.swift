import Foundation
import Testing
@testable import RecorderSyncCore

let testToken = "test-token-not-a-secret"

func tempDir() -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("valr-test-\(UUID().uuidString)", isDirectory: true)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

func okBody(duplicate: Bool = false, id: String = "phone:abc") -> String {
    "{\"ok\": true, \"result\": \"ok\", \"duplicate\": \(duplicate), \"id\": \"\(id)\"}"
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock(); private var n = 0
    func next() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n }
}

final class FakeDeleter: NoteDeleting, @unchecked Sendable {
    private let lock = NSLock()
    var calls: [(String, String)] = []
    func moveToRecentlyDeleted(noteID: String, expectedTitle: String) async throws {
        lock.lock(); calls.append((noteID, expectedTitle)); lock.unlock()
    }
}

let noSleep = RetryPolicy(attempts: 3, baseDelay: 0.01, maxDelay: 0.01, sleep: { _ in })

@Suite("Uploader against a local mock server")
struct UploaderTests {
    @Test func sendsMultipartWithBearerAndHints() async throws {
        let server = try MockServer { _ in (200, okBody()) }
        defer { server.stop() }
        let file = tempDir().appendingPathComponent("Call with Jane Doe.m4a")
        let audio = try NotesFixture.audioData(sample: "sample-a")
        try audio.write(to: file)
        let hints = UploadHints(filename: "Call with Jane Doe.m4a", title: "Call with Jane Doe", name: "Jane Doe",
                                recordedAt: Date(timeIntervalSince1970: 1_790_000_000))
        let response = try await Uploader(server: server.url, token: testToken).upload(file: file, hints: hints)
        #expect(response.isOK && response.id == "phone:abc" && response.duplicate == false)

        let req = try #require(server.requests.first)
        #expect(req.method == "POST" && req.path == "/hooks/recording-upload")
        #expect(req.headers["authorization"] == "Bearer \(testToken)")
        let form = req.multipart()
        #expect(form["file"]?.0 == audio)
        #expect(form["file"]?.1 == "Call with Jane Doe.m4a")
        #expect(form["filename"].map { String(decoding: $0.0, as: UTF8.self) } == "Call with Jane Doe.m4a")
        #expect(form["title"].map { String(decoding: $0.0, as: UTF8.self) } == "Call with Jane Doe")
        #expect(form["name"].map { String(decoding: $0.0, as: UTF8.self) } == "Jane Doe")
        #expect(form["recorded_at"].map { String(decoding: $0.0, as: UTF8.self) } == "2026-09-21T14:13:20Z")
    }

    @Test func classifiesErrors() async throws {
        let file = tempDir().appendingPathComponent("a.m4a")
        try NotesFixture.audioData().write(to: file)
        let hints = UploadHints(filename: "a.m4a", title: nil, name: nil, recordedAt: nil)
        for (status, expected) in [(401, UploadError.unauthorized), (415, .rejected(415, "Not audio")),
                                   (503, .transient("HTTP 503")), (429, .transient("HTTP 429"))] {
            let server = try MockServer { _ in (status, "{\"detail\": \"Not audio\"}") }
            defer { server.stop() }
            await #expect(throws: expected) {
                _ = try await Uploader(server: server.url, token: testToken).upload(file: file, hints: hints)
            }
        }
        // Nothing listening: a network error, which is retryable.
        let closed = try MockServer { _ in (200, okBody()) }
        let url = closed.url
        closed.stop()
        do {
            _ = try await Uploader(server: url, token: testToken).upload(file: file, hints: hints)
            Issue.record("expected an error")
        } catch let e as UploadError {
            #expect(e.isRetryable)
        }
    }
}

@Suite("Sync engine: fixture Notes + mock VALR")
struct SyncEngineTests {
    @Test func uploadsOnlyCallRecordingsOnceAndRecordsState() async throws {
        let fx = try NotesFixture()
        let jane = try fx.addNote(title: "Call with Jane Doe", sample: "sample-a")
        let number = try fx.addNote(title: "Call with +1 555 0100", sample: "sample-b")
        try fx.addNote(title: "Voice memo about ideas")
        try fx.addNote(title: "Call with Not Yet", writeFile: false)
        let counter = Counter()
        let server = try MockServer { _ in (200, okBody(id: "phone:\(counter.next())")) }
        defer { server.stop() }
        let store = StateStore(directory: tempDir())
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep)
        let uploader = Uploader(server: server.url, token: testToken)
        let settings = AppSettings(serverURL: server.url, onboardingDone: true)

        let r1 = await engine.run(settings: settings, uploader: uploader)
        #expect(r1.sent == 2 && r1.problem == nil && r1.waitingForDownload == 1)
        #expect(server.requests.count == 2)
        let st = store.state
        #expect(st.done[jane.attachmentID]?.sha256 == (try FileHasher.sha256(of: jane.file!)))
        #expect(st.done[number.attachmentID] != nil)
        #expect(st.sentToday() == 2)
        #expect(st.lastSync != nil)
        // The number note sends no digits.
        let titles = server.requests.compactMap { $0.multipart()["title"].map { String(decoding: $0.0, as: UTF8.self) } }
        #expect(Set(titles) == ["Call with Jane Doe", "Phone call"])
        #expect(server.requests.allSatisfy { $0.multipart()["filename"].map { String(decoding: $0.0, as: UTF8.self) }?.contains("555") == false })

        // Second pass: nothing new is sent.
        let r2 = await engine.run(settings: settings, uploader: uploader)
        #expect(r2.sent == 0 && server.requests.count == 2)

        // State survives a restart.
        let reloaded = StateStore(directory: store.url.deletingLastPathComponent()).state
        #expect(reloaded.done.mapValues(\.sha256) == store.state.done.mapValues(\.sha256))
        #expect(reloaded.sentByDay == store.state.sentByDay)
        let raw = try String(contentsOf: store.url, encoding: .utf8)
        #expect(!raw.contains("Jane") && !raw.contains(testToken))
    }

    @Test func retriesTransientErrorsThenSucceeds() async throws {
        let fx = try NotesFixture()
        let added = try fx.addNote(title: "Call with Retry")
        let counter = Counter()
        let server = try MockServer { _ in counter.next() < 3 ? (503, "{\"detail\": \"busy\"}") : (200, okBody()) }
        defer { server.stop() }
        let store = StateStore(directory: tempDir())
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep)
        let r = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: server.url, token: testToken))
        #expect(r.sent == 1)
        #expect(server.requests.count == 3)
        #expect(store.state.done[added.attachmentID] != nil)
    }

    @Test func notMarkedDoneWithoutOK() async throws {
        let fx = try NotesFixture()
        let added = try fx.addNote(title: "Call with Down")
        let server = try MockServer { _ in (502, "bad gateway") }
        defer { server.stop() }
        let store = StateStore(directory: tempDir())
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep)
        let r = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: server.url, token: testToken))
        #expect(r.sent == 0)
        if case .offline = r.problem {} else { Issue.record("expected offline, got \(String(describing: r.problem))") }
        #expect(store.state.done[added.attachmentID] == nil)
        #expect(server.requests.count == 3) // 3 attempts
        // Backs off: an automatic pass right after does not hit the server, a forced one does.
        _ = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: server.url, token: testToken))
        #expect(server.requests.count == 3)
        _ = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: server.url, token: testToken), force: true)
        #expect(server.requests.count == 6)
    }

    @Test func unauthorizedStopsAndRejectedIsNotRetried() async throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with A")
        let store = StateStore(directory: tempDir())
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep)
        let s401 = try MockServer { _ in (401, "{\"detail\": \"Missing or wrong upload token.\"}") }
        defer { s401.stop() }
        let r = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: s401.url, token: "wrong"))
        #expect(r.problem == .unauthorized && s401.requests.count == 1 && store.state.done.isEmpty)

        let s415 = try MockServer { _ in (415, "{\"detail\": \"Not audio\"}") }
        defer { s415.stop() }
        let r2 = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: s415.url, token: testToken))
        #expect(r2.rejected == 1 && store.state.rejected.count == 1)
        _ = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: s415.url, token: testToken))
        #expect(s415.requests.count == 1)
    }

    @Test func duplicateAnswerCountsAsDoneAndSameAudioIsNotResent() async throws {
        let fx = try NotesFixture()
        let audio = try NotesFixture.audioData()
        try fx.addNote(title: "Call with Twice", data: audio)
        try fx.addNote(title: "Call with Twice again", data: audio)
        let server = try MockServer { _ in (200, okBody(duplicate: true)) }
        defer { server.stop() }
        let store = StateStore(directory: tempDir())
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep)
        let r = await engine.run(settings: AppSettings(onboardingDone: true), uploader: Uploader(server: server.url, token: testToken))
        #expect(r.sent == 0 && r.duplicates == 2 && server.requests.count == 1)
        #expect(store.state.done.count == 2 && store.state.sentToday() == 0)
    }

    @Test func existingRecordingsCanBeSkipped() async throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with Old", created: Date(timeIntervalSince1970: 1_700_000_000))
        try fx.addNote(title: "Call with New", created: Date(timeIntervalSince1970: 1_800_000_000))
        let server = try MockServer { _ in (200, okBody()) }
        defer { server.stop() }
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: StateStore(directory: tempDir()), retry: noSleep)
        let settings = AppSettings(includeExistingRecordings: false, onboardingDone: true, setupDate: Date(timeIntervalSince1970: 1_750_000_000))
        let r = await engine.run(settings: settings, uploader: Uploader(server: server.url, token: testToken))
        #expect(r.sent == 1)
        #expect(server.requests.first?.multipart()["title"].map { String(decoding: $0.0, as: UTF8.self) } == "Call with New")
    }

    @Test func deletesOnlyConfirmedCallNotesWhenTurnedOn() async throws {
        let fx = try NotesFixture()
        let call = try fx.addNote(title: "Call with Delete Me")
        try fx.addNote(title: "Voice memo")
        let server = try MockServer { _ in (200, okBody()) }
        defer { server.stop() }
        let deleter = FakeDeleter()
        let reader = fx.reader
        let store = StateStore(directory: tempDir())
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep, deleter: deleter)
        let uploader = Uploader(server: server.url, token: testToken)

        // Off by default: nothing deleted.
        _ = await engine.run(settings: AppSettings(onboardingDone: true), uploader: uploader)
        #expect(deleter.calls.isEmpty)

        let on = AppSettings(deleteNoteAfterUpload: true, includeOtherAudio: true, onboardingDone: true)
        let r = await engine.run(settings: on, uploader: uploader)
        #expect(r.notesDeleted == 1)
        #expect(deleter.calls.count == 1)
        #expect(deleter.calls.first?.0 == "x-coredata://\(NotesFixture.storeUUID)/ICNote/p\(call.notePK)")
        #expect(deleter.calls.first?.1 == "Call with Delete Me")
        _ = await engine.run(settings: on, uploader: uploader)
        #expect(deleter.calls.count == 1) // not twice
    }

    @Test func noAccessIsReportedWithoutUploading() async throws {
        let store = StateStore(directory: tempDir())
        let engine = SyncEngine(source: { throw NotesStoreReader.ReadError.noAccess }, store: store, retry: noSleep)
        let r = await engine.run(settings: AppSettings(), uploader: nil)
        #expect(r.problem == .noAccess)
    }

    @Test func appleScriptIsQuotedAndChecksTitle() {
        let s = AppleScriptNoteDeleter.script(noteID: "x-coredata://S/ICNote/p5", expectedTitle: "Call with \"Q\" \\ x")
        #expect(s.contains("note id \"x-coredata://S/ICNote/p5\""))
        #expect(s.contains("is not \"Call with \\\"Q\\\" \\\\ x\""))
        #expect(s.contains("delete theNote"))
    }
}

@Suite("Pairing client (future endpoint)")
struct PairingTests {
    @Test func notSupportedOn404AndTokenOn200() async throws {
        let missing = try MockServer { _ in (404, "{\"detail\": \"Not Found\"}") }
        defer { missing.stop() }
        await #expect(throws: PairingClient.PairingError.notSupported) {
            _ = try await PairingClient(server: missing.url).exchange(code: "abcd-1234", deviceName: "Mac")
        }
        let ok = try MockServer { _ in (200, "{\"ok\": true, \"token\": \"paired-test-token\", \"account\": \"Owner\"}") }
        defer { ok.stop() }
        let paired = try await PairingClient(server: ok.url).exchange(code: "abcd-1234", deviceName: "Mac")
        #expect(paired.token == "paired-test-token")
        let sent = try #require(ok.requests.first)
        #expect(sent.path == "/hooks/device-pair")
        #expect(String(decoding: sent.body, as: UTF8.self).contains("ABCD1234"))
    }
}

@Suite("Token check")
struct TokenCheckTests {
    @Test func goodTokenGets415BadGets401() async throws {
        let server = try MockServer { req in
            req.headers["authorization"] == "Bearer \(testToken)" ? (415, "{\"detail\": \"form\"}") : (401, "{\"detail\": \"token\"}")
        }
        defer { server.stop() }
        #expect(await Uploader(server: server.url, token: testToken).checkToken() == .valid)
        #expect(await Uploader(server: server.url, token: "nope").checkToken() == .invalid)
        #expect(server.requests.allSatisfy { $0.body.isEmpty })
    }
}

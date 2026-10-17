import Foundation
import Testing
@testable import RecorderSyncCore

final class FakeFetcher: DownloadFetching, @unchecked Sendable {
    private let lock = NSLock()
    private var _calls: [([DownloadRequest], DownloadFetchMethod)] = []
    var error: DownloadFetchError?
    /// Called on each fetch, e.g. to put the audio file in place as Notes would.
    var onFetch: (([DownloadRequest]) -> Void)?
    var calls: [([DownloadRequest], DownloadFetchMethod)] { lock.lock(); defer { lock.unlock() }; return _calls }
    private func record(_ r: [DownloadRequest], _ m: DownloadFetchMethod) { lock.lock(); _calls.append((r, m)); lock.unlock() }
    func fetch(_ requests: [DownloadRequest], method: DownloadFetchMethod) async throws {
        record(requests, method)
        if let error { throw error }
        onFetch?(requests)
    }
}

final class Clock: @unchecked Sendable {
    private let lock = NSLock()
    private var t = Date(timeIntervalSince1970: 1_790_000_000)
    var now: Date { lock.lock(); defer { lock.unlock() }; return t }
    func advance(_ s: TimeInterval) { lock.lock(); t += s; lock.unlock() }
}

@Suite("Asking Notes to download waiting recordings")
struct DownloadFetchTests {
    @Test func trackerGoesSilentThenShowRateLimitedThenGivesUp() {
        var t = DownloadFetchTracker(policy: DownloadFetchPolicy(minInterval: 30, maxAttempts: 3, silentAttempts: 1))
        let start = Date(timeIntervalSince1970: 1_000)
        #expect(t.plan(["A"], at: start) == ["A": .silent])
        t.recordAttempt(["A"], at: start)
        #expect(t.plan(["A"], at: start + 10).isEmpty)          // at most one attempt per 30 s
        #expect(t.nextDue(for: ["A"]) == start + 30)
        #expect(t.plan(["A"], at: start + 30) == ["A": .show])
        t.recordAttempt(["A"], at: start + 30)
        t.recordAttempt(["A"], at: start + 60)
        #expect(t.hasGivenUp("A"))
        #expect(t.plan(["A"], at: start + 600).isEmpty)
        #expect(t.nextDue(for: ["A"]) == nil)
        #expect(t.plan(["A"], at: start + 600, now: true) == ["A": .show]) // "Fetch now"
        t.keepOnly([])                                           // downloaded: forgotten
        #expect(t.entries.isEmpty)
    }

    @Test func requestUsesTheAttachmentScriptID() {
        let a = AudioAttachment(attachmentID: "ATT", mediaID: "M", uti: "com.apple.m4a-audio", notePK: 7,
                                noteTitle: "Call with Someone", noteCreated: nil, folderTitle: nil,
                                mediaFilename: "x.m4a", fileURL: nil, attachmentPK: 8)
        #expect(DownloadRequest.make(for: a, storeUUID: "S")?.scriptID == "x-coredata://S/ICAttachment/p8")
        #expect(DownloadRequest.make(for: a, storeUUID: nil) == nil)
        var noPK = a; noPK.attachmentPK = nil
        #expect(DownloadRequest.make(for: noPK, storeUUID: "S") == nil)
    }

    @Test func scriptsAreQuotedAndOnlyReadOrShow() {
        let save = NotesFetchScripts.save(scriptID: "x-coredata://S/ICAttachment/p8", to: "/tmp/a \"b\"/0.m4a")
        #expect(save.contains("save attachment id \"x-coredata://S/ICAttachment/p8\" in (POSIX file \"/tmp/a \\\"b\\\"/0.m4a\")"))
        #expect(save.contains("with timeout of 20 seconds"))
        let show = NotesFetchScripts.show(scriptID: "x-coredata://S/ICAttachment/p8")
        #expect(show.contains("show attachment id \"x-coredata://S/ICAttachment/p8\""))
        for s in [save, show] { #expect(!s.contains("delete") && !s.contains("set ")) }
        #expect(DownloadFetchError.from(appleScriptError: -1743) == .notAllowed)
        #expect(DownloadFetchError.from(appleScriptError: -1744) == .notAllowed)
        #expect(DownloadFetchError.from(appleScriptError: -1728) == .failed(-1728))
    }

    @Test func readerReportsTheAttachmentPK() throws {
        let fx = try NotesFixture()
        let added = try fx.addNote(title: "Call with Not Yet", writeFile: false)
        let snap = try fx.reader.read()
        #expect(snap.attachments.first?.attachmentPK == added.notePK + 1)
        #expect(snap.storeUUID == NotesFixture.storeUUID)
    }

    @Test func engineAsksNotesThenUploadsOnceTheAudioArrives() async throws {
        let fx = try NotesFixture()
        let waiting = try fx.addNote(title: "Call with Not Yet", writeFile: false)
        try fx.addNote(title: "Voice memo not downloaded", writeFile: false) // not a call: never fetched
        let server = try MockServer { _ in (200, okBody()) }
        defer { server.stop() }
        let clock = Clock()
        let fetcher = FakeFetcher()
        let reader = fx.reader
        let store = StateStore(directory: tempDir())
        let engine = SyncEngine(source: { try reader.read() }, store: store, retry: noSleep, fetcher: fetcher,
                                fetchPolicy: DownloadFetchPolicy(minInterval: 30, maxAttempts: 3, silentAttempts: 1),
                                now: { clock.now })
        let uploader = Uploader(server: server.url, token: testToken)
        let settings = AppSettings(serverURL: server.url, onboardingDone: true)

        let r1 = await engine.run(settings: settings, uploader: uploader)
        #expect(r1.waitingForDownload == 1 && r1.downloadRequested == 1 && r1.sent == 0 && r1.problem == nil)
        #expect(fetcher.calls.count == 1 && fetcher.calls[0].1 == .silent)
        #expect(fetcher.calls[0].0 == [DownloadRequest(attachmentID: waiting.attachmentID,
                                                       scriptID: "x-coredata://\(NotesFixture.storeUUID)/ICAttachment/p\(waiting.notePK + 1)")])
        #expect(r1.nextDownloadCheck == clock.now + 30)

        // A pass 10 s later (e.g. FSEvents) does not ask again.
        clock.advance(10)
        let r2 = await engine.run(settings: settings, uploader: uploader)
        #expect(r2.downloadRequested == 0 && fetcher.calls.count == 1)

        // 30 s after the first try: opens the note; this time Notes downloads the audio.
        clock.advance(25)
        fetcher.onFetch = { _ in
            let dir = fx.root.appendingPathComponent("Accounts/\(fx.accountID)/Media/\(waiting.mediaID)/GEN-1", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? NotesFixture.audioData().write(to: dir.appendingPathComponent("Call with Not Yet.m4a"))
        }
        let r3 = await engine.run(settings: settings, uploader: uploader)
        #expect(fetcher.calls.count == 2 && fetcher.calls[1].1 == .show)
        #expect(r3.downloadRequested == 1)

        // The next pass (FSEvents after the download) uploads it and stops asking.
        let r4 = await engine.run(settings: settings, uploader: uploader)
        #expect(r4.sent == 1 && r4.waitingForDownload == 0 && r4.downloadRequested == 0 && r4.nextDownloadCheck == nil)
        #expect(store.state.done[waiting.attachmentID] != nil)
        #expect(fetcher.calls.count == 2)
    }

    @Test func givesUpAfterMaxAttemptsUntilFetchNow() async throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with Never Arrives", writeFile: false)
        let clock = Clock()
        let fetcher = FakeFetcher()
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: StateStore(directory: tempDir()), retry: noSleep,
                                fetcher: fetcher, fetchPolicy: DownloadFetchPolicy(minInterval: 30, maxAttempts: 2),
                                now: { clock.now })
        let settings = AppSettings(onboardingDone: true)
        _ = await engine.run(settings: settings, uploader: nil)
        clock.advance(31)
        let r2 = await engine.run(settings: settings, uploader: nil)
        #expect(r2.downloadGaveUp == 1 && r2.nextDownloadCheck == nil)
        clock.advance(600)
        let r3 = await engine.run(settings: settings, uploader: nil)
        #expect(r3.downloadRequested == 0 && r3.downloadGaveUp == 1 && r3.waitingForDownload == 1)
        #expect(fetcher.calls.map(\.1) == [.silent, .show])

        let r4 = await engine.run(settings: settings, uploader: nil, force: true, fetchNow: true)
        #expect(r4.downloadRequested == 1 && r4.downloadGaveUp == 0)
        #expect(fetcher.calls.last?.1 == .show)
    }

    @Test func notAllowedIsReportedAndSkippedRecordingsAreNotFetched() async throws {
        let fx = try NotesFixture()
        try fx.addNote(title: "Call with Old One", created: Date(timeIntervalSince1970: 1_700_000_000), writeFile: false)
        let fetcher = FakeFetcher()
        let reader = fx.reader
        let engine = SyncEngine(source: { try reader.read() }, store: StateStore(directory: tempDir()), retry: noSleep,
                                fetcher: fetcher)
        // Existing recordings are excluded: the old note is neither counted nor fetched.
        let skip = AppSettings(includeExistingRecordings: false, onboardingDone: true, setupDate: Date(timeIntervalSince1970: 1_750_000_000))
        let r1 = await engine.run(settings: skip, uploader: nil)
        #expect(r1.waitingForDownload == 0 && fetcher.calls.isEmpty)

        fetcher.error = .notAllowed
        let r2 = await engine.run(settings: AppSettings(onboardingDone: true), uploader: nil)
        #expect(r2.problem == .notesAutomationDenied && r2.waitingForDownload == 1)
        #expect(fetcher.calls.count == 1)
    }
}

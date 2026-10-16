import Foundation
import os

let log = Logger(subsystem: "com.haciensus.valr.mac", category: "sync")

public struct RetryPolicy: Sendable {
    public var attempts: Int
    public var baseDelay: TimeInterval
    public var maxDelay: TimeInterval
    public var sleep: @Sendable (TimeInterval) async -> Void

    public init(attempts: Int = 4, baseDelay: TimeInterval = 2, maxDelay: TimeInterval = 60,
                sleep: @escaping @Sendable (TimeInterval) async -> Void = { try? await Task.sleep(nanoseconds: UInt64($0 * 1e9)) }) {
        self.attempts = attempts
        self.baseDelay = baseDelay
        self.maxDelay = maxDelay
        self.sleep = sleep
    }

    /// 2 s, 4 s, 8 s … capped, with up to 20% jitter.
    public func delay(afterAttempt n: Int) -> TimeInterval {
        let raw = min(maxDelay, baseDelay * pow(2, Double(n - 1)))
        return raw * Double.random(in: 1.0...1.2)
    }
}

/// Moves a note to Recently Deleted. The app uses AppleScript; tests use a fake.
public protocol NoteDeleting: Sendable {
    func moveToRecentlyDeleted(noteID: String, expectedTitle: String) async throws
}

public struct SyncReport: Equatable, Sendable {
    public enum Problem: Equatable, Sendable {
        case noToken, noAccess, notesMissing, unauthorized, offline(String), schema(String), deleteFailed(String)
    }
    public var sent = 0
    public var duplicates = 0
    public var rejected = 0
    public var waitingForDownload = 0
    public var pending = 0
    public var notesDeleted = 0
    public var callRecordingsSeen = 0
    public var problem: Problem?
    public init() {}
}

/// One sync pass: read Notes, pick new call recordings, upload, record, optionally delete.
public actor SyncEngine {
    public typealias SnapshotSource = @Sendable () throws -> NotesStoreReader.Snapshot

    private let source: SnapshotSource
    private let store: StateStore
    private let retry: RetryPolicy
    private let deleter: NoteDeleting?
    private let now: @Sendable () -> Date
    /// Uploads per pass. VALR allows 60 an hour per address.
    public var maxUploadsPerPass = 30
    public static let maxBytes = 100 * 1024 * 1024

    private var consecutiveFailures = 0
    private var notBefore: Date?

    public init(source: @escaping SnapshotSource, store: StateStore, retry: RetryPolicy = RetryPolicy(),
                deleter: NoteDeleting? = nil, now: @escaping @Sendable () -> Date = { Date() }) {
        self.source = source
        self.store = store
        self.retry = retry
        self.deleter = deleter
        self.now = now
    }

    public func run(settings: AppSettings, uploader: Uploader?, force: Bool = false) async -> SyncReport {
        var report = SyncReport()
        if !force, let notBefore, now() < notBefore {
            report.problem = .offline("waiting before the next try")
            return report
        }
        let snapshot: NotesStoreReader.Snapshot
        do {
            snapshot = try source()
        } catch let error as NotesStoreReader.ReadError {
            switch error {
            case .noAccess: report.problem = .noAccess
            case .notFound: report.problem = .notesMissing
            case .unexpectedSchema(let m), .sqlite(let m): report.problem = .schema(m)
            }
            return report
        } catch {
            report.problem = .schema("\(error)")
            return report
        }

        // Pick the recordings to send.
        var work: [(AudioAttachment, String?, Bool)] = [] // attachment, contact, isCall
        for a in snapshot.attachments {
            let kind = CallRecordingClassifier.classify(a)
            var contact: String?
            var isCall = false
            switch kind {
            case .callRecording(let c): contact = c; isCall = true; report.callRecordingsSeen += 1
            case .otherAudio: guard settings.includeOtherAudio else { continue }
            case .unsupported(let why):
                if why.hasPrefix("audio not downloaded"), CallRecordingClassifier.contactFromTitle(a.noteTitle) != nil {
                    report.waitingForDownload += 1
                }
                continue
            }
            let st = store.state
            if st.done[a.attachmentID] != nil || st.rejected[a.attachmentID] != nil { continue }
            if !settings.includeExistingRecordings, let created = a.noteCreated, created < settings.setupDate { continue }
            work.append((a, contact, isCall))
        }
        report.pending = work.count

        if !work.isEmpty {
            guard let uploader else { report.problem = .noToken; return report }
            for (a, contact, _) in work.prefix(maxUploadsPerPass) {
                guard let file = a.fileURL else { continue }
                let size = ((try? FileManager.default.attributesOfItem(atPath: file.path))?[.size] as? NSNumber)?.intValue ?? 0
                if size > Self.maxBytes || !FileHasher.looksLikeAudio(file) {
                    let reason = size > Self.maxBytes ? "larger than 100 MB" : "not audio"
                    store.update { $0.rejected[a.attachmentID] = .init(reason: reason, at: now()) }
                    report.rejected += 1
                    continue
                }
                guard let sha = try? FileHasher.sha256(of: file) else { continue }
                if store.state.isDone(sha: sha) {
                    // Same audio already sent under another attachment id (e.g. a re-synced note).
                    store.update { $0.done[a.attachmentID] = .init(sha256: sha, valrID: nil, duplicate: true, at: now(), notePK: a.notePK) }
                    report.duplicates += 1
                    continue
                }
                let hints = UploadHints.from(a, contact: contact)
                switch await uploadWithRetry(uploader, file: file, hints: hints) {
                case .success(let response):
                    consecutiveFailures = 0
                    notBefore = nil
                    let day = SyncState.dayKey(now())
                    store.update {
                        $0.done[a.attachmentID] = .init(sha256: sha, valrID: response.id, duplicate: response.duplicate == true,
                                                        at: now(), notePK: a.notePK)
                        if response.duplicate != true { $0.sentByDay[day, default: 0] += 1 }
                    }
                    if response.duplicate == true { report.duplicates += 1 } else { report.sent += 1 }
                    log.info("Upload ok (duplicate: \(response.duplicate == true, privacy: .public))")
                case .failure(.unauthorized):
                    report.problem = .unauthorized
                    log.error("Upload refused: token")
                    return finish(report)
                case .failure(.rejected(let code, let detail)):
                    store.update { $0.rejected[a.attachmentID] = .init(reason: "HTTP \(code): \(detail)", at: now()) }
                    report.rejected += 1
                    log.error("Upload rejected: HTTP \(code, privacy: .public)")
                case .failure(let other):
                    consecutiveFailures += 1
                    let wait = min(30 * 60, 30 * pow(2, Double(consecutiveFailures - 1)))
                    notBefore = now().addingTimeInterval(wait)
                    report.problem = .offline(other.description)
                    log.error("Upload failed, will retry later: \(other.description, privacy: .public)")
                    return finish(report)
                }
            }
        }

        if settings.deleteNoteAfterUpload, let deleter {
            await deleteFinishedNotes(snapshot: snapshot, deleter: deleter, report: &report)
        }
        return finish(report)
    }

    private func finish(_ report: SyncReport) -> SyncReport {
        store.update { $0.lastSync = now() }
        var r = report
        r.pending = max(0, r.pending - r.sent - r.duplicates - r.rejected)
        return r
    }

    private func uploadWithRetry(_ uploader: Uploader, file: URL, hints: UploadHints) async -> Result<UploadResponse, UploadError> {
        var attempt = 0
        while true {
            attempt += 1
            do {
                return .success(try await uploader.upload(file: file, hints: hints))
            } catch let error as UploadError {
                if !error.isRetryable || attempt >= retry.attempts { return .failure(error) }
                await retry.sleep(retry.delay(afterAttempt: attempt))
            } catch {
                return .failure(.transient("local file error"))
            }
        }
    }

    /// Deletes a note only when it is a call recording and every audio attachment in it was
    /// confirmed by VALR. Uses the title as a safety check in the AppleScript.
    private func deleteFinishedNotes(snapshot: NotesStoreReader.Snapshot, deleter: NoteDeleting, report: inout SyncReport) async {
        guard let storeUUID = snapshot.storeUUID else { return }
        let byNote = Dictionary(grouping: snapshot.attachments, by: \.notePK)
        for (pk, atts) in byNote {
            guard let first = atts.first, CallRecordingClassifier.contactFromTitle(first.noteTitle) != nil else { continue }
            let st = store.state
            guard atts.allSatisfy({ st.done[$0.attachmentID] != nil }),
                  !atts.contains(where: { st.done[$0.attachmentID]?.noteDeleted == true }) else { continue }
            let noteID = "x-coredata://\(storeUUID)/ICNote/p\(pk)"
            do {
                try await deleter.moveToRecentlyDeleted(noteID: noteID, expectedTitle: first.noteTitle)
                store.update { s in for a in atts { s.done[a.attachmentID]?.noteDeleted = true } }
                report.notesDeleted += 1
            } catch {
                report.problem = report.problem ?? .deleteFailed("\(error)")
                log.error("Could not move a note to Recently Deleted")
            }
        }
    }

    /// Counts only, for the Settings "Check Notes" button. No titles or names.
    public static func diagnose(_ snapshot: NotesStoreReader.Snapshot) -> String {
        var calls = 0, other = 0, missing = 0, unsupported = 0
        for a in snapshot.attachments {
            switch CallRecordingClassifier.classify(a) {
            case .callRecording: calls += 1
            case .otherAudio: other += 1
            case .unsupported(let why): if why.hasPrefix("audio not downloaded") { missing += 1 } else { unsupported += 1 }
            }
        }
        var lines = ["Audio attachments in Notes: \(snapshot.attachments.count)",
                     "Call recordings ready to send: \(calls)",
                     "Other audio (not sent unless turned on): \(other)",
                     "Not downloaded from iCloud yet: \(missing)",
                     "Unsupported formats: \(unsupported)"]
        lines += snapshot.schemaNotes
        return lines.joined(separator: "\n")
    }
}

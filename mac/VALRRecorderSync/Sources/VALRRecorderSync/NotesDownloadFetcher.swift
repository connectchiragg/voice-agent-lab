import AppKit
import Foundation
import RecorderSyncCore
import os

private let fetchLog = Logger(subsystem: "com.haciensus.valr.mac", category: "fetch")

/// Asks Notes to download call recordings whose audio has not arrived from iCloud yet
/// (see RecorderSyncCore/NotesDownloadFetch.swift for when and why). Needs the Automation
/// permission for Notes; macOS asks the first time ("VALR wants access to control Notes").
/// Never changes a note: `save` writes a copy to a private temp folder (deleted at once) and
/// `show` only selects the note in Notes' window.
struct NotesDownloadFetcher: DownloadFetching {
    static let notesBundleID = "com.apple.Notes"

    func fetch(_ requests: [DownloadRequest], method: DownloadFetchMethod) async throws {
        guard !requests.isEmpty else { return }
        await Self.launchNotesHiddenIfNeeded()
        switch method {
        case .silent: try await saveCopies(requests)
        case .show: try await showNotes(requests)
        }
    }

    // MARK: Silent: save a copy, which makes Notes load the attachment's data

    private func saveCopies(_ requests: [DownloadRequest]) async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("valr-fetch-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: dir) } // the copy is never used: Notes' own file is uploaded
        for (i, r) in requests.enumerated() {
            let path = dir.appendingPathComponent("\(i).m4a").path
            try await Self.run(NotesFetchScripts.save(scriptID: r.scriptID, to: path))
        }
    }

    // MARK: Show: open the note in Notes, then give the focus back

    private func showNotes(_ requests: [DownloadRequest]) async throws {
        let previous = NSWorkspace.shared.frontmostApplication
        let notesWasActive = previous?.bundleIdentifier == Self.notesBundleID
        let notesWasHidden = Self.runningNotes()?.isHidden ?? true
        do {
            for r in requests {
                try await Self.run(NotesFetchScripts.show(scriptID: r.scriptID))
                // Notes starts the download when the note is on screen; give it a moment per note.
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
        } catch {
            if !notesWasActive { await Self.restore(previous, hideNotes: notesWasHidden) }
            throw error
        }
        guard !notesWasActive else { return }
        // Notes activates itself shortly after `show` returns; give the focus back only after that,
        // or Notes would come to the front again afterwards.
        let deadline = Date().addingTimeInterval(2)
        while Self.runningNotes()?.isActive != true, Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        try? await Task.sleep(nanoseconds: 300_000_000)
        await Self.restore(previous, hideNotes: notesWasHidden)
    }

    /// Hides Notes first (it is the active app now) when it was hidden before, then reactivates
    /// the app that was in front.
    @MainActor
    static func restore(_ previous: NSRunningApplication?, hideNotes: Bool) {
        if hideNotes { runningNotes()?.hide() }
        if let previous, !previous.isTerminated, previous.bundleIdentifier != notesBundleID { previous.activate() }
    }

    // MARK: Helpers

    static func runningNotes() -> NSRunningApplication? {
        NSRunningApplication.runningApplications(withBundleIdentifier: notesBundleID).first
    }

    /// Starts Notes hidden and in the background when it is not running (Notes must run to download).
    static func launchNotesHiddenIfNeeded() async {
        guard runningNotes() == nil,
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: notesBundleID) else { return }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.hides = true
        config.addsToRecentItems = false
        _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: config)
        let deadline = Date().addingTimeInterval(10)
        while runningNotes()?.isFinishedLaunching != true, Date() < deadline {
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
    }

    @MainActor
    static func run(_ source: String) throws {
        var error: NSDictionary?
        guard let script = NSAppleScript(source: source) else { throw DownloadFetchError.failed(-1) }
        script.executeAndReturnError(&error)
        if let error {
            let number = (error[NSAppleScript.errorNumber] as? Int) ?? -1
            fetchLog.error("Notes AppleScript failed: \(number, privacy: .public)")
            throw DownloadFetchError.from(appleScriptError: number)
        }
    }

    enum Automation { case allowed, denied, notAsked, unknown }

    /// The Automation permission for Notes, checked without asking the user.
    static func automationState() -> Automation {
        guard runningNotes() != nil else { return .unknown }
        var target = AEAddressDesc()
        let id = Array(notesBundleID.utf8)
        guard AECreateDesc(DescType(typeApplicationBundleID), id, id.count, &target) == noErr else { return .unknown }
        defer { AEDisposeDesc(&target) }
        let result = AEDeterminePermissionToAutomateTarget(&target, AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
        switch result {
        case noErr: return .allowed
        case OSStatus(errAEEventNotPermitted): return .denied
        case OSStatus(errAEEventWouldRequireUserConsent): return .notAsked
        default: return .unknown
        }
    }
}

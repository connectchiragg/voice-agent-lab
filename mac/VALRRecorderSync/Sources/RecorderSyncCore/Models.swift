import Foundation

/// One audio attachment found in the Notes store. Holds only what the sync needs:
/// no note body text is ever read.
public struct AudioAttachment: Equatable, Sendable {
    /// ZICCLOUDSYNCINGOBJECT.ZIDENTIFIER of the attachment row (also the UUID inside
    /// the AppleScript `content identifier`, "cid:<UUID>@icloud.apple.com").
    public var attachmentID: String
    /// ZICCLOUDSYNCINGOBJECT.ZIDENTIFIER of the media row (the folder name under Media/).
    public var mediaID: String
    public var uti: String
    /// Z_PK of the note, used to build the AppleScript note id (x-coredata://<store>/ICNote/p<pk>).
    public var notePK: Int64
    public var noteTitle: String
    public var noteCreated: Date?
    public var folderTitle: String?
    public var mediaFilename: String?
    /// The media file on disk, when it was found (iCloud may not have downloaded it yet).
    public var fileURL: URL?
    public var durationSeconds: Double?

    public init(attachmentID: String, mediaID: String, uti: String, notePK: Int64, noteTitle: String,
                noteCreated: Date?, folderTitle: String?, mediaFilename: String?, fileURL: URL?,
                durationSeconds: Double? = nil) {
        self.attachmentID = attachmentID
        self.mediaID = mediaID
        self.uti = uti
        self.notePK = notePK
        self.noteTitle = noteTitle
        self.noteCreated = noteCreated
        self.folderTitle = folderTitle
        self.mediaFilename = mediaFilename
        self.fileURL = fileURL
        self.durationSeconds = durationSeconds
    }
}

/// User choices. The upload token is NOT here: it lives in the Keychain.
public struct AppSettings: Codable, Equatable, Sendable {
    public var serverURL: URL
    /// Move the note to Recently Deleted after VALR confirmed the upload. Default off.
    public var deleteNoteAfterUpload: Bool
    /// Also send recordings that were already in Notes when the app was set up. Default on
    /// (VALR de-duplicates, so a re-send is harmless).
    public var includeExistingRecordings: Bool
    /// Also send other m4a audio in Notes that does not look like a call recording. Default off.
    public var includeOtherAudio: Bool
    public var onboardingDone: Bool
    public var setupDate: Date

    public static let defaultServer = URL(string: "https://valr.haciensus.com")!

    public init(serverURL: URL = AppSettings.defaultServer, deleteNoteAfterUpload: Bool = false,
                includeExistingRecordings: Bool = true, includeOtherAudio: Bool = false,
                onboardingDone: Bool = false, setupDate: Date = Date()) {
        self.serverURL = serverURL
        self.deleteNoteAfterUpload = deleteNoteAfterUpload
        self.includeExistingRecordings = includeExistingRecordings
        self.includeOtherAudio = includeOtherAudio
        self.onboardingDone = onboardingDone
        self.setupDate = setupDate
    }
}

public enum Paths {
    /// ~/Library/Application Support/VALR Recorder Sync/
    public static var supportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("VALR Recorder Sync", isDirectory: true)
    }

    /// ~/Library/Group Containers/group.com.apple.notes/
    public static var notesContainer: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.notes", isDirectory: true)
    }
}

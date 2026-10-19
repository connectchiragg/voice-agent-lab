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
    /// Z_PK of the attachment row, used to build the AppleScript attachment id
    /// (x-coredata://<store>/ICAttachment/p<pk>) when Notes must be asked to download the audio.
    public var attachmentPK: Int64?

    public init(attachmentID: String, mediaID: String, uti: String, notePK: Int64, noteTitle: String,
                noteCreated: Date?, folderTitle: String?, mediaFilename: String?, fileURL: URL?,
                durationSeconds: Double? = nil, attachmentPK: Int64? = nil) {
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
        self.attachmentPK = attachmentPK
    }
}

/// User choices. The upload token is NOT here: it lives in TokenStore (Application Support/VALR/upload-token, 0600).
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
    /// A quiet Mac notification (no sound) when VALR received a call recording. Default on.
    public var notifyWhenSent: Bool

    public static let defaultServer = URL(string: "https://valr.haciensus.com")!

    public init(serverURL: URL = AppSettings.defaultServer, deleteNoteAfterUpload: Bool = false,
                includeExistingRecordings: Bool = true, includeOtherAudio: Bool = false,
                onboardingDone: Bool = false, setupDate: Date = Date(), notifyWhenSent: Bool = true) {
        self.serverURL = serverURL
        self.deleteNoteAfterUpload = deleteNoteAfterUpload
        self.includeExistingRecordings = includeExistingRecordings
        self.includeOtherAudio = includeOtherAudio
        self.onboardingDone = onboardingDone
        self.setupDate = setupDate
        self.notifyWhenSent = notifyWhenSent
    }

    private enum CodingKeys: String, CodingKey {
        case serverURL, deleteNoteAfterUpload, includeExistingRecordings, includeOtherAudio, onboardingDone, setupDate, notifyWhenSent
    }

    /// Tolerant decoding: settings saved by an older version (without newer keys) keep everything they had.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AppSettings()
        serverURL = try c.decodeIfPresent(URL.self, forKey: .serverURL) ?? d.serverURL
        deleteNoteAfterUpload = try c.decodeIfPresent(Bool.self, forKey: .deleteNoteAfterUpload) ?? d.deleteNoteAfterUpload
        includeExistingRecordings = try c.decodeIfPresent(Bool.self, forKey: .includeExistingRecordings) ?? d.includeExistingRecordings
        includeOtherAudio = try c.decodeIfPresent(Bool.self, forKey: .includeOtherAudio) ?? d.includeOtherAudio
        onboardingDone = try c.decodeIfPresent(Bool.self, forKey: .onboardingDone) ?? d.onboardingDone
        setupDate = try c.decodeIfPresent(Date.self, forKey: .setupDate) ?? d.setupDate
        notifyWhenSent = try c.decodeIfPresent(Bool.self, forKey: .notifyWhenSent) ?? d.notifyWhenSent
    }
}

public enum Paths {
    static var applicationSupport: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    }

    /// ~/Library/Application Support/VALR/
    public static var supportDirectory: URL {
        applicationSupport.appendingPathComponent("VALR", isDirectory: true)
    }

    /// Where the app kept its state before it was renamed from "VALR Recorder Sync" to "VALR".
    public static var legacySupportDirectory: URL {
        applicationSupport.appendingPathComponent("VALR Recorder Sync", isDirectory: true)
    }

    /// ~/Library/Group Containers/group.com.apple.notes/
    public static var notesContainer: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.notes", isDirectory: true)
    }
}

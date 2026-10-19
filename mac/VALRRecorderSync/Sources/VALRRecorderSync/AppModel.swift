import AppKit
import Foundation
import RecorderSyncCore
import ServiceManagement
import UserNotifications

/// App state shared by the menu, onboarding and settings.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var settings: AppSettings { didSet { saveSettings() } }
    @Published private(set) var hasToken = false
    @Published private(set) var access: NotesStoreReader.AccessState = .denied
    @Published private(set) var isSyncing = false
    @Published private(set) var lastSync: Date?
    @Published private(set) var sentToday = 0
    @Published private(set) var status = "Not set up yet"
    @Published private(set) var problem: SyncReport.Problem?
    /// Call recordings whose audio has not come down from iCloud yet (the menu then offers "Fetch now").
    @Published private(set) var waitingForDownload = 0
    /// Call recordings found but not yet received by VALR.
    @Published private(set) var pending = 0
    /// Whether macOS shows the menu-bar icon (StatusIconWatcher keeps this current).
    @Published var iconVisibility: StatusIconVisibility = .unknown

    let reader = NotesStoreReader()
    // Carries over an install from before the rename ("VALR Recorder Sync") before anything reads state or settings.
    let store: StateStore = { LegacyMigration.run(); return StateStore() }()
    /// The upload token: a 0600 file in Application Support, read once per launch (TokenStore.swift).
    /// `defaults write <bundle id> disableKeychainMigration -bool YES` keeps a test build away from the old Keychain item.
    let tokens = TokenStore(legacy: UserDefaults.standard.bool(forKey: "disableKeychainMigration") ? nil : TokenKeychain())
    private lazy var engine = SyncEngine(source: { [reader] in try reader.read() }, store: store,
                                         deleter: AppleScriptNoteDeleter(), fetcher: NotesDownloadFetcher())
    private var timer: Timer?
    private var watcher: FSEventsWatcher?
    private var debounce: DispatchWorkItem?
    private var accessPoll: Timer?
    private var fetchFollowUp: DispatchWorkItem?
    static let scanInterval: TimeInterval = 120

    private init() {
        let saved = UserDefaults.standard.data(forKey: "settings").flatMap { try? JSONDecoder().decode(AppSettings.self, from: $0) }
        settings = saved ?? AppSettings()
        hasToken = tokens.token(for: settings.serverURL) != nil
        access = reader.accessState()
        lastSync = store.state.lastSync
        sentToday = store.state.sentToday()
    }

    private func saveSettings() {
        if let data = try? JSONEncoder().encode(settings) { UserDefaults.standard.set(data, forKey: "settings") }
        hasToken = tokens.token(for: settings.serverURL) != nil // cached: no disk or Keychain access
    }

    var needsOnboarding: Bool { !settings.onboardingDone || !hasToken }

    // MARK: Token

    func saveToken(_ token: String) throws {
        let clean = token.trimmingCharacters(in: .whitespacesAndNewlines)
        try tokens.save(clean, for: settings.serverURL)
        hasToken = true
    }

    func forgetToken() {
        tokens.delete(for: settings.serverURL)
        hasToken = false
    }

    func checkToken(_ token: String) async -> Uploader.TokenCheck {
        await Uploader(server: settings.serverURL, token: token.trimmingCharacters(in: .whitespacesAndNewlines)).checkToken()
    }

    // MARK: Full Disk Access

    func refreshAccess() { access = reader.accessState() }

    /// Polls while onboarding is open, so the step turns green as soon as access is granted.
    func startAccessPolling() {
        accessPoll?.invalidate()
        accessPoll = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in
            Task { @MainActor in AppModel.shared.refreshAccess() }
        }
    }

    func stopAccessPolling() { accessPoll?.invalidate(); accessPoll = nil }

    func openFullDiskAccessSettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        NSWorkspace.shared.open(url)
    }

    func revealAppInFinder() {
        NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
    }

    // MARK: Launch at login

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                status = "Could not change Launch at login"
            }
            objectWillChange.send()
        }
    }

    // MARK: Sync

    func start() {
        guard !needsOnboarding else { status = "Finish setup to start"; return }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: Self.scanInterval, repeats: true) { _ in
            Task { @MainActor in await AppModel.shared.sync() }
        }
        if watcher == nil {
            watcher = FSEventsWatcher(path: reader.container.path) { _ in
                Task { @MainActor in AppModel.shared.scheduleSoon() }
            }
            watcher?.start()
        }
        requestNotificationPermission()
        Task { await sync() }
    }

    /// Notes writes in bursts; wait a few seconds after the last change.
    func scheduleSoon() {
        debounce?.cancel()
        let item = DispatchWorkItem { Task { @MainActor in await AppModel.shared.sync() } }
        debounce = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: item)
    }

    /// The menu's "Fetch now": ask Notes straight away to download every waiting call recording.
    func fetchNow() {
        Task { await sync(force: true, fetchNow: true) }
    }

    func openAutomationSettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")!
        NSWorkspace.shared.open(url)
    }

    /// A pass when the next download request is due (about 30 s), so a waiting recording does not
    /// sit until the 2-minute timer. FSEvents starts a pass anyway as soon as the audio lands.
    private func scheduleFetchFollowUp(at date: Date?) {
        fetchFollowUp?.cancel()
        guard let date else { return }
        let item = DispatchWorkItem { Task { @MainActor in await AppModel.shared.sync() } }
        fetchFollowUp = item
        DispatchQueue.main.asyncAfter(deadline: .now() + max(1, date.timeIntervalSinceNow) + 1, execute: item)
    }

    func sync(force: Bool = false, fetchNow: Bool = false) async {
        guard !isSyncing, settings.onboardingDone else { return }
        isSyncing = true
        defer { isSyncing = false }
        refreshAccess()
        let token = tokens.token(for: settings.serverURL)
        let uploader = token.map { Uploader(server: settings.serverURL, token: $0) }
        let report = await engine.run(settings: settings, uploader: uploader, force: force, fetchNow: fetchNow)
        lastSync = store.state.lastSync
        sentToday = store.state.sentToday()
        problem = report.problem
        status = describe(report)
        waitingForDownload = report.waitingForDownload
        pending = report.pending
        scheduleFetchFollowUp(at: report.nextDownloadCheck)
        if settings.notifyWhenSent, let title = SentNotice.title(contacts: report.sentContacts) { notify(title: title) }
    }

    private func describe(_ r: SyncReport) -> String {
        switch r.problem {
        case .noAccess: return "Needs Full Disk Access"
        case .notesMissing: return "Notes data not found on this Mac"
        case .noToken: return "Upload token missing"
        case .unauthorized: return "VALR refused the upload token"
        case .offline: return "Can't reach VALR, will retry"
        case .schema: return "Couldn't read Notes (see Settings › Check Notes)"
        case .deleteFailed: return "Sent, but couldn't move a note to Recently Deleted"
        case .notesAutomationDenied:
            return "Can't ask Notes to download \(Self.recordings(r.waitingForDownload)): allow VALR to control Notes in System Settings › Privacy & Security › Automation"
        case nil:
            if r.waitingForDownload > 0, r.downloadGaveUp < r.waitingForDownload {
                return "Asking Notes to download \(Self.recordings(r.waitingForDownload)) from iCloud…"
            }
            if r.waitingForDownload > 0 { return "Waiting for iCloud to download \(Self.recordings(r.waitingForDownload))" }
            if r.pending > 0 { return "\(r.pending) recording(s) still to send" }
            return "Up to date"
        }
    }

    static func recordings(_ n: Int) -> String { n == 1 ? "1 recording" : "\(n) recordings" }

    /// One line for the window: is VALR connected?
    var connectionText: String {
        if !hasToken { return "Not connected (no upload token)" }
        switch problem {
        case .unauthorized: return "VALR refused the upload token"
        case .offline: return "Can't reach VALR, will retry"
        default: return "Connected to \(settings.serverURL.host ?? settings.serverURL.absoluteString)"
        }
    }

    var fullDiskAccessText: String {
        switch access {
        case .granted: return "Granted"
        case .notesMissing: return "No Notes data found"
        case .denied: return "Not granted"
        }
    }

    var automationText: String {
        switch NotesDownloadFetcher.automationState() {
        case .allowed: return "Allowed"
        case .denied: return "Not allowed"
        case .notAsked: return "Not asked yet"
        case .unknown: return "Unknown (Notes is not open)"
        }
    }

    func diagnose() -> String {
        do { return SyncEngine.diagnose(try reader.read()) } catch { return "\(error)" }
    }

    // MARK: Notifications

    private var canNotify: Bool { Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app" }

    private func requestNotificationPermission() {
        guard canNotify else { return }
        // Banners only: VALR's notifications are quiet (no sound).
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
    }

    private func notify(title: String) {
        guard canNotify else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = SentNotice.body
        content.sound = nil
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    func openVALR() {
        NSWorkspace.shared.open(settings.serverURL)
    }
}

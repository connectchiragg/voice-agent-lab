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

    let reader = NotesStoreReader()
    let store = StateStore()
    let keychain = TokenKeychain()
    private lazy var engine = SyncEngine(source: { [reader] in try reader.read() }, store: store,
                                         deleter: AppleScriptNoteDeleter())
    private var timer: Timer?
    private var watcher: FSEventsWatcher?
    private var debounce: DispatchWorkItem?
    private var accessPoll: Timer?
    static let scanInterval: TimeInterval = 120

    private init() {
        let saved = UserDefaults.standard.data(forKey: "settings").flatMap { try? JSONDecoder().decode(AppSettings.self, from: $0) }
        settings = saved ?? AppSettings()
        hasToken = keychain.load(for: settings.serverURL) != nil
        access = reader.accessState()
        lastSync = store.state.lastSync
        sentToday = store.state.sentToday()
    }

    private func saveSettings() {
        if let data = try? JSONEncoder().encode(settings) { UserDefaults.standard.set(data, forKey: "settings") }
        hasToken = keychain.load(for: settings.serverURL) != nil
    }

    var needsOnboarding: Bool { !settings.onboardingDone || !hasToken }

    // MARK: Token

    func saveToken(_ token: String) throws {
        let clean = token.trimmingCharacters(in: .whitespacesAndNewlines)
        try keychain.save(clean, for: settings.serverURL)
        hasToken = true
    }

    func forgetToken() {
        keychain.delete(for: settings.serverURL)
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

    func sync(force: Bool = false) async {
        guard !isSyncing, settings.onboardingDone else { return }
        isSyncing = true
        defer { isSyncing = false }
        refreshAccess()
        let token = keychain.load(for: settings.serverURL)
        let uploader = token.map { Uploader(server: settings.serverURL, token: $0) }
        let report = await engine.run(settings: settings, uploader: uploader, force: force)
        lastSync = store.state.lastSync
        sentToday = store.state.sentToday()
        problem = report.problem
        status = describe(report)
        if report.sent > 0 { notify(sent: report.sent) }
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
        case nil:
            if r.waitingForDownload > 0 { return "Waiting for \(r.waitingForDownload) recording(s) to download from iCloud" }
            if r.pending > 0 { return "\(r.pending) recording(s) still to send" }
            return "Up to date"
        }
    }

    func diagnose() -> String {
        do { return SyncEngine.diagnose(try reader.read()) } catch { return "\(error)" }
    }

    // MARK: Notifications

    private var canNotify: Bool { Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app" }

    private func requestNotificationPermission() {
        guard canNotify else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func notify(sent: Int) {
        guard canNotify else { return }
        let content = UNMutableNotificationContent()
        content.title = sent == 1 ? "Sent 1 call recording to VALR" : "Sent \(sent) call recordings to VALR"
        content.body = "VALR will transcribe it and add it to Past interviews."
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    func openVALR() {
        NSWorkspace.shared.open(settings.serverURL)
    }
}

import AppKit
import Carbon.HIToolbox
import RecorderSyncCore
import SwiftUI

/// Keeps VALR reachable: one copy at a time, a window when it is opened, and a one-time hint when
/// macOS hides the menu-bar icon (a full menu bar, or the notch). The decisions live in
/// RecorderSyncCore/AppPresence.swift (unit-tested); this file feeds them AppKit facts.

enum AppInfo {
    static var version: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0" }
    static var bundleID: String { Bundle.main.bundleIdentifier ?? "com.haciensus.valr.mac" }
    static let pidKey = "pid"
    static let versionKey = "version"

    /// True when macOS opened VALR as a login item (then no window opens by itself).
    static func launchedAtLogin() -> Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              event.eventClass == AEEventClass(kCoreEventClass), event.eventID == AEEventID(kAEOpenApplication)
        else { return false }
        return event.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue == OSType(keyAELaunchedAsLogInItem)
    }
}

/// "Show your window" and its answer, between copies of VALR (distributed notifications, this Mac only).
@MainActor
enum InstanceChannel {
    static var show: Notification.Name { .init(AppInfo.bundleID + ".showWindow") }
    static var shown: Notification.Name { .init(AppInfo.bundleID + ".windowShown") }
    private static var me: Int32 { ProcessInfo.processInfo.processIdentifier }
    private static var listening = false

    /// Called early, so a copy that is still starting can already answer.
    static func listen() {
        guard !listening else { return }
        listening = true
        DistributedNotificationCenter.default().addObserver(forName: show, object: nil, queue: .main) { note in
            let from = (note.userInfo?[AppInfo.pidKey] as? NSNumber)?.int32Value
            guard from != me else { return }
            MainActor.assumeIsolated {
                Windows.shared.showMainOrOnboarding()
                DistributedNotificationCenter.default().postNotificationName(
                    shown, object: nil, userInfo: [AppInfo.pidKey: NSNumber(value: me), AppInfo.versionKey: AppInfo.version],
                    deliverImmediately: true)
            }
        }
    }

    /// Asks the copy `pid` to show its window. True when it answered (copies before 1.0.3 never answer).
    static func askToShow(pid: Int32, timeout: TimeInterval = 1.5) async -> Bool {
        await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            var token: NSObjectProtocol?
            var done = false
            let finish: @MainActor (Bool) -> Void = { ok in
                guard !done else { return }
                done = true
                if let token { DistributedNotificationCenter.default().removeObserver(token) }
                cont.resume(returning: ok)
            }
            token = DistributedNotificationCenter.default().addObserver(forName: shown, object: nil, queue: .main) { note in
                guard (note.userInfo?[AppInfo.pidKey] as? NSNumber)?.int32Value == pid else { return }
                MainActor.assumeIsolated { finish(true) }
            }
            DistributedNotificationCenter.default().postNotificationName(
                show, object: nil, userInfo: [AppInfo.pidKey: NSNumber(value: me)], deliverImmediately: true)
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { MainActor.assumeIsolated { finish(false) } }
        }
    }
}

@MainActor
enum SingleInstanceGuard {
    /// Returns false when this copy should quit because another copy is in charge (and now shows its window).
    static func resolve() async -> Bool {
        let current = NSRunningApplication.current
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: AppInfo.bundleID)
            .filter { $0.processIdentifier != current.processIdentifier && !$0.isTerminated }
        let copies = others.map { app in
            RunningCopy(pid: app.processIdentifier,
                        version: app.bundleURL.flatMap { Bundle(url: $0)?.infoDictionary?["CFBundleShortVersionString"] as? String },
                        launchDate: app.launchDate)
        }
        switch SingleInstance.decide(myVersion: AppInfo.version, myPID: current.processIdentifier,
                                     myLaunch: current.launchDate, others: copies) {
        case .run:
            return true
        case .handOff(let pid):
            if await InstanceChannel.askToShow(pid: pid) {
                NSLog("VALR: another copy (pid %d) is running and showed its window; quitting this one", pid)
                return false
            }
            // No answer: an older VALR (before 1.0.3; its bundle on disk may already be the new version).
            NSLog("VALR: copy pid %d did not answer; replacing it", pid)
            await quit(others.filter { $0.processIdentifier == pid })
            return true
        case .replace(let pids):
            NSLog("VALR: replacing older copies %@", pids.map(String.init).joined(separator: ","))
            await quit(others.filter { pids.contains($0.processIdentifier) })
            return true
        }
    }

    /// Asks each app to quit, waits up to 5 s, then force-quits what is left (only older VALR copies get here).
    private static func quit(_ apps: [NSRunningApplication]) async {
        apps.forEach { $0.terminate() }
        for _ in 0..<20 where apps.contains(where: { !$0.isTerminated }) {
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        for app in apps where !app.isTerminated { app.forceTerminate() }
    }
}

/// Watches whether macOS shows VALR's menu-bar icon and, once ever, opens the window with a hint.
@MainActor
final class StatusIconWatcher {
    static let shared = StatusIconWatcher()
    private static let hintShownKey = "hiddenIconHintShown"
    private var observers: [NSObjectProtocol] = []
    private var timer: Timer?

    static var hintAlreadyShown: Bool {
        get { UserDefaults.standard.bool(forKey: hintShownKey) }
        set { UserDefaults.standard.set(newValue, forKey: hintShownKey) }
    }

    func start() {
        guard observers.isEmpty else { return }
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { StatusIconWatcher.shared.refresh() }
        })
        observers.append(nc.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: nil, queue: .main) { note in
            guard let w = note.object as? NSWindow, Self.isStatusWindow(w) else { return }
            MainActor.assumeIsolated { StatusIconWatcher.shared.refresh() }
        })
        // Let the menu bar settle after launch, then judge twice before showing the hint.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { MainActor.assumeIsolated { StatusIconWatcher.shared.refresh() } }
        DispatchQueue.main.asyncAfter(deadline: .now() + 9) {
            MainActor.assumeIsolated { StatusIconWatcher.shared.refresh(mayShowHint: true) }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in
            MainActor.assumeIsolated { StatusIconWatcher.shared.refresh(mayShowHint: true) }
        }
    }

    private var lastVerdict: StatusIconVisibility = .unknown

    func refresh(mayShowHint: Bool = false) {
        let verdict = assess()
        let confirmed = verdict == .hidden && lastVerdict == .hidden
        lastVerdict = verdict
        AppModel.shared.iconVisibility = verdict
        if mayShowHint, confirmed, LaunchWindowPolicy.shouldShowHiddenIconHint(verdict, alreadyShown: Self.hintAlreadyShown) {
            NSLog("VALR: menu-bar icon is hidden by macOS; showing the window once")
            Self.hintAlreadyShown = true
            Windows.shared.showMainOrOnboarding()
        }
    }

    static func isStatusWindow(_ w: NSWindow) -> Bool {
        String(describing: type(of: w)).contains("StatusBarWindow")
    }

    func assess() -> StatusIconVisibility {
        guard let window = NSApp.windows.first(where: Self.isStatusWindow) else {
            return StatusIconVisibility.assess(itemFrame: nil, windowVisible: false, screenFrame: nil,
                                               notchRightArea: nil, menuBarShown: NSMenu.menuBarVisible())
        }
        let screen = window.screen ?? NSScreen.screens.first { $0.frame.intersects(window.frame) } ?? NSScreen.main
        var right = screen?.auxiliaryTopRightArea
        // The auxiliary areas may be screen-local; put them in global coordinates like the window frame.
        if let s = screen?.frame, let r = right, s.origin != .zero, !s.contains(CGPoint(x: r.midX, y: r.midY)) {
            right = r.offsetBy(dx: s.minX, dy: s.minY)
        }
        let visible = window.isVisible && window.occlusionState.contains(.visible)
        let verdict = StatusIconVisibility.assess(itemFrame: window.frame, windowVisible: visible, screenFrame: screen?.frame,
                                                  notchRightArea: right, menuBarShown: NSMenu.menuBarVisible())
        NSLog("VALR: status item frame %@ visible=%d screen %@ rightOfNotch %@ -> %@",
              NSStringFromRect(window.frame), visible ? 1 : 0, NSStringFromRect(screen?.frame ?? .zero),
              right.map { NSStringFromRect($0) } ?? "none", "\(verdict)")
        return verdict
    }
}

/// The "VALR for Mac" window: what VALR is doing, and the menu's actions, for when the icon is hidden.
struct MainWindowView: View {
    @ObservedObject var model: AppModel
    @State private var automation = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                Text("VALR for Mac").font(.title2.bold())
                Spacer()
                Text("Version \(AppInfo.version)").font(.caption).foregroundStyle(.secondary)
            }
            if model.iconVisibility == .hidden {
                Label(LaunchWindowPolicy.hiddenIconHint, systemImage: "menubar.rectangle")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
                row("Status", model.status)
                row("VALR", model.connectionText)
                row("Last sync", model.lastSync.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "Never")
                row("Sent today", "\(model.sentToday)")
                row("Still to send", "\(model.pending)")
                row("Waiting for iCloud", model.waitingForDownload == 0 ? "None" : AppModel.recordings(model.waitingForDownload))
                row("Full Disk Access", model.fullDiskAccessText)
                row("Notes automation", automation)
            }
            if model.needsOnboarding {
                Button("Finish setup…") { Windows.shared.showOnboarding() }
            }
            HStack {
                Button(model.isSyncing ? "Syncing…" : "Sync now") { Task { await model.sync(force: true) } }
                    .disabled(model.isSyncing || model.needsOnboarding)
                Button("Fetch now") { model.fetchNow() }
                    .disabled(model.isSyncing || model.waitingForDownload == 0)
                Spacer()
                Button("Settings…") { Windows.shared.showSettings() }
                Button("Quit VALR") { NSApp.terminate(nil) }
            }
        }
        .padding(24)
        .frame(width: 480)
        .onAppear {
            model.refreshAccess()
            automation = model.automationText
            if model.iconVisibility == .hidden { StatusIconWatcher.hintAlreadyShown = true }
        }
    }

    @ViewBuilder
    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            Text(value).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
    }
}

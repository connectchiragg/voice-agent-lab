import CoreGraphics
import Foundation

/// Decisions that keep VALR reachable: one copy running, a window when it is opened, and a hint
/// when macOS hides the menu-bar icon. Pure logic, so it is unit-tested; the app feeds it AppKit facts.

/// "1.0.10" > "1.0.9". Missing parts count as 0; non-numeric parts as 0.
public enum AppVersion {
    public static func compare(_ a: String, _ b: String) -> ComparisonResult {
        let pa = parts(a), pb = parts(b)
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i] : 0, y = i < pb.count ? pb[i] : 0
            if x != y { return x < y ? .orderedAscending : .orderedDescending }
        }
        return .orderedSame
    }

    private static func parts(_ v: String) -> [Int] {
        v.split(separator: ".").map { Int($0.prefix { $0.isNumber }) ?? 0 }
    }
}

/// Another process with VALR's bundle ID.
public struct RunningCopy: Equatable, Sendable {
    public var pid: Int32
    /// CFBundleShortVersionString of the copy's bundle; nil when it cannot be read.
    public var version: String?
    public var launchDate: Date?

    public init(pid: Int32, version: String?, launchDate: Date?) {
        self.pid = pid
        self.version = version
        self.launchDate = launchDate
    }
}

public enum SingleInstance {
    public enum Decision: Equatable, Sendable {
        /// No other copy: carry on.
        case run
        /// Only older copies are running: ask them to quit, then carry on.
        case replace([Int32])
        /// A copy of the same or a newer version is already running: show its window and quit this one.
        case handOff(to: Int32)
    }

    /// A copy whose version cannot be read is treated as the same version (never killed by guesswork).
    /// Two copies of the same version: the one launched first stays, so a double launch never quits both.
    public static func decide(myVersion: String, myPID: Int32, myLaunch: Date?, others: [RunningCopy]) -> Decision {
        let others = others.filter { $0.pid != myPID }
        guard !others.isEmpty else { return .run }
        func beatsMe(_ o: RunningCopy) -> Bool {
            switch AppVersion.compare(o.version ?? myVersion, myVersion) {
            case .orderedDescending: return true
            case .orderedAscending: return false
            case .orderedSame:
                let theirs = o.launchDate ?? .distantPast, mine = myLaunch ?? .distantFuture
                return theirs != mine ? theirs < mine : o.pid < myPID
            }
        }
        let winners = others.filter(beatsMe)
        if let best = winners.max(by: { AppVersion.compare($0.version ?? myVersion, $1.version ?? myVersion) == .orderedAscending }) {
            return .handOff(to: best.pid)
        }
        let older = others.filter { AppVersion.compare($0.version ?? myVersion, myVersion) == .orderedAscending }
        return older.isEmpty ? .run : .replace(older.map(\.pid))
    }
}

/// Whether macOS is showing the menu-bar icon. On a crowded menu bar macOS silently hides status
/// items that do not fit; on a notched MacBook items that would sit under or left of the notch are hidden.
public enum StatusIconVisibility: Equatable, Sendable {
    case visible, hidden, unknown

    /// - itemFrame: the status item's button window frame (global coordinates), nil if not found.
    /// - windowVisible: the window is on screen and its occlusion state includes `.visible`.
    /// - screenFrame: the frame of the screen the menu bar is on.
    /// - notchRightArea: `NSScreen.auxiliaryTopRightArea` (the menu bar right of the notch), nil without a notch.
    /// - menuBarShown: false while the menu bar is auto-hidden or in full screen (then nothing can be judged).
    public static func assess(itemFrame: CGRect?, windowVisible: Bool, screenFrame: CGRect?,
                              notchRightArea: CGRect?, menuBarShown: Bool) -> StatusIconVisibility {
        guard menuBarShown, let item = itemFrame, let screen = screenFrame else { return .unknown }
        if item.isEmpty || item.width < 1 { return .hidden }
        // Mostly off the screen: macOS parks hidden items outside the visible menu bar.
        let onScreen = item.intersection(screen)
        if onScreen.isNull || onScreen.width < item.width / 2 { return .hidden }
        if let right = notchRightArea, !right.isEmpty, item.minX < right.minX - 0.5 { return .hidden }
        return windowVisible ? .visible : .hidden
    }
}

/// When the VALR window should open by itself.
public enum LaunchWindowPolicy {
    public enum Window: Equatable, Sendable { case none, onboarding, main }

    /// Opening VALR from Finder, Spotlight or Launchpad shows a window (a menu-bar-only app otherwise
    /// seems to do nothing). Opening at login stays quiet.
    public static func onLaunch(needsOnboarding: Bool, launchedAtLogin: Bool) -> Window {
        if needsOnboarding { return .onboarding }
        return launchedAtLogin ? .none : .main
    }

    /// Show the window with the "icon hidden" hint at most once, and only when the icon is surely hidden.
    public static func shouldShowHiddenIconHint(_ visibility: StatusIconVisibility, alreadyShown: Bool) -> Bool {
        visibility == .hidden && !alreadyShown
    }

    public static let hiddenIconHint = "Your menu bar is full, so macOS hid VALR's icon. Open VALR from Spotlight anytime."
}

/// The quiet notification after an upload: "Call with Jane Doe sent to VALR".
public enum SentNotice {
    /// - contacts: one entry per recording VALR received in this pass (nil when no contact name is known).
    public static func title(contacts: [String?]) -> String? {
        guard !contacts.isEmpty else { return nil }
        if contacts.count == 1 {
            if let name = contacts[0]?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty {
                return "Call with \(name) sent to VALR"
            }
            return "Call recording sent to VALR"
        }
        return "\(contacts.count) call recordings sent to VALR"
    }

    public static let body = "VALR will transcribe it and add it to Past interviews."
}

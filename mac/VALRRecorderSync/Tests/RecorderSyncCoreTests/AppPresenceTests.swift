import CoreGraphics
import Foundation
import Testing
@testable import RecorderSyncCore

@Suite("Keeping VALR reachable")
struct AppPresenceTests {
    let t0 = Date(timeIntervalSince1970: 1_000)

    @Test func comparesVersions() {
        #expect(AppVersion.compare("1.0.3", "1.0.2") == .orderedDescending)
        #expect(AppVersion.compare("1.0.9", "1.0.10") == .orderedAscending)
        #expect(AppVersion.compare("1.0", "1.0.0") == .orderedSame)
        #expect(AppVersion.compare("2", "1.9.9") == .orderedDescending)
    }

    @Test func runsAloneWhenNothingElseIsRunning() {
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0, others: []) == .run)
        // Its own entry is ignored.
        let me = RunningCopy(pid: 10, version: "1.0.3", launchDate: t0)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0, others: [me]) == .run)
    }

    @Test func replacesOlderCopies() {
        let old = [RunningCopy(pid: 1, version: "1.0.1", launchDate: t0), RunningCopy(pid: 2, version: "1.0.2", launchDate: t0)]
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0 + 60, others: old) == .replace([1, 2]))
    }

    @Test func handsOffToASameOrNewerCopy() {
        let same = RunningCopy(pid: 1, version: "1.0.3", launchDate: t0)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0 + 5, others: [same]) == .handOff(to: 1))
        let newer = RunningCopy(pid: 2, version: "1.0.4", launchDate: t0 + 99)
        let older = RunningCopy(pid: 3, version: "1.0.1", launchDate: t0)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0 + 5, others: [older, newer]) == .handOff(to: 2))
    }

    @Test func twoSameVersionLaunchesKeepExactlyOne() {
        let a = RunningCopy(pid: 1, version: "1.0.3", launchDate: t0)
        let b = RunningCopy(pid: 2, version: "1.0.3", launchDate: t0 + 1)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 1, myLaunch: t0, others: [b]) == .run)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 2, myLaunch: t0 + 1, others: [a]) == .handOff(to: 1))
        // Same launch time: the lower pid stays.
        let c = RunningCopy(pid: 3, version: "1.0.3", launchDate: t0)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 1, myLaunch: t0, others: [c]) == .run)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 3, myLaunch: t0, others: [a]) == .handOff(to: 1))
    }

    @Test func neverKillsACopyWhoseVersionIsUnknown() {
        let unknown = RunningCopy(pid: 1, version: nil, launchDate: t0)
        #expect(SingleInstance.decide(myVersion: "1.0.3", myPID: 10, myLaunch: t0 + 5, others: [unknown]) == .handOff(to: 1))
    }

    // Chirag's MacBook: 1470 x 956 pt, notch between x ≈ 652 and ≈ 818.
    let screen = CGRect(x: 0, y: 0, width: 1470, height: 956)
    let rightOfNotch = CGRect(x: 818, y: 924, width: 652, height: 32)

    @Test func iconLeftOfTheNotchIsHidden() {
        for x in [582.0, 614.0, 700.0] {
            let item = CGRect(x: x, y: 924, width: 30, height: 24)
            #expect(StatusIconVisibility.assess(itemFrame: item, windowVisible: true, screenFrame: screen,
                                                notchRightArea: rightOfNotch, menuBarShown: true) == .hidden)
        }
    }

    @Test func iconRightOfTheNotchIsVisible() {
        let item = CGRect(x: 1100, y: 924, width: 30, height: 24)
        #expect(StatusIconVisibility.assess(itemFrame: item, windowVisible: true, screenFrame: screen,
                                            notchRightArea: rightOfNotch, menuBarShown: true) == .visible)
        // ...unless macOS reports it occluded.
        #expect(StatusIconVisibility.assess(itemFrame: item, windowVisible: false, screenFrame: screen,
                                            notchRightArea: rightOfNotch, menuBarShown: true) == .hidden)
    }

    @Test func noNotchScreensUseOcclusionAndPosition() {
        let wide = CGRect(x: 0, y: 0, width: 1920, height: 1080)
        let item = CGRect(x: 1500, y: 1056, width: 30, height: 24)
        #expect(StatusIconVisibility.assess(itemFrame: item, windowVisible: true, screenFrame: wide,
                                            notchRightArea: nil, menuBarShown: true) == .visible)
        let parked = CGRect(x: -500, y: 1056, width: 30, height: 24)
        #expect(StatusIconVisibility.assess(itemFrame: parked, windowVisible: true, screenFrame: wide,
                                            notchRightArea: nil, menuBarShown: true) == .hidden)
    }

    @Test func cannotJudgeWithoutAMenuBarOrAWindow() {
        let item = CGRect(x: 582, y: 924, width: 30, height: 24)
        #expect(StatusIconVisibility.assess(itemFrame: item, windowVisible: false, screenFrame: screen,
                                            notchRightArea: rightOfNotch, menuBarShown: false) == .unknown)
        #expect(StatusIconVisibility.assess(itemFrame: nil, windowVisible: false, screenFrame: screen,
                                            notchRightArea: rightOfNotch, menuBarShown: true) == .unknown)
    }

    @Test func opensAWindowWhenTheUserOpensVALR() {
        #expect(LaunchWindowPolicy.onLaunch(needsOnboarding: true, launchedAtLogin: true) == .onboarding)
        #expect(LaunchWindowPolicy.onLaunch(needsOnboarding: false, launchedAtLogin: false) == .main)
        #expect(LaunchWindowPolicy.onLaunch(needsOnboarding: false, launchedAtLogin: true) == .none)
    }

    @Test func hiddenIconHintShowsOnce() {
        #expect(LaunchWindowPolicy.shouldShowHiddenIconHint(.hidden, alreadyShown: false))
        #expect(!LaunchWindowPolicy.shouldShowHiddenIconHint(.hidden, alreadyShown: true))
        #expect(!LaunchWindowPolicy.shouldShowHiddenIconHint(.unknown, alreadyShown: false))
        #expect(!LaunchWindowPolicy.shouldShowHiddenIconHint(.visible, alreadyShown: false))
    }

    @Test func sentNoticeNamesTheCall() {
        #expect(SentNotice.title(contacts: []) == nil)
        #expect(SentNotice.title(contacts: ["Jane Doe"]) == "Call with Jane Doe sent to VALR")
        #expect(SentNotice.title(contacts: [nil]) == "Call recording sent to VALR")
        #expect(SentNotice.title(contacts: ["Jane Doe", nil]) == "2 call recordings sent to VALR")
    }

    @Test func settingsSavedByAnOlderVersionKeepTheirValues() throws {
        // 1.0.2 saved settings without notifyWhenSent; decoding must not reset onboarding.
        var old = AppSettings(deleteNoteAfterUpload: true, onboardingDone: true)
        old.notifyWhenSent = false
        var json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(old)) as! [String: Any]
        json.removeValue(forKey: "notifyWhenSent")
        let decoded = try JSONDecoder().decode(AppSettings.self, from: JSONSerialization.data(withJSONObject: json))
        #expect(decoded.onboardingDone && decoded.deleteNoteAfterUpload && decoded.notifyWhenSent)
        #expect(decoded.serverURL == AppSettings.defaultServer)
    }
}

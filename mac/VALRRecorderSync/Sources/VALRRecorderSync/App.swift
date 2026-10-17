import AppKit
import RecorderSyncCore
import SwiftUI

@main
struct VALRRecorderSyncApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @ObservedObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            if model.problem == nil, let mark = MenuBarIcon.mark {
                Image(nsImage: mark).accessibilityLabel("VALR")
            } else {
                Image(systemName: model.problem == nil ? "waveform.circle" : "exclamationmark.circle")
                    .accessibilityLabel("VALR")
            }
        }
    }
}

/// VALR's mark, the Signal V, as a monochrome template image (Resources/MenuBarIconTemplate.png
/// and @2x, 18 pt; scripts/make_icons.py --mac), so macOS tints it for light and dark menu bars.
enum MenuBarIcon {
    static let mark: NSImage? = {
        guard let image = NSImage(named: "MenuBarIconTemplate") else { return nil }
        image.isTemplate = true
        image.size = NSSize(width: 18, height: 18)
        return image
    }()
}

/// The lockup (the Signal V and the "valr" wordmark) at the top of VALR's windows:
/// Resources/Lockup-light.png and Lockup-dark.png, rendered from static/icons/valr-lockup-*.svg.
struct BrandHeader: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HStack {
            if let image = NSImage(named: scheme == .dark ? "Lockup-dark" : "Lockup-light") {
                Image(nsImage: image).resizable().interpolation(.high).aspectRatio(contentMode: .fit).frame(height: 24)
            } else {
                Text("valr").font(.system(size: 22, design: .serif))
            }
            Spacer()
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("VALR")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory) // no Dock icon (LSUIElement also set in Info.plist)
        Task { @MainActor in
            if AppModel.shared.needsOnboarding {
                Windows.shared.showOnboarding()
            } else {
                AppModel.shared.start()
            }
        }
    }
}

struct MenuContent: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Text(model.status)
        if let last = model.lastSync {
            Text("Last sync: \(last.formatted(date: .omitted, time: .shortened))")
        } else {
            Text("Last sync: never")
        }
        Text("Sent today: \(model.sentToday)")
        Divider()
        Button(model.isSyncing ? "Syncing…" : "Sync now") { Task { await model.sync(force: true) } }
            .disabled(model.isSyncing || model.needsOnboarding)
            .keyboardShortcut("s")
        Button("Open VALR") { model.openVALR() }
        Divider()
        if model.needsOnboarding {
            Button("Finish setup…") { Windows.shared.showOnboarding() }
        }
        Button("Settings…") { Windows.shared.showSettings() }
            .keyboardShortcut(",")
        Button("Quit VALR") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

/// Plain AppKit windows hosting SwiftUI views: reliable in a menu-bar-only app.
@MainActor
final class Windows {
    static let shared = Windows()
    private var onboarding: NSWindow?
    private var settings: NSWindow?

    func showOnboarding() {
        if onboarding == nil {
            onboarding = make(title: "Set up VALR", view: OnboardingView(model: AppModel.shared) { [weak self] in
                self?.onboarding?.close()
                self?.onboarding = nil
                AppModel.shared.start()
            })
        }
        present(onboarding)
    }

    func showSettings() {
        if settings == nil {
            settings = make(title: "VALR Settings", view: SettingsView(model: AppModel.shared))
            settings?.isReleasedWhenClosed = false
        }
        present(settings)
    }

    private func make<V: View>(title: String, view: V) -> NSWindow {
        let framed = VStack(alignment: .leading, spacing: 0) {
            BrandHeader().padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 2)
            view
        }
        let window = NSWindow(contentViewController: NSHostingController(rootView: framed))
        window.title = title
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        window.center()
        return window
    }

    private func present(_ window: NSWindow?) {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}

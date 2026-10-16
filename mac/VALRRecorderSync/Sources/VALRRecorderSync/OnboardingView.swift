import RecorderSyncCore
import SwiftUI

/// First run: 1 address, 2 token (or pairing code), 3 Full Disk Access, 4 options.
struct OnboardingView: View {
    @ObservedObject var model: AppModel
    var onDone: () -> Void

    @State private var address = ""
    @State private var token = ""
    @State private var pairingCode = ""
    @State private var usePairing = false
    @State private var tokenMessage = ""
    @State private var tokenOK = false
    @State private var busy = false
    @State private var launchAtLogin = true

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Send iPhone call recordings to VALR").font(.title2.bold())
            Text("Your iPhone saves call recordings in Notes, and iCloud brings them to this Mac. This app sends each new one to VALR. Only the audio and a few hints (the note's title, the contact name and the time) are sent, never other notes.")
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(.secondary)

            step(1, "VALR address", done: URL(string: address)?.scheme != nil) {
                TextField("https://valr.haciensus.com", text: $address).textFieldStyle(.roundedBorder)
            }

            step(2, "Upload token", done: tokenOK) {
                if usePairing {
                    HStack {
                        TextField("Pairing code from VALR", text: $pairingCode).textFieldStyle(.roundedBorder)
                        Button("Connect") { Task { await pair() } }.disabled(pairingCode.isEmpty || busy)
                    }
                } else {
                    Text("In VALR open Settings › Phone call recordings › Show upload token, copy it, and paste it here. It is kept in your macOS Keychain.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        SecureField("Upload token", text: $token).textFieldStyle(.roundedBorder)
                        Button("Check & save") { Task { await saveToken() } }.disabled(token.isEmpty || busy)
                    }
                }
                Toggle("I have a pairing code instead", isOn: $usePairing).font(.callout)
                if !tokenMessage.isEmpty { Text(tokenMessage).font(.callout).foregroundStyle(tokenOK ? .green : .red) }
            }

            step(3, "Full Disk Access", done: model.access == .granted) {
                Text("Call recordings live inside the Notes data, which macOS protects. Open System Settings › Privacy & Security › Full Disk Access, click +, and add VALR Recorder Sync (or switch it on). This step turns green by itself.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Open Full Disk Access settings") { model.openFullDiskAccessSettings() }
                    Button("Show the app in Finder") { model.revealAppInFinder() }
                }
                switch model.access {
                case .granted: Text("Access granted.").foregroundStyle(.green).font(.callout)
                case .notesMissing: Text("Access looks fine, but no Notes data was found. Open Notes once and turn on iCloud › Notes.").font(.callout).foregroundStyle(.orange)
                case .denied: Text("Not granted yet.").foregroundStyle(.secondary).font(.callout)
                }
            }

            step(4, "Options", done: true) {
                Toggle("Move the note to Recently Deleted after VALR has it", isOn: $model.settings.deleteNoteAfterUpload)
                Text("Off by default. Notes keeps deleted notes for 30 days. macOS will ask once to let this app control Notes.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("Also send recordings already in Notes", isOn: $model.settings.includeExistingRecordings)
                Toggle("Open at login", isOn: $launchAtLogin)
            }

            HStack {
                Spacer()
                Button("Start syncing") { finish() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!tokenOK || model.access == .denied)
            }
        }
        .padding(24)
        .frame(width: 560)
        .onAppear {
            address = model.settings.serverURL.absoluteString
            tokenOK = model.hasToken
            if tokenOK { tokenMessage = "A token is already saved." }
            model.startAccessPolling()
        }
        .onDisappear { model.stopAccessPolling() }
    }

    @ViewBuilder
    private func step<Content: View>(_ n: Int, _ title: String, done: Bool, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: done ? "checkmark.circle.fill" : "\(n).circle").foregroundStyle(done ? .green : .secondary)
                Text(title).font(.headline)
            }
            content()
        }
    }

    private func applyAddress() -> Bool {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: trimmed), let scheme = url.scheme, ["https", "http"].contains(scheme), url.host != nil else {
            tokenMessage = "The address should look like https://valr.example.com"
            return false
        }
        if scheme == "http", !["localhost", "127.0.0.1"].contains(url.host ?? "") {
            tokenMessage = "Use an https address."
            return false
        }
        model.settings.serverURL = url
        return true
    }

    private func saveToken() async {
        guard applyAddress() else { return }
        busy = true; defer { busy = false }
        switch await model.checkToken(token) {
        case .valid:
            do { try model.saveToken(token); token = ""; tokenOK = true; tokenMessage = "Token works and is saved in the Keychain." }
            catch { tokenOK = false; tokenMessage = "Could not save to the Keychain." }
        case .invalid: tokenOK = false; tokenMessage = "VALR did not accept this token. Copy it again, or rotate it in VALR."
        case .notConfigured: tokenOK = false; tokenMessage = "This VALR has recording uploads turned off."
        case .unreachable(let why): tokenOK = false; tokenMessage = "Could not reach VALR (\(why)). Check the address."
        }
    }

    private func pair() async {
        guard applyAddress() else { return }
        busy = true; defer { busy = false }
        do {
            let paired = try await PairingClient(server: model.settings.serverURL)
                .exchange(code: pairingCode, deviceName: "VALR Recorder Sync on \(Host.current().localizedName ?? "Mac")")
            try model.saveToken(paired.token)
            tokenOK = true
            tokenMessage = "Connected."
        } catch let error as PairingClient.PairingError {
            tokenOK = false; tokenMessage = error.description
        } catch {
            tokenOK = false; tokenMessage = "Could not save to the Keychain."
        }
    }

    private func finish() {
        model.settings.setupDate = Date()
        model.settings.onboardingDone = true
        if model.launchAtLogin != launchAtLogin { model.launchAtLogin = launchAtLogin }
        onDone()
    }
}

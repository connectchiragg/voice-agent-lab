import AppKit
import RecorderSyncCore
import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @State private var newToken = ""
    @State private var message = ""
    @State private var diagnostics = ""

    var body: some View {
        Form {
            Section("VALR") {
                LabeledContent("Address", value: model.settings.serverURL.absoluteString)
                LabeledContent("Upload token", value: model.hasToken ? "Saved in Keychain" : "Missing")
                HStack {
                    SecureField("Paste a new token", text: $newToken)
                    Button("Check & save") { Task { await replaceToken() } }.disabled(newToken.isEmpty)
                }
                if !message.isEmpty { Text(message).font(.callout) }
                Button("Run setup again…") { Windows.shared.showOnboarding() }
            }
            Section("Sync") {
                Toggle("Move the note to Recently Deleted after VALR has it", isOn: $model.settings.deleteNoteAfterUpload)
                Toggle("Also send recordings already in Notes", isOn: $model.settings.includeExistingRecordings)
                Toggle("Also send other audio in Notes (not just calls)", isOn: $model.settings.includeOtherAudio)
                Toggle("Open at login", isOn: Binding(get: { model.launchAtLogin }, set: { model.launchAtLogin = $0 }))
            }
            Section("Notes access") {
                LabeledContent("Full Disk Access", value: model.access == .granted ? "Granted" : (model.access == .notesMissing ? "No Notes data found" : "Not granted"))
                HStack {
                    Button("Open Full Disk Access settings") { model.openFullDiskAccessSettings() }
                    Button("Check Notes") { diagnostics = model.diagnose() }
                }
                if !diagnostics.isEmpty {
                    Text(diagnostics).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                }
                Button("Show sync state folder") {
                    NSWorkspace.shared.activateFileViewerSelecting([Paths.supportDirectory])
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .padding(.vertical, 8)
        .onAppear { model.refreshAccess() }
    }

    private func replaceToken() async {
        switch await model.checkToken(newToken) {
        case .valid:
            do { try model.saveToken(newToken); newToken = ""; message = "Saved." } catch { message = "Could not save to the Keychain." }
        case .invalid: message = "VALR did not accept this token."
        case .notConfigured: message = "This VALR has recording uploads turned off."
        case .unreachable(let why): message = "Could not reach VALR (\(why))."
        }
    }
}

import Foundation

/// The app used to be called "VALR Recorder Sync" (bundle id com.haciensus.valr.recordersync).
/// On first launch as "VALR" this carries over what an existing install had, so nothing is
/// sent twice and nothing has to be set up again:
///  - the state folder (what was already sent): ~/Library/Application Support/VALR Recorder Sync/
///    is moved to …/VALR/ when the new folder has no state yet;
///  - the settings (VALR address, options, "setup done") from the old preferences domain.
/// The upload token needs nothing: the Keychain service name never changed (Keychain.swift).
public enum LegacyMigration {
    public static let legacyDefaultsDomain = "com.haciensus.valr.recordersync"
    public static let settingsKey = "settings"

    /// Moves `legacy` to `current` when `legacy` has a state.json and `current` doesn't.
    /// Returns true when it moved something. Safe to call on every launch.
    @discardableResult
    public static func migrateSupportFolder(from legacy: URL = Paths.legacySupportDirectory,
                                            to current: URL = Paths.supportDirectory,
                                            fileManager fm: FileManager = .default) -> Bool {
        let oldState = legacy.appendingPathComponent("state.json")
        let newState = current.appendingPathComponent("state.json")
        guard fm.fileExists(atPath: oldState.path), !fm.fileExists(atPath: newState.path) else { return false }
        do {
            if fm.fileExists(atPath: current.path) {
                // An empty new folder (created before migrating): move the files in, then drop the old folder.
                for name in try fm.contentsOfDirectory(atPath: legacy.path) {
                    let target = current.appendingPathComponent(name)
                    if !fm.fileExists(atPath: target.path) {
                        try fm.moveItem(at: legacy.appendingPathComponent(name), to: target)
                    }
                }
                try? fm.removeItem(at: legacy)
            } else {
                try fm.createDirectory(at: current.deletingLastPathComponent(), withIntermediateDirectories: true)
                try fm.moveItem(at: legacy, to: current)
            }
            return true
        } catch {
            log.error("Could not move the old state folder: \(error.localizedDescription, privacy: .public)")
            return false
        }
    }

    /// Copies the saved settings from the old preferences domain when the new one has none.
    @discardableResult
    public static func migrateSettings(into defaults: UserDefaults = .standard,
                                       from legacy: UserDefaults? = UserDefaults(suiteName: legacyDefaultsDomain)) -> Bool {
        guard defaults.data(forKey: settingsKey) == nil,
              let old = legacy?.data(forKey: settingsKey) else { return false }
        defaults.set(old, forKey: settingsKey)
        return true
    }

    /// Both steps; call before anything reads the state or the settings.
    public static func run() {
        migrateSupportFolder()
        migrateSettings()
    }
}

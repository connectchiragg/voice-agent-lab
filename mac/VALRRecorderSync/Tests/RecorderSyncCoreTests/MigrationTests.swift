import Foundation
import Testing
@testable import RecorderSyncCore

@Suite("Rename from \"VALR Recorder Sync\" to \"VALR\"")
struct MigrationTests {
    @Test func movesTheOldStateFolderOnce() throws {
        let base = tempDir()
        let legacy = base.appendingPathComponent("VALR Recorder Sync"), current = base.appendingPathComponent("VALR")
        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        try Data("{\"version\":1}".utf8).write(to: legacy.appendingPathComponent("state.json"))

        #expect(LegacyMigration.migrateSupportFolder(from: legacy, to: current))
        #expect(FileManager.default.fileExists(atPath: current.appendingPathComponent("state.json").path))
        #expect(!FileManager.default.fileExists(atPath: legacy.path))
        #expect(!LegacyMigration.migrateSupportFolder(from: legacy, to: current)) // Nothing left to do.
    }

    @Test func movesIntoAnEmptyNewFolderButNeverOverwritesNewState() throws {
        let base = tempDir()
        let legacy = base.appendingPathComponent("VALR Recorder Sync"), current = base.appendingPathComponent("VALR")
        for dir in [legacy, current] { try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true) }
        try Data("old".utf8).write(to: legacy.appendingPathComponent("state.json"))
        #expect(LegacyMigration.migrateSupportFolder(from: legacy, to: current))
        #expect(try String(contentsOf: current.appendingPathComponent("state.json"), encoding: .utf8) == "old")

        try FileManager.default.createDirectory(at: legacy, withIntermediateDirectories: true)
        try Data("older".utf8).write(to: legacy.appendingPathComponent("state.json"))
        #expect(!LegacyMigration.migrateSupportFolder(from: legacy, to: current))
        #expect(try String(contentsOf: current.appendingPathComponent("state.json"), encoding: .utf8) == "old")
    }

    @Test func copiesSettingsOnlyWhenTheNewDomainHasNone() throws {
        let newName = "valr-test-new-\(UUID().uuidString)", oldName = "valr-test-old-\(UUID().uuidString)"
        let new = try #require(UserDefaults(suiteName: newName)), old = try #require(UserDefaults(suiteName: oldName))
        defer { new.removePersistentDomain(forName: newName); old.removePersistentDomain(forName: oldName) }
        old.set(Data("saved".utf8), forKey: LegacyMigration.settingsKey)

        #expect(LegacyMigration.migrateSettings(into: new, from: old))
        #expect(new.data(forKey: LegacyMigration.settingsKey) == Data("saved".utf8))
        old.set(Data("other".utf8), forKey: LegacyMigration.settingsKey)
        #expect(!LegacyMigration.migrateSettings(into: new, from: old))
        #expect(new.data(forKey: LegacyMigration.settingsKey) == Data("saved".utf8))
    }

    @Test func keychainServiceNameIsUnchanged() {
        #expect(TokenKeychain().service == "com.haciensus.valr.recordersync.upload-token")
    }
}

// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "VALRRecorderSync",
    platforms: [.macOS(.v14)],
    products: [
        // The binary inside VALR.app is named VALR (Activity Monitor shows it).
        .executable(name: "VALR", targets: ["VALRRecorderSync"]),
    ],
    targets: [
        // Everything testable without a GUI: reading Notes, classifying, uploading, state.
        .target(
            name: "RecorderSyncCore",
            linkerSettings: [.linkedLibrary("sqlite3")]
        ),
        // The SwiftUI menu-bar app.
        .executableTarget(
            name: "VALRRecorderSync",
            dependencies: ["RecorderSyncCore"]
        ),
        .testTarget(
            name: "RecorderSyncCoreTests",
            dependencies: ["RecorderSyncCore"],
            resources: [.copy("Fixtures")]
        ),
    ]
)

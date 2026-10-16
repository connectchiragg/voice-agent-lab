# VALR for Mac (menu-bar app)

When you record a phone call on an iPhone (iOS 18.1 or later), the iPhone saves it in **Notes** as an
audio file plus a transcript. iCloud then copies that note to your Mac. This small menu-bar app
notices each new call recording on the Mac and sends the audio to VALR. VALR transcribes it and adds it
to **Past interviews**, the same as a Truecaller upload.

```
iPhone call  →  Notes (iCloud)  →  this Mac  →  VALR (menu bar)  →  POST /hooks/recording-upload  →  Past interviews
```

- Menu-bar only (no Dock icon). It can open at login. Needs macOS 14 or later.
- It checks when the Notes data changes (FSEvents) and also every 2 minutes.
- It sends **only the audio** and a few hints: the file name, the note title ("Call with Jane"), the
  contact name, and the time of the call. It never sends any other note text. If a title has digits
  (a phone number), it is replaced with "Phone call", so no number leaves the Mac.
- The upload token is kept in the macOS **Keychain**. The app never writes it to a file or a log.
- An item counts as done only after VALR answers `ok`. What was sent is recorded in
  `~/Library/Application Support/VALR/state.json`, which holds attachment IDs, SHA-256
  digests and VALR IDs only.
- Optional and **off by default**: after VALR confirms a recording, the app moves the note to Notes'
  **Recently Deleted** folder, where Notes keeps it for 30 days.

The app is called **VALR** (`VALR.app`, bundle ID `com.haciensus.valr.mac`) and uses VALR's orb as
its icon; in the menu bar it shows a monochrome version of the orb that follows light and dark mode.

## Install and set up (owner, about 3 minutes)

> **Quit the old VALR Recorder Sync if you installed it.** Its menu › **Quit VALR Recorder Sync**,
> then move `/Applications/VALR Recorder Sync.app` to the Bin. VALR picks up its settings, its
> history of what was sent and its upload token by itself (see *Upgrading from VALR Recorder Sync*).

1. **Download it:** in VALR, **Settings › Phone call recordings › VALR for Mac › Download for Mac**
   (`VALR-<version>.dmg`; the page shows its SHA-256). Or build it:
   ```bash
   cd ~/projects/voice-agent-lab-macsync/mac
   ./build.sh            # makes dist/VALR.app, plus VALR-<version>.zip and VALR-<version>.dmg
   ```
2. **Install it:** open the DMG and drag **VALR** into **Applications**. Install it there first:
   macOS ties Full Disk Access to the app's location and signature.
3. **Open it the first time:** right-click the app and choose **Open**, then **Open** again.
   You only need to do this once, because the app is not notarized.
   - If macOS still blocks it, open **System Settings › Privacy & Security** and click **Open Anyway**.
   - Or run: `xattr -dr com.apple.quarantine /Applications/VALR.app`.
4. The setup window opens:
   1. **VALR address:** leave `https://valr.haciensus.com`.
   2. **Upload token:** in VALR, open **Settings › Phone call recordings › Show upload token**.
      Copy the token, paste it into the app, and click **Check & save**. The app checks the token
      without sending any audio, then stores it in the Keychain.
      - If you already put this token in the iPhone Shortcut, use the same token. Pressing
        **Rotate** in VALR would break the Shortcut.
   3. **Full Disk Access:**
      1. Click **Open Full Disk Access settings**.
      2. Click **+** and choose **Applications › VALR**, or switch it on if it's
         already in the list.
      3. The step in the app turns green by itself.
   4. **Options:** "Move the note to Recently Deleted" is off by default. If you turn it on, macOS
      asks once: *"VALR wants to control Notes"*. Click **Allow**.
5. Click **Start syncing**. The VALR orb appears in the menu bar.
6. **Check it:** open **Settings… › Check Notes**. It shows counts only, for example
   "Call recordings ready to send: 1".
7. **Test it end to end:** make a short call on the iPhone and record it. Wait until the note appears
   in Notes on the Mac and its audio has downloaded. Within about 2 minutes you get the notification
   *"Sent 1 call recording to VALR"*.

**The menu shows:** the status, the last sync time, how many were sent today, **Sync now**,
**Open VALR**, **Settings…** and **Quit**.

**When you rebuild the app:** an ad-hoc signature changes with every build, so macOS may:
- ask again for Keychain access (click **Always Allow**);
- need Full Disk Access granted again (remove the app from the list and add it back).

A Developer ID signature (see below) avoids this.

## Upgrading from VALR Recorder Sync

The app used to be called "VALR Recorder Sync" (bundle ID `com.haciensus.valr.recordersync`).
On its first launch, VALR (`Sources/RecorderSyncCore/LegacyMigration.swift`):
- moves `~/Library/Application Support/VALR Recorder Sync/` to `…/VALR/`, so recordings already
  sent are not sent again (VALR de-duplicates anyway);
- copies the settings (VALR address, options, setup done) from the old preferences;
- reads the same Keychain item: its service name, `com.haciensus.valr.recordersync.upload-token`,
  did not change. macOS may ask once whether VALR may use it: click **Always Allow**.

What you do by hand:
1. Quit the old app (menu › **Quit VALR Recorder Sync**) and move it from Applications to the Bin.
2. **System Settings › General › Login Items:** remove *VALR Recorder Sync* if it's listed. Turn
   on "Open at login" again in VALR's Settings if you want it.
3. **System Settings › Privacy & Security › Full Disk Access:** remove *VALR Recorder Sync* and add
   **VALR** (the setup window's step turns green).

## Releasing a new version

```bash
VERSION=1.0.1 scripts/release_mac.sh   # from the repo root
```
It runs `mac/build.sh`, copies the DMG to `downloads/VALR-<version>.dmg` (removing older ones) and
writes `downloads/mac.json` (version, size, SHA-256). Commit `downloads/`: the deploy ships the
branch with `git archive`, and the Dockerfile copies `downloads/` into the app image. The site
serves it to the signed-in owner at `GET /valr/downloads/mac`.

## How it reads the recordings

On macOS, AppleScript can list notes and attachment names, but it can't export an attachment's audio
reliably. So the app reads the Notes store directly, **read-only**, the same way public
forensic tools do:

1. It copies `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`, plus its `-wal`
   and `-shm` files, into a private temporary folder. It opens only the copy and deletes it
   afterwards.
   - Notes' own database is never opened, locked or changed. A test checks this: the file's
     bytes and modification time stay the same.
2. In the table `ZICCLOUDSYNCINGOBJECT`, an **attachment** row has:
   - `ZTYPEUTI`, for example `com.apple.m4a-audio` (`ZTYPEUTI1` takes priority when it exists);
   - `ZNOTE`, which points to the note row (title in `ZTITLE1`, creation date in `ZCREATIONDATE3`);
   - `ZMEDIA`, which points to the **media** row (`ZIDENTIFIER`, `ZFILENAME`, and `ZGENERATION1`
     on iOS 17+/macOS 14+).
3. The audio file is at:
   `…/group.com.apple.notes/Accounts/<account ZIDENTIFIER>/Media/<media ZIDENTIFIER>/[<generation>/]<ZFILENAME>`.
   If that exact path is missing, the app searches every account's `Media/<media ZIDENTIFIER>/`
   folder instead.
4. Column names change between OS versions, so the app reads them with `PRAGMA table_info` and uses
   whichever exist:
   - note account: `ZACCOUNT7`, `ZACCOUNT4` or `ZACCOUNT3`;
   - creation date: `ZCREATIONDATE3` or `ZCREATIONDATE1`.
5. It skips:
   - notes marked for deletion (`ZMARKEDFORDELETION`);
   - notes in Recently Deleted (folder `ZFOLDERTYPE` = 1);
   - locked notes (`ZISPASSWORDPROTECTED`), whose media is encrypted. They are never decrypted.
6. **Which audio counts as a call recording.** On the owner's Mac (macOS 26.6), a real synced call
   is a note titled **"Call with <Contact>"** in the ordinary "Notes" folder. There is no "Call
   Recordings" folder on the Mac. The note has one attachment, **"Call with <Contact>.m4a"**. So the
   app checks, in order:
   1. the title prefix (English, plus best-effort translations);
   2. the attachment name;
   3. a "Call Recordings" folder name (as on the iPhone, including translations).

   Other audio in Notes, such as voice memos, is not sent unless you turn that on in Settings.

**Sources**

- [apple_cloud_notes_parser](https://github.com/threeplanetssoftware/apple_cloud_notes_parser) by
  Three Planets Software:
  - `lib/AppleNotesEmbeddedObject.rb`: the media path, `ZMEDIA` → `ZIDENTIFIER`/`ZFILENAME`, and
    the generation columns.
  - `lib/AppleNotesEmbeddedPublicAudio.rb`: audio attachments.
  - `lib/AppleUniformTypeIdentifier.rb`: `com.apple.m4a-audio` is audio.
  - `lib/AppleNoteStore.rb`: the column names for each version, `ZCREATIONDATE3`, `ZACCOUNT7`, and
    the iOS 18 / iOS 26 markers.
  - `lib/AppleNotesAccount.rb`: the `Accounts/<id>/` folder.

**How sure we are**

- **High** for the attachment → media → file chain. It has been stable from iOS 9 to iOS 26, and
  the parser above handles it today.
- **Medium** for how a synced *call recording* is stored. The title and attachment name were
  observed on this Mac, but nobody has yet checked the database rows of a real call recording.
  Settings › **Check Notes** confirms it after the first real call.

**If direct reading ever breaks** (a schema change, or no Full Disk Access):
- The menu says so.
- The fallback is manual: in Notes, open the call, use the audio's **⋯ › Save to Files / Show in
  Finder**, then drop the file on the iPhone Shortcut or upload it some other way.
- The app does not automate this through AppleScript, because AppleScript can't export the audio
  reliably.

## Distribution: how other people install it

| Option | Cost | What a new user sees |
|---|---|---|
| **Ad-hoc signed .zip/.dmg** (implemented: `mac/build.sh`) | free | Must right-click › Open once, or clear quarantine (`xattr -dr com.apple.quarantine …`). Rebuilds need Full Disk Access and Keychain access granted again. |
| **Developer ID signed + notarized** (TODOs with exact `notarytool` commands in `build.sh`) | Apple Developer Program, **US$99/year** | Opens normally with no warnings. Permissions survive updates, because the signature identity stays the same. |
| **Sparkle auto-updates** (described, not built) | free library; needs a place to host `appcast.xml` (VALR's static files, or GitHub Releases) | The app checks for updates and installs them itself. Use it together with Developer ID signing. Updates are signed with an EdDSA key kept outside the repo. |
| **Homebrew cask** (described) | free | `brew install --cask valr`. A cask points at a versioned .dmg URL and its SHA-256. You can use your own tap (`brew tap haciensus/valr`) from the start. The official homebrew-cask repository only accepts notarized, reasonably popular apps. |

**Recommendation:** use the free ad-hoc build while VALR has one user. Move to Developer ID,
notarization and Sparkle once someone outside the team installs it.

## Multi-user design note

VALR is single-owner today: there is one upload token, a hash of which is stored server-side.

**To serve other people, VALR would need:**

1. **One upload token per user, or better per device.** Each token is stored as a hash together with
   its user ID, so uploads land in that user's Past interviews.
   - The existing Settings › Phone call recordings card (Show / Rotate) mostly covers this already.
     It would become a per-user list of devices that can each be revoked.
2. **"Connect a Mac" pairing** instead of copy-pasting a long token:
   1. In VALR Settings, the user clicks **Connect a Mac** and gets a short code, for example
      `ABCD-1234`, that works once and expires after 10 minutes.
   2. The app sends `POST /hooks/device-pair {code, device}`.
   3. VALR answers `{ok, token, account}`.
   4. The app stores the token in the Keychain.
   5. The route must be public: add a Cloudflare Access bypass, like the one for
      `/hooks/recording-upload`, and rate-limit it.
3. **Per-user limits and storage quotas**, instead of only the per-IP limit used today.

**What's already in the app:** the app side is done.
- `PairingClient` in `Sources/RecorderSyncCore/Pairing.swift` handles step 2, and it is tested
  against a mock server.
- The setup screen has an "I have a pairing code instead" switch.
- Until VALR has this endpoint, the app answers "This VALR does not offer pairing codes yet. Paste
  the upload token instead."

## Developing

```bash
cd mac
./test.sh                                  # unit tests: fixture Notes DB + local mock server
./build.sh                                 # VALR.app, VALR-<version>.zip/.dmg in mac/dist/
cd VALRRecorderSync && swift build         # debug build
```

**Running the tests:**
- They use `swift-testing`. With only the Command Line Tools installed, `test.sh` adds the framework
  paths for you. With full Xcode, plain `swift test` works.
- They never open the real Notes database:
  - `Tests/RecorderSyncCoreTests/Fixture.swift` builds a `NoteStore.sqlite` with the documented
    columns, in both the modern and the iOS 15 layout, plus media folders.
  - `Fixtures/sample-*.m4a` are synthetic speech files made with `say`.
  - `MockServer.swift` is a local HTTP server on 127.0.0.1 that stands in for VALR.
- Universal (Intel + Apple Silicon) builds need full Xcode. With only the Command Line Tools,
  `build.sh` builds for this Mac's architecture.

**Code layout:**

| File | What it does |
|---|---|
| `Sources/RecorderSyncCore/NotesStoreReader.swift` | Copy the database, adapt to the schema, find the media files. |
| `CallRecordingClassifier.swift` | Decide what counts as a call recording, and pick out the contact name. |
| `Uploader.swift` | Multipart upload, token check, error types. |
| `SyncEngine.swift` | One sync pass: retries with backoff, state, optional delete, diagnostics. |
| `StateStore.swift` | `state.json`. |
| `Keychain.swift` | The token. |
| `NotesDeleter.swift` | AppleScript to move a note to Recently Deleted, after checking the note's title. |
| `Pairing.swift` | The future "Connect a Mac" pairing. |
| `LegacyMigration.swift` | Carries an install over from the old name, VALR Recorder Sync. |
| `Sources/VALRRecorderSync/` | SwiftUI menu-bar app, setup, Settings, FSEvents watcher, notifications, open at login (`SMAppService`). |

**Retries:**
- A network error, a 5xx or a 429 is retried 4 times within a pass (2 s, 4 s, 8 s). After that the
  app waits 30 s, then 1 min, and so on, up to 30 min, before trying again. **Sync now** skips the
  wait.
- A 401 stops syncing and asks for a new token.
- A 413 or 415 marks that file as rejected, so it isn't sent again.
- At most 30 uploads per pass, because VALR allows 60 per hour per address.

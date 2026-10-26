# iOS In Rotation — 2026-10-21

## Built

- Added live and demo API methods/models for get, add, remove, and complete-order updates.
- Added one shared observable `RotationStore` for the phone and CarPlay. It persists Rotation metadata and full
  performance details for offline queue construction.
- Added background original-track downloads to Application Support, excluded them from backup, made Wi-Fi the
  default, exposed a cellular setting, restored active tasks after relaunch, and added retry/progress/storage state.
- Added the performance toggle and a dedicated phone Rotation tab with play, drag reorder, swipe remove, download
  status, retry, and total downloaded storage.
- Made `PlayerController` prefer each track's local original file before its stream URL.
- Reworked CarPlay tabs to Rotation, Performers, Performances, and Live. Rotation is first and one-tap playable;
  Performances contains recent/On This Day/Optoberfest plus Composer and Collection browsing.
- Bumped `CURRENT_PROJECT_VERSION` from 9 to 10 and updated `ios/CARPLAY.md`.

## Main files

- `ios/Sethster/API/APIClient.swift`, `MockAPIClient.swift`
- `ios/Sethster/Model/RotationModels.swift`, `RotationStore.swift`
- `ios/Sethster/Views/Rotation/RotationView.swift`, `Views/Performance/PerformanceView.swift`
- `ios/Sethster/Player/PlayerController.swift`
- `ios/Sethster/CarPlay/CarPlayController.swift`, `CarPlaySceneDelegate.swift`
- `ios/Sethster/App/SethsterApp.swift`, `Views/Settings/SettingsView.swift`

## Test

1. Connect to the Sethster server and add a performance from its detail screen.
2. Open Rotation and confirm progress reaches Downloaded and storage increases.
3. Reorder and swipe-remove; confirm the order survives relaunch and removal deletes local storage.
4. Disable networking, launch from Rotation on phone and CarPlay, and confirm playback uses local tracks.
5. In Settings, confirm cellular downloading is off by default and can be enabled.
6. Enable demo mode and confirm Rotation browsing/toggling still works without attempting audio downloads.
7. Build with `cd ios && SIM="iPhone 18 Pro" zsh tools/build.sh`.

## Verification

- A clean Swift 6 strict-concurrency iPhoneOS build completed with `BUILD SUCCEEDED`, with no source warnings or
  errors (code signing disabled for the local compile).
- The exact simulator command was also attempted, but this host's CoreSimulator runtime service was unavailable
  (`simdiskimaged` disconnected), so Xcode could not discover the named simulator. This is a host simulator-service
  failure before compilation; rerun step 7 after CoreSimulator is restored.

## Review

- Fixed background relaunch setup so the completion handler is installed before the lazily created background
  session begins delivering events, and the shared `RotationStore` is restored as the delegate's live owner.
- Background attempts are now identified by task, so cancellation, cellular-policy restarts, and re-adds cannot let
  a stale callback recreate removed media or overwrite the state of its replacement task. Launch-time scheduling
  also reconciles with the background session before creating work, preventing duplicate tasks while restoration is
  still asynchronous.
- Downloads now require a successful HTTP response and a nonempty regular file whose size matches the manifest when
  the server supplies `bytes`. Invalid/partial files are deleted and are never reported as downloaded or passed to
  `AVPlayer`; the temporary download is validated before being moved into Application Support.
- Server-side removals discovered by refresh now cancel active tasks, discard cached details, and delete the whole
  performance media directory. Explicit removal retains the same cancel-and-delete behavior after API success.
- Changing “Download over cellular” now cancels and recreates existing background tasks, because a task's cellular
  permission is immutable after creation.
- Rotation refreshes discard responses from an obsolete server/demo generation, and the app refreshes Rotation on
  every data-source generation change. Cached live Rotation remains available during automatic offline fallback,
  while explicit demo mode receives the demo Rotation state.
- Complete-order updates are serialized and coalesced; stale reorder responses no longer overwrite a newer phone
  drag or leave CarPlay observing a different order. A terminal failure restores the last server-confirmed order.
- CarPlay now removes replaced Rotation row objects from its playing-indicator index and guards the shared Now
  Playing template transition, avoiding unbounded stale rows and duplicate pushes while preserving platform list,
  section, tab, and navigation-depth limits.
- Review confirmed that Rotation playback resolves each queued track to its validated local original before the
  stream URL, including while the app is using automatic offline fallback. Demo items still browse/toggle normally
  and intentionally show the no-audio message.
- Verification: a Swift 6 code-signing-disabled iPhoneOS compile completed with `BUILD SUCCEEDED`. The requested
  `SIM="iPhone 18 Pro" zsh tools/build.sh` was retried, but this host still cannot discover simulator runtimes because
  `simdiskimaged` is disconnected; the failure occurs during destination discovery before project compilation.

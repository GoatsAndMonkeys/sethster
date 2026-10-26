# CarPlay

Sethster ships a CarPlay **audio** UI that shares the phone app's player, API client and settings.

## What's in the app

- `Sethster/CarPlay/CarPlaySceneDelegate.swift`: `CPTemplateApplicationSceneDelegate`, declared in
  Info.plist (`UIApplicationSceneManifest` → `CPTemplateApplicationSceneSessionRoleApplication`). The phone UI
  is still SwiftUI's `WindowGroup`; the manifest only adds the CarPlay role.
- `Sethster/CarPlay/CarPlayController.swift`: the templates.
  - `CPTabBarTemplate` is clamped to Apple's maximum and has four Dad-focused tabs:
    - **Rotation** (first): the short personal list. A tap starts the performance, including from the cached
      detail and original files when there is no reception. A checkmark and “Downloaded” mark complete items.
    - **Performers**: artists and conductors (most credits first) → their performances.
    - **Browse** (the phone's Browse): a Heavy Rotation preview; Composers, Performers, Ensembles, Venues, Years,
      Collections and All Performances; then On This Day, October's Optoberfest picks and Recently Completed.
    - **Radio**: *Your Stations* (every seeded station from `/api/radio/stations`, last played first), *Start a
      Station* (Composer / Performer / Ensemble Stations: the phone's station presets from `/api/radio/seeds`),
      and *Live Radio* (server list, falling back to the bundled directory). Reloaded each time the tab is
      selected. Without the server only live radio is offered.
    Lists longer than the car's item budget become an A–Z index of letter pages (`showPaged`); shorter
    alphabetical lists get letter sections with the side index.
    **Now Playing** is `CPNowPlayingTemplate.shared`. It is pushed when
    playback starts and is always reachable from CarPlay's own Now Playing button. It is not a tab, because
    Apple only allows list, grid, information and point-of-interest templates as tabs.
  - Each performance row shows the title, "performers · date", the server artwork, or the app's generated cover when there is no artwork. A
    "now playing" indicator marks the performance or live station that is currently playing. Composer and
    performer images use a portrait-aware half-crop/half-stretch square treatment.
  - Choosing a performance fetches it and plays it from track 1 on the **same** `PlayerController` the phone
    uses (`AppServices.player`), then pushes Now Playing. The Now Playing "Tracks" button (Up
    Next) lists the tracks so you can jump to one; it is hidden during radio (seeded or live).
  - Lists load lazily with async/await. They show "Loading…" first, then a "Try Again" row with the error
    if loading fails. Lists are trimmed to `CPListTemplate.maximumItemCount` / `maximumSectionCount`.
    Navigation stays within the audio-app limit of 5 templates (root included): Now Playing is pushed from
    the root when there would be no room left for its Tracks list.
  - When the app is serving the bundled demo data (no server), CarPlay lists the demo recordings. Selecting one
    shows a "Not available offline" alert because demo recordings have no audio.
- `AppServices` (in `SethsterApp.swift`) holds the single `AppModel`, `RotationStore`, `PlayerController`, and
  `RadioPlayer` for both scenes. CarPlay can connect without the phone UI ever appearing. Rotation metadata,
  full performance details, and background downloads live in Application Support and are excluded from backup.

## Entitlement: `com.apple.developer.carplay-audio`

Apple granted the CarPlay Audio capability for the App ID `com.goatsandmonkeys.sethster` in October 2026.
`Sethster/Entitlements/Sethster.entitlements` contains the CarPlay audio key, and `project.yml` applies it to
every build configuration and SDK:

```yaml
    settings:
      base:
        CODE_SIGN_ENTITLEMENTS: Sethster/Entitlements/Sethster.entitlements
```

A signed Release device build succeeds with this entitlement and the automatically managed provisioning profile.

## Testing in the simulator

`tools/build.sh` builds a Debug simulator app with the entitlement embedded:

```sh
strings ~/Library/Developer/Xcode/DerivedData/Sethster/Build/Products/Debug-iphonesimulator/Sethster.app/Sethster.debug.dylib | grep carplay-audio
```

**Xcode 27.1 caveat:** Simulator.app has been replaced by **Device Hub**. Device Hub has no
*I/O → External Displays → CarPlay* menu. Its "CarPlay Simulator" plug-in only connects to physical
iPhones over USB or Bluetooth. Private CoreSimulator calls could turn on CarPlay in the iOS 27 simulator
(`SimDevice enableCarPlay:`), switch the built-in "Wireless" screen to a CarPlay mode, and attach the
CarPlay HID service. The CarPlay shell launched, but it still treated that screen as an AirPlay
mirror ("is car display: NO"). So the CarPlay scene could not be shown in the iOS 27 simulator here.

Ways to see the CarPlay UI:

- An Xcode or simulator runtime whose Simulator still has *I/O → External Displays → CarPlay* (Xcode ≤ 26).
  Build with `tools/build.sh`, run the app, open the CarPlay display, and choose a downloaded item from Rotation.
- A signed device build, with the iPhone plugged into Device Hub's **CarPlay Simulator** (or a real car).

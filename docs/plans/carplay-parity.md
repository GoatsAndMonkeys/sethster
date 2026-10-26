# CarPlay parity: Browse + Radio (2026-10-23)

Goal: the car gets the phone's Browse (composers, performers, ensembles, venues, collections, years, all
performances) and all of Radio (the user's created seeded stations, the seed "presets", live stations).
Build on `ios/Sethster/CarPlay/CarPlayController.swift`; no server changes (every endpoint already exists:
`/api/radio/stations`, `/api/radio/seeds?type=&limit=`, `/api/radio/live`, `/api/performances`, etc.).

"Presets" = the phone's Radio → Stations "Start a station" list (`RadioClient.seeds`, one station per
composer / performer / ensemble, ranked by excerpt + famous counts). Live stations (server `/live`, bundled
`live_stations.json` fallback) are shown too, so either reading of "presets" is covered.

## Tabs (4, still clamped to `CPTabBarTemplate.maximumTabCount`)

1. **Rotation**: unchanged.
2. **Performers**: unchanged (ranked by credits), but goes through the new paging helper.
3. **Browse** (was "Performances"; the phone calls it Browse): Heavy Rotation preview, then a Browse section:
   Composers, Performers, Ensembles, Venues, Years, Collections, **All Performances** (new), then
   On This Day / Optoberfest / Recently Completed as today.
4. **Radio** (replaces "Live"):
   - *Your Stations*: every `RadioClient.stations()` row (server order = last played first). Tap → `radio.start(station:name:api:)`.
   - *Start a Station*: three rows: Composer / Performer / Ensemble Stations → pushed list of
     `seeds(type:, limit: 500)` (ranked), tap → `radio.start(seed:api:)`.
   - *Live Radio*: every live station, as today.
   - If everything doesn't fit the item budget, Your Stations shows a prefix + "All N Stations" row, and Live
     collapses into one "Live Stations" row that pushes the full list.
   - Demo mode / no server: Your Stations and presets replaced by one row "Station radio needs the Sethster
     server"; Live still works (bundled list).
   - Refreshed every time the Radio tab is selected (`CPTabBarTemplateDelegate`), so stations created on the
     phone or by starting a preset in the car appear.

## Long lists: letter paging

One helper `showPaged(elements, letter:, row:, in:, ordered:)`:
- Fits in `CPListTemplate.maximumItemCount` (re-read at build time; the car can lower it while driving):
  alphabetical lists become one section per letter with `sectionIndexTitle` (if the letter count fits
  `maximumSectionCount`, else one flat section); ranked lists stay one flat ranked section.
- Too many: a list of letter rows ("A · 14 composers"), each pushing that letter's items (alphabetical;
  rows built lazily so we don't build/load images for thousands of rows). A single overfull letter is still
  clamped by `clamp`.
- Letter key: first letter of the existing `sortKey` (last name) for people/composers, of the title for
  performances/venues/stations; diacritics folded; non-letters → "#".
- Used by: Composers, Performers tab + Performers/Ensembles pushes, Venues, All Performances (all
  performances via the existing `allPerformances` pager), seed preset lists, "All N Stations".

## Depth

Audio apps: 5 templates including the root. Deepest path: Browse → Composers → Letter → Composer → Now
Playing = 5, and NP's Tracks list would be a 6th. So: `showNowPlaying` (which already reuses NP if it is in
the stack) pops to root first when pushing NP would leave no room for Tracks (stack ≥ 4). A `maxDepth = 5`
constant guards the list push helpers too (at the limit, pop to root before pushing).

## Playback / account safety

- Seeded / live starts are synchronous, but the lists are loaded async: each list captures
  `account.epoch` at load; the tap handler refuses (just `done()`) if the epoch changed. Same for live rows
  (currently unchecked).
- Now Playing "Tracks" (Up Next) button disabled whenever `radio.isActive` (seeded radio has no
  PlayerController queue; today it's only disabled for live).
- Playing indicators: seeded-station rows track `radio.stationId` (added to `observeRadio`).
- After `radio.start…`, if `radio.errorMessage` is set (e.g. no server) present the alert, else push Now Playing.

## Not in scope / gaps

- No search in CarPlay (CPSearchTemplate isn't allowed for audio apps); no deleting stations from the car.
- No server changes, no TestFlight. CarPlay simulator can't attach on Xcode 27.1 (see ios/CARPLAY.md), so
  verification = clean simulator build + code review; ios has no unit-test target.
- Update ios/CARPLAY.md's tab description.

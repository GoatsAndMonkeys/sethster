import CarPlay
import Observation
import UIKit

/// Builds and drives Dad's CarPlay audio UI: Rotation, Performers, Browse and Radio. Now Playing is the
/// shared CPNowPlayingTemplate, pushed when playback starts (CarPlay also shows its own Now Playing button
/// in the navigation bar whenever Sethster is the now-playing app).
/// Lists are shallow (tab → list [→ letter] → performances), load lazily, and respect the CarPlay item/section
/// limits: a list too long for the car's item budget becomes an A–Z index of letter pages.
@MainActor
final class CarPlayController: NSObject {
    private let interface: CPInterfaceController
    private let model: AppModel
    private let rotationStore: RotationStore
    private let player: PlayerController
    private let radio: RadioPlayer
    private let account: AccountStore

    private var tabBar: CPTabBarTemplate?
    private let rotation = CPListTemplate(title: "Rotation", sections: [])
    private let performers = CPListTemplate(title: "Performers", sections: [])
    /// The phone's Browse: Heavy Rotation preview, ways to browse, then On This Day etc.
    private let performances = CPListTemplate(title: "Browse", sections: [])
    /// Your seeded stations, the station presets (one per composer / performer / ensemble), and live radio.
    private let radioTab = CPListTemplate(title: "Radio", sections: [])
    /// The whole root while there's no approved account (or demo browsing) on the phone.
    private let signIn = CPListTemplate(title: "Sethster", sections: [])
    private var showingContent = false
    private var shownEpoch = 0
    private var nowPlaying: CPNowPlayingTemplate { .shared }

    /// Performance list items currently on screen, keyed by performance id, for the "now playing" indicator.
    /// Weak, so rows of popped templates drop out on their own.
    private var performanceItems = RowIndex()
    private var rotationItems: [String: [CPListItem]] = [:]
    /// The Performances tab's sections below "In Rotation", kept so a Rotation change can redraw the tab without
    /// refetching home. nil until home has loaded (or while it shows an error).
    private var homeSections: [CPListSection]?
    private var liveItems = RowIndex()
    /// Seeded-station rows (Your Stations and the presets), keyed by station id, for the "now playing" indicator.
    private var stationItems = RowIndex()
    /// Only the newest Radio tab load may draw (it reloads every time the tab is selected).
    private var radioLoadToken = 0
    private var placeholderCache: [String: UIImage] = [:]
    private var lastRadioError: String?
    private var loadedGeneration = -1
    private var active = false
    private var nowPlayingTransitionInFlight = false

    init(interface: CPInterfaceController, model: AppModel, rotation: RotationStore,
         player: PlayerController, radio: RadioPlayer, account: AccountStore) {
        self.interface = interface
        self.model = model
        rotationStore = rotation
        self.player = player
        self.radio = radio
        self.account = account
        super.init()
    }

    // MARK: - Lifecycle

    func start() {
        active = true
        rotation.tabImage = UIImage(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
        performers.tabImage = UIImage(systemName: "person.2")
        performances.tabImage = UIImage(systemName: "music.note.list")
        radioTab.tabImage = UIImage(systemName: "dot.radiowaves.left.and.right")
        for t in [rotation, performers, performances, radioTab] { showLoading(t) }

        configureNowPlaying()
        let templates = [rotation, performers, performances, radioTab]
        let tabs = CPTabBarTemplate(templates: Array(templates.prefix(CPTabBarTemplate.maximumTabCount)))
        tabs.delegate = self
        tabBar = tabs
        showingContent = account.showsContent
        shownEpoch = account.epoch
        if showingContent {
            interface.setRootTemplate(tabs, animated: false, completion: nil)
            reloadAll()
        } else {
            showSignIn()
        }
        observeAccount()
        observeModel()
        observeRotation()
        observePlayer()
        observeRadio()
    }

    func stop() {
        active = false
        nowPlaying.remove(self)
    }

    // MARK: - Account

    /// Not approved (and not browsing demo data): one row pointing at the phone, with the account's status.
    private func showSignIn() {
        let detail: String? = switch account.gate {
        case .status(.pending): "Your account is waiting for approval."
        case .status(.rejected): "Your account request wasn’t approved."
        case .status(.disabled): "Your account has been turned off."
        default: nil
        }
        let item = CPListItem(text: "Open Sethster on your iPhone to sign in", detailText: detail,
                              image: UIImage(systemName: "iphone"))
        item.handler = { _, completion in completion() }
        signIn.updateSections([CPListSection(items: [item])])
        interface.setRootTemplate(signIn, animated: false, completion: nil)
    }

    /// Sign-in, sign-out, account switch or revocation on the phone: reset to the right root templates.
    private func observeAccount() {
        guard active else { return }
        withObservationTracking {
            _ = account.showsContent
            _ = account.epoch
            _ = account.gate
        } onChange: { [weak self] in
            Task { @MainActor in
                guard let self, self.active else { return }
                self.applyAccount()
                self.observeAccount()
            }
        }
    }

    private func applyAccount() {
        let content = account.showsContent
        let epochChanged = account.epoch != shownEpoch
        shownEpoch = account.epoch
        if !content {
            if interface.presentedTemplate != nil { interface.dismissTemplate(animated: false, completion: nil) }
            showingContent = false
            showSignIn()
            return
        }
        guard !showingContent || epochChanged, let tabBar else { return }
        showingContent = true
        if interface.presentedTemplate != nil { interface.dismissTemplate(animated: false, completion: nil) }
        interface.setRootTemplate(tabBar, animated: false, completion: nil)
        interface.popToRootTemplate(animated: false, completion: nil)
        tabBar.select(rotation)
        reloadAll()
    }

    private func reloadAll() {
        loadedGeneration = model.generation
        performanceItems = RowIndex()
        rotationItems = [:]
        homeSections = nil
        liveItems = RowIndex()
        stationItems = RowIndex()
        Task { await loadRotation() }
        Task { await loadPerformers() }
        Task { await loadRecent() }
        Task { await loadRadio() }
    }

    private func observeRotation() {
        guard active else { return }
        withObservationTracking {
            _ = rotationStore.items
            _ = rotationStore.trackProgress
            _ = rotationStore.failedTracks
        } onChange: { [weak self] in
            Task { @MainActor in
                guard let self, self.active else { return }
                await self.loadRotation(refresh: false)
                self.observeRotation()
            }
        }
    }

    /// Refetch when the server / demo setting changes on the phone.
    private func observeModel() {
        guard active else { return }
        withObservationTracking { _ = model.generation } onChange: { [weak self] in
            Task { @MainActor in
                guard let self, self.active else { return }
                if self.showingContent, self.model.generation != self.loadedGeneration {
                    if !(self.interface.topTemplate is CPNowPlayingTemplate) {
                        self.interface.popToRootTemplate(animated: false, completion: nil)
                    }
                    self.reloadAll()
                }
                self.observeModel()
            }
        }
    }

    /// Keep the "playing" indicator on list rows in sync with the shared player.
    private func observePlayer() {
        guard active else { return }
        withObservationTracking { _ = player.performance?.id; _ = player.isPlaying } onChange: { [weak self] in
            Task { @MainActor in
                guard let self, self.active else { return }
                self.refreshPlayingIndicators()
                self.observePlayer()
            }
        }
    }

    /// Live radio has its own observable player, so its row and the Now Playing controls need separate tracking.
    private func observeRadio() {
        guard active else { return }
        withObservationTracking {
            _ = radio.live?.id
            _ = radio.stationId
            _ = radio.isPlaying
            _ = radio.errorMessage
        } onChange: { [weak self] in
            Task { @MainActor in
                guard let self, self.active else { return }
                self.refreshPlayingIndicators()
                self.refreshNowPlayingControls()
                if let message = self.radio.errorMessage, message != self.lastRadioError {
                    self.lastRadioError = message
                    self.presentAlert(message)
                } else if self.radio.errorMessage == nil {
                    self.lastRadioError = nil
                }
                self.observeRadio()
            }
        }
    }

    private func refreshPlayingIndicators() {
        let current = player.hasQueue ? player.performance?.id : nil
        performanceItems.forEach { id, item in item.isPlaying = id == current }
        let currentLive = radio.isLive ? radio.live?.id : nil
        liveItems.forEach { id, item in item.isPlaying = id == currentLive }
        let currentStation = radio.isLive ? nil : radio.stationId
        stationItems.forEach { id, item in item.isPlaying = id == currentStation }
    }

    // MARK: - Tabs

    private func loadRotation(refresh: Bool = true) async {
        if refresh { await rotationStore.refresh() }
        for rows in rotationItems.values { performanceItems.remove(rows) }
        rotationItems = [:]
        show([CPListSection(items: rotationStore.items.map(rotationRow))], in: rotation, empty: "Nothing in Rotation")
        renderPerformances()
    }

    /// A Rotation row (plays the downloaded copy when there is one). Built separately for each tab that shows it.
    private func rotationRow(_ item: RotationItem) -> CPListItem {
        let downloaded = rotationStore.downloadState(for: item.id) == .downloaded
        let detail = [item.subtitle ?? item.composers.first?.name, Fmt.shortDate(item.date),
                      downloaded ? "Downloaded" : nil]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        let row = CPListItem(text: item.title, detailText: detail,
                             image: placeholder(title: item.title, seed: item.collection ?? item.id),
                             accessoryImage: downloaded ? UIImage(systemName: "checkmark.circle.fill") : nil,
                             accessoryType: .none)
        row.playingIndicatorLocation = .trailing
        row.isPlaying = player.hasQueue && player.performance?.id == item.id
        loadImage(item.artworkUrl, into: row, api: model.api)
        row.handler = { [weak self] _, done in self?.playRotation(item, done: done) }
        performanceItems.add(row, for: item.id)
        rotationItems[item.id, default: []].append(row)
        return row
    }

    private func loadPerformers() async {
        await fillPerformers(performers)
    }

    private func loadRecent() async {
        let api = model.api
        let generation = model.generation, epoch = account.epoch
        // A request from before a server/demo switch or account change must not overwrite the newer load
        // (e.g. a live request timing out after the offline fallback has drawn the demo lists).
        var current: Bool { generation == model.generation && epoch == account.epoch }
        do {
            async let homeRequest = api.home()
            await OptoberfestStore.shared.load(api: api)
            let home = try await homeRequest
            guard current else { return }
            var sections: [CPListSection] = []
            if !home.onThisDay.isEmpty {
                sections.append(CPListSection(items: home.onThisDay.map(performanceItem), header: "On This Day",
                                              sectionIndexTitle: nil))
            }
            if OptoberfestStore.isActive(), let pick = OptoberfestStore.shared.today {
                // One section (today's pick, then the rest) to stay within CarPlay's section limit.
                sections.append(CPListSection(items: ([pick] + OptoberfestStore.shared.more).map(themeItem),
                                              header: OptoberfestStore.headline, sectionIndexTitle: nil))
            }
            if !home.recent.isEmpty {
                sections.append(CPListSection(items: home.recent.map(performanceItem), header: "Recently Completed",
                                              sectionIndexTitle: nil))
            }
            homeSections = sections
            renderPerformances()
        } catch {
            guard current else { return }
            homeSections = nil
            showError(error, in: performances) { [weak self] in await self?.loadRecent() }
        }
    }

    /// Browse tab: Heavy Rotation, then ways to browse, then On This Day / Optoberfest / Recently Completed.
    /// Redrawn when the Rotation changes; waits for home so a load error isn't overwritten.
    private func renderPerformances() {
        guard let homeSections else { return }
        var sections: [CPListSection] = []
        let all = rotationStore.items
        if !all.isEmpty {
            // A short preview so Browse stays inside the CarPlay item budget; the Rotation tab has the full list.
            var rows = all.prefix(Self.rotationPreviewCount).map(rotationRow)
            if all.count > Self.rotationPreviewCount {
                let more = CPListItem(text: "All of Heavy Rotation", detailText: "\(all.count) performances",
                                      image: UIImage(systemName: "arrow.trianglehead.2.clockwise.rotate.90"),
                                      accessoryImage: nil, accessoryType: .disclosureIndicator)
                more.handler = { [weak self] _, done in
                    guard let self else { done(); return }
                    self.tabBar?.select(self.rotation)
                    done()
                }
                rows.append(more)
            }
            sections.append(CPListSection(items: rows, header: "Heavy Rotation", sectionIndexTitle: nil))
        }
        let browse: [(String, String, String, (@escaping () -> Void) -> Void)] = [
            ("Composers", "By composer", "music.quarternote.3", { [weak self] in self?.pushComposers(done: $0) }),
            ("Performers", "Singers, conductors, soloists", "person.2",
             { [weak self] in self?.pushPeople(title: "Performers", ensembles: false, done: $0) }),
            ("Ensembles", "Orchestras, choruses, companies", "person.3",
             { [weak self] in self?.pushPeople(title: "Ensembles", ensembles: true, done: $0) }),
            ("Venues", "Opera houses and concert halls", "building.columns", { [weak self] in self?.pushVenues(done: $0) }),
            ("Years", "By year performed", "calendar", { [weak self] in self?.pushYears(done: $0) }),
            ("Collections", "Recording collections", "square.stack", { [weak self] in self?.pushCollections(done: $0) }),
            ("All Performances", "Every recording, A–Z", "list.bullet.rectangle",
             { [weak self] in self?.pushAllPerformances(done: $0) }),
        ]
        let browseItems = browse.map { text, detail, symbol, push -> CPListItem in
            let item = CPListItem(text: text, detailText: detail, image: UIImage(systemName: symbol),
                                  accessoryImage: nil, accessoryType: .disclosureIndicator)
            item.handler = { _, done in push(done) }
            return item
        }
        // Server unreachable: say so above the (demo) lists; the Rotation cache above is real.
        sections.append(CPListSection(items: browseItems,
                                      header: model.isOfflineFallback ? "Server unreachable · Demo recordings" : "Browse",
                                      sectionIndexTitle: nil))
        sections += homeSections
        show(sections, in: performances, empty: "Nothing here yet")
    }

    // MARK: - Radio

    /// Radio tab: Your Stations (every seeded station, last played first), Start a Station (the presets), and Live
    /// Radio. Reloaded whenever the tab is selected so stations made on the phone (or by a preset here) appear.
    private func loadRadio() async {
        radioLoadToken += 1
        let token = radioLoadToken
        let api = model.api
        let epoch = account.epoch
        let client = RadioClient(api: api)
        async let liveRequest = Self.liveStations(client)
        var stations: Result<[RadioStation], any Error>?
        if let client {
            do { stations = .success(try await client.stations()) } catch { stations = .failure(error) }
        }
        let live = await liveRequest
        guard token == radioLoadToken, epoch == account.epoch, active else { return }
        renderRadio(stations: stations, live: live, api: api, epoch: epoch)
    }

    private static func liveStations(_ client: RadioClient?) async -> [LiveStation] {
        if let client, let server = try? await client.live(), !server.isEmpty { return server }
        return BundledLiveStations.all
    }

    /// `stations` nil: no collection radio (demo data / no server); live radio still works.
    private func renderRadio(stations: Result<[RadioStation], any Error>?, live: [LiveStation],
                             api: any SethsterAPI, epoch: Int) {
        let budget = CPListTemplate.maximumItemCount
        let presets = stations == nil ? [] : RadioSeedType.allCases.map(presetRow)
        var yours: [CPListItem] = []
        switch stations {
        case nil:
            yours = [infoRow("Station radio needs the Sethster server", detail: "Live radio still works.",
                             symbol: "antenna.radiowaves.left.and.right.slash")]
        case .failure(let error)?:
            let retry = CPListItem(text: "Can’t load your stations", detailText: Self.message(for: error),
                                   image: UIImage(systemName: "arrow.clockwise"))
            retry.handler = { [weak self] _, done in
                done()
                Task { await self?.loadRadio() }
            }
            yours = [retry]
        case .success(let list)?:
            if list.isEmpty {
                yours = [infoRow("No stations yet", detail: "Start one below, or from Radio on your iPhone.",
                                 symbol: "dot.radiowaves.left.and.right")]
            } else if list.count + presets.count + min(live.count, 1) <= budget {
                // All of them fit (Live collapses to one row below if it must).
                yours = list.map { stationRow($0, api: api, epoch: epoch) }
            } else {
                // Too long for the car: a preview, then the whole list (A–Z pages if needed) one level down.
                let room = max(1, min(Self.radioPreviewCount, budget - presets.count - 2))
                yours = list.prefix(room).map { stationRow($0, api: api, epoch: epoch) }
                if list.count > room {
                    let all = CPListItem(text: "All Your Stations", detailText: "\(list.count) stations",
                                         image: UIImage(systemName: "list.bullet"), accessoryImage: nil,
                                         accessoryType: .disclosureIndicator)
                    all.handler = { [weak self] _, done in
                        guard let self else { done(); return }
                        let t = CPListTemplate(title: "Your Stations", sections: [])
                        self.showPaged(list, key: { Self.nameKey($0.name) }, order: .ranked, noun: "stations",
                                       in: t, empty: "No stations") { [unowned self] in
                            self.stationRow($0, api: api, epoch: epoch)
                        }
                        self.push(t, done: done)
                    }
                    yours.append(all)
                }
            }
        }
        var sections = [CPListSection(items: yours, header: "Your Stations", sectionIndexTitle: nil)]
        if !presets.isEmpty {
            sections.append(CPListSection(items: presets, header: "Start a Station", sectionIndexTitle: nil))
        }
        let used = yours.count + presets.count
        if live.isEmpty {
            // Nothing to list (no server list and no bundled directory).
        } else if live.count <= budget - used {
            sections.append(CPListSection(items: live.map { liveRow($0, api: api, epoch: epoch) },
                                          header: "Live Radio", sectionIndexTitle: nil))
        } else {
            let row = CPListItem(text: "Live Stations", detailText: "\(live.count) stations",
                                 image: UIImage(systemName: "antenna.radiowaves.left.and.right"),
                                 accessoryImage: nil, accessoryType: .disclosureIndicator)
            row.handler = { [weak self] _, done in
                guard let self else { done(); return }
                let t = CPListTemplate(title: "Live Radio", sections: [])
                self.showPaged(live, key: { Self.nameKey($0.name) }, order: .ranked, noun: "stations",
                               in: t, empty: "No live stations") { [unowned self] in
                    self.liveRow($0, api: api, epoch: epoch)
                }
                self.push(t, done: done)
            }
            sections.append(CPListSection(items: [row], header: "Live Radio", sectionIndexTitle: nil))
        }
        show(sections, in: radioTab, empty: "No radio")
    }

    private func infoRow(_ text: String, detail: String?, symbol: String) -> CPListItem {
        let item = CPListItem(text: text, detailText: detail, image: UIImage(systemName: symbol))
        item.handler = { _, done in done() }
        return item
    }

    /// One of "Composer / Performer / Ensemble Stations" → every station of that kind the collection can make.
    private func presetRow(_ type: RadioSeedType) -> CPListItem {
        let (text, detail) = switch type {
        case .composer: ("Composer Stations", "A station for each composer")
        case .person: ("Performer Stations", "Singers, conductors, soloists")
        case .ensemble: ("Ensemble Stations", "Orchestras, choruses, companies")
        }
        let item = CPListItem(text: text, detailText: detail, image: UIImage(systemName: type.icon),
                              accessoryImage: nil, accessoryType: .disclosureIndicator)
        item.handler = { [weak self] _, done in
            guard let self else { done(); return }
            let t = CPListTemplate(title: text, sections: [])
            self.showLoading(t)
            self.push(t, done: done)
            Task { await self.fillSeeds(t, type: type) }
        }
        return item
    }

    /// Most excerpts (and famous numbers) first, as on the phone; A–Z pages when the car can't show them all.
    private func fillSeeds(_ template: CPListTemplate, type: RadioSeedType) async {
        let api = model.api
        let epoch = account.epoch
        guard let client = RadioClient(api: api) else {
            show([], in: template, empty: "Station radio needs the Sethster server")
            return
        }
        do {
            let seeds = try await client.seeds(q: nil, type: type, limit: 500)
            guard epoch == account.epoch else { return }
            showPaged(seeds, key: { Self.nameKey($0.name, lastName: $0.type != .ensemble) }, order: .ranked,
                      noun: "stations", in: template, empty: "No stations") { [unowned self] in
                self.seedRow($0, api: api, epoch: epoch)
            }
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillSeeds(template, type: type) }
        }
    }

    private func seedRow(_ seed: RadioSeed, api: any SethsterAPI, epoch: Int) -> CPListItem {
        let count = seed.excerptCount.map { n -> String in
            let f = seed.famousCount ?? 0
            return f > 0 ? "\(n) excerpts, \(f) famous" : "\(n) excerpts"
        }
        let detail = [seed.subtitle, count].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        let item = CPListItem(text: "\(seed.name) Radio", detailText: detail,
                              image: placeholder(title: "", seed: seed.id))
        item.playingIndicatorLocation = .trailing
        item.isPlaying = !radio.isLive && radio.stationId == seed.stationId
        loadImage(seed.imageUrl, into: item, api: api, mode: .portrait)
        item.handler = { [weak self] _, done in
            self?.startRadio(epoch: epoch, done: done) { $0.radio.start(seed: seed, api: api) }
        }
        stationItems.add(item, for: seed.stationId)
        return item
    }

    private func stationRow(_ station: RadioStation, api: any SethsterAPI, epoch: Int) -> CPListItem {
        let detail = [station.excerptCount.map { "\($0) excerpts" }, station.playCount.map { "\($0) played" }]
            .compactMap { $0 }.joined(separator: " · ")
        let item = CPListItem(text: station.name, detailText: detail.isEmpty ? nil : detail,
                              image: placeholder(title: "", seed: station.id))
        item.playingIndicatorLocation = .trailing
        item.isPlaying = !radio.isLive && radio.stationId == station.id
        item.handler = { [weak self] _, done in
            self?.startRadio(epoch: epoch, done: done) {
                $0.radio.start(station: station.id, name: station.name, api: api)
            }
        }
        stationItems.add(item, for: station.id)
        return item
    }

    private func liveRow(_ station: LiveStation, api: any SethsterAPI, epoch: Int) -> CPListItem {
        let detail = [station.broadcaster, station.city, station.country]
            .compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · ")
        let item = CPListItem(text: station.name, detailText: detail,
                              image: placeholder(title: "", seed: station.id))
        item.playingIndicatorLocation = .trailing
        item.isPlaying = radio.isLive && radio.live?.id == station.id
        loadImage(station.logoUrl, into: item, api: api)
        item.handler = { [weak self] _, done in
            self?.startRadio(epoch: epoch, done: done) { $0.radio.start(live: station, api: api) }
        }
        liveItems.add(item, for: station.id)
        return item
    }

    /// Starts a seeded or live station picked from a list loaded under `epoch` (ignored after a sign-out or
    /// account switch), then shows Now Playing. Seeded-station errors arrive later via `observeRadio`.
    private func startRadio(epoch: Int, done: @escaping () -> Void, _ start: (CarPlayController) -> Void) {
        guard epoch == account.epoch, account.showsContent else { done(); return }
        start(self)
        refreshPlayingIndicators()
        refreshNowPlayingControls()
        done()
        if let message = radio.errorMessage {
            lastRadioError = message
            presentAlert(message)
        } else {
            showNowPlaying()
        }
    }

    // MARK: - Browse

    private func pushComposers(done: @escaping () -> Void) {
        let template = CPListTemplate(title: "Composers", sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillComposers(template) }
    }

    private func fillComposers(_ template: CPListTemplate) async {
        let api = model.api
        do {
            let list = try await api.composers()
                .filter { ($0.performanceCount ?? 1) > 0 }
                .sorted { Self.sortKey($0.name) < Self.sortKey($1.name) }
            showPaged(list, key: { Self.sortKey($0.name) }, order: .alphabetical, noun: "composers", in: template,
                      empty: "No composers") { [unowned self] c -> CPListItem in
                let count = c.performanceCount.map { $0 == 1 ? "1 performance" : "\($0) performances" }
                let item = CPListItem(text: c.name, detailText: [c.years?.value, count].compactMap { $0 }
                    .filter { !$0.isEmpty }.joined(separator: " · "),
                                      image: placeholder(title: "", seed: c.id), accessoryImage: nil,
                                      accessoryType: .disclosureIndicator)
                loadImage(c.imageUrl, into: item, api: api, mode: .portrait)
                item.handler = { [weak self] _, done in
                    self?.pushPerformances(title: c.name, query: PerformanceQuery(composer: c.id), done: done)
                }
                return item
            }
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillComposers(template) }
        }
    }

    private func pushCollections(done: @escaping () -> Void) {
        let template = CPListTemplate(title: "Collections", sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillCollections(template) }
    }

    private func fillCollections(_ template: CPListTemplate) async {
        let api = model.api
        do {
            let list = try await api.home().collections
            let items = list.map { c -> CPListItem in
                let item = CPListItem(text: c.name,
                                      detailText: c.count.map { $0 == 1 ? "1 performance" : "\($0) performances" },
                                      image: placeholder(title: "", seed: c.name), accessoryImage: nil,
                                      accessoryType: .disclosureIndicator)
                item.handler = { [weak self] _, done in
                    self?.pushPerformances(title: c.name, query: PerformanceQuery(collection: c.name), done: done)
                }
                return item
            }
            show([CPListSection(items: items)], in: template, empty: "No collections")
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillCollections(template) }
        }
    }

    private func pushVenues(done: @escaping () -> Void) {
        let template = CPListTemplate(title: "Venues", sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillVenues(template) }
    }

    private func fillVenues(_ template: CPListTemplate) async {
        let api = model.api
        do {
            var counts: [String: (VenueRef, Int)] = [:]
            for p in try await Self.allPerformances(api) {
                if let v = p.venue { counts[v.id] = (v, (counts[v.id]?.1 ?? 0) + 1) }
            }
            let venues = counts.values.map { VenueSummary(id: $0.0.id, name: $0.0.name, city: $0.0.city, count: $0.1) }
                .sorted {
                $0.count != $1.count ? $0.count > $1.count : Self.sortKey($0.name) < Self.sortKey($1.name)
            }
            showPaged(venues, key: { Self.nameKey($0.name) }, order: .ranked, noun: "venues", in: template,
                      empty: "No venues") { [unowned self] v -> CPListItem in
                let count = v.count == 1 ? "1 performance" : "\(v.count) performances"
                let item = CPListItem(text: v.name, detailText: [v.city, count].compactMap { $0 }
                    .filter { !$0.isEmpty }.joined(separator: " · "),
                                      image: placeholder(title: "", seed: v.id), accessoryImage: nil,
                                      accessoryType: .disclosureIndicator)
                item.handler = { [weak self] _, done in
                    self?.pushPerformances(title: v.name, query: PerformanceQuery(venue: v.id), done: done)
                }
                return item
            }
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillVenues(template) }
        }
    }

    private func pushYears(done: @escaping () -> Void) {
        let template = CPListTemplate(title: "Years", sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillYears(template) }
    }

    /// Years, newest first.
    private func fillYears(_ template: CPListTemplate) async {
        let api = model.api
        do {
            var counts: [Int: Int] = [:]
            for p in try await Self.allPerformances(api) {
                if let y = p.date.flatMap({ Int($0.prefix(4)) }) { counts[y, default: 0] += 1 }
            }
            let items = counts.keys.sorted(by: >).map { y -> CPListItem in
                let n = counts[y]!
                let item = CPListItem(text: String(y), detailText: n == 1 ? "1 performance" : "\(n) performances",
                                      image: nil, accessoryImage: nil, accessoryType: .disclosureIndicator)
                item.handler = { [weak self] _, done in
                    self?.pushPerformances(title: String(y), query: PerformanceQuery(year: String(y)), done: done)
                }
                return item
            }
            show([CPListSection(items: items)], in: template, empty: "No dated performances")
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillYears(template) }
        }
    }

    private func pushAllPerformances(done: @escaping () -> Void) {
        let template = CPListTemplate(title: "All Performances", sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillAllPerformances(template) }
    }

    /// Every performance by title (then date), as A–Z sections or, past the car's item budget, letter pages.
    private func fillAllPerformances(_ template: CPListTemplate) async {
        let api = model.api
        do {
            // By title ignoring a leading article (so each letter page is in order), then work and date.
            let all = Self.uniquePerformances(try await Self.allPerformances(api))
                .map { (key: Self.nameKey($0.title), p: $0) }
                .sorted { a, b in
                    let c = a.key.compare(b.key, options: [.caseInsensitive, .diacriticInsensitive, .numeric])
                    return c != .orderedSame ? c == .orderedAscending : Self.workThenDate(a.p, b.p)
                }
                .map(\.p)
            showPaged(all, key: { Self.nameKey($0.title) }, order: .alphabetical, noun: "performances",
                      in: template, empty: "No performances") { [unowned self] in self.performanceItem($0) }
        } catch {
            showError(error, in: template) { [weak self] in await self?.fillAllPerformances(template) }
        }
    }

    private func pushPeople(title: String, ensembles: Bool, done: @escaping () -> Void) {
        let template = CPListTemplate(title: title, sections: [])
        showLoading(template)
        push(template, done: done)
        Task { await fillPerformers(template, ensembles: ensembles) }
    }

    /// `ensembles`: nil lists everyone (the Performers tab), true only ensembles, false only individuals.
    private func fillPerformers(_ template: CPListTemplate, ensembles: Bool? = nil) async {
        let api = model.api
        let generation = model.generation
        do {
            let fetched = try await api.people(q: nil)
            // Superseded by a server/demo switch: the newer load draws this template.
            guard generation == model.generation else { return }
            let people = fetched
                .filter { ($0.creditCount ?? 1) > 0 }
                .filter { p in ensembles.map { ($0 == (p.kind == "ensemble")) } ?? true }
                .sorted {
                    if let a = $0.creditCount, let b = $1.creditCount, a != b { return a > b }
                    if $0.creditCount != nil, $1.creditCount == nil { return true }
                    if $0.creditCount == nil, $1.creditCount != nil { return false }
                    return Self.sortKey($0.name) < Self.sortKey($1.name)
                }
            showPaged(people, key: { Self.nameKey($0.name, lastName: $0.kind != "ensemble") }, order: .ranked,
                      noun: ensembles == true ? "ensembles" : "performers", in: template,
                      empty: ensembles == true ? "No ensembles" : "No performers") {
                [unowned self] person -> CPListItem in
                let count = person.creditCount.map { $0 == 1 ? "1 performance" : "\($0) performances" }
                let item = CPListItem(text: person.name, detailText: count,
                                      image: placeholder(title: "", seed: person.id), accessoryImage: nil,
                                      accessoryType: .disclosureIndicator)
                loadImage(person.imageUrl, into: item, api: api, mode: .portrait)
                item.handler = { [weak self] _, done in
                    self?.pushPersonPerformances(person, done: done)
                }
                return item
            }
        } catch {
            guard generation == model.generation else { return }
            showError(error, in: template) { [weak self] in await self?.fillPerformers(template, ensembles: ensembles) }
        }
    }

    private func pushPersonPerformances(_ person: PersonSummary, done: @escaping () -> Void) {
        let template = CPListTemplate(title: person.name, sections: [])
        showLoading(template)
        push(template, done: done)
        let api = model.api
        Task {
            do {
                let detail = try await api.person(id: person.id)
                let performances = Self.uniquePerformances(detail.credits.map(\.performance))
                show(performanceSections(performances), in: template,
                     empty: "No performances")
            } catch {
                showPersonError(error, template: template, person: person, api: api)
            }
        }
    }

    private func showPersonError(_ error: any Error, template: CPListTemplate, person: PersonSummary,
                                 api: any SethsterAPI) {
        showError(error, in: template) { [weak self] in
            await self?.fillPersonPerformances(template, person: person, api: api)
        }
    }

    private func fillPersonPerformances(_ template: CPListTemplate, person: PersonSummary,
                                        api: any SethsterAPI) async {
        do {
            let detail = try await api.person(id: person.id)
            let performances = Self.uniquePerformances(detail.credits.map(\.performance))
            show(performanceSections(performances), in: template,
                 empty: "No performances")
        } catch {
            showError(error, in: template) { [weak self] in
                await self?.fillPersonPerformances(template, person: person, api: api)
            }
        }
    }

    /// Composer / collection → its performances (one level deep). The template is pushed immediately with a
    /// loading state, then filled.
    private func pushPerformances(title: String, query: PerformanceQuery, done: @escaping () -> Void) {
        let template = CPListTemplate(title: title, sections: [])
        showLoading(template)
        push(template, done: done)
        let api = model.api
        var q = query
        q.sort = "title"   // re-sorted by work then date on the phone; truncation keeps the start of the alphabet
        q.limit = min(200, CPListTemplate.maximumItemCount)
        Task { await self.fill(template, api: api, query: q) }
    }

    private func fill(_ template: CPListTemplate, api: any SethsterAPI, query: PerformanceQuery) async {
        do {
            let items = try await api.performances(query).items
            show(performanceSections(items), in: template, empty: "No performances")
        } catch {
            showError(error, in: template) { [weak self] in await self?.fill(template, api: api, query: query) }
        }
    }

    // MARK: - Performances & playback

    private func playRotation(_ item: RotationItem, done: @escaping () -> Void) {
        let api = model.api
        let epoch = account.epoch
        Task {
            do {
                let perf: PerformanceDetail
                if let cached = rotationStore.cachedPerformance(item.id) {
                    perf = cached
                } else {
                    perf = try await api.performance(id: item.id)
                }
                guard !perf.tracks.isEmpty else { throw CarPlayError.noTracks }
                // Signed out / switched server while fetching: don't refill the queue teardown just cleared.
                guard epoch == account.epoch else { done(); return }
                player.play(perf, api: api)
                done()
                if let message = player.errorMessage { presentAlert(message) } else { showNowPlaying() }
            } catch {
                done()
                presentAlert(Self.message(for: error))
            }
        }
    }

    /// A drill-down list: anything in Heavy Rotation first (in Rotation order), then the rest grouped by work,
    /// oldest performance first within a work.
    private func performanceSections(_ list: [PerformanceSummary]) -> [CPListSection] {
        var order: [String: Int] = [:]
        for (i, item) in rotationStore.items.enumerated() where order[item.id] == nil { order[item.id] = i }
        let pinned = list.filter { order[$0.id] != nil }.sorted { order[$0.id]! < order[$1.id]! }
        let rest = list.filter { order[$0.id] == nil }.sorted(by: Self.workThenDate)
        guard !pinned.isEmpty else { return [CPListSection(items: rest.map(performanceItem))] }
        var sections = [CPListSection(items: pinned.map(performanceItem), header: "Heavy Rotation",
                                      sectionIndexTitle: nil)]
        if !rest.isEmpty {
            sections.append(CPListSection(items: rest.map(performanceItem), header: "All Performances",
                                          sectionIndexTitle: nil))
        }
        return sections
    }

    private func performanceItem(_ p: PerformanceSummary) -> CPListItem {
        let detail = [p.subtitle ?? p.composers.first?.name, Fmt.shortDate(p.date)]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        let item = CPListItem(text: p.title, detailText: detail,
                              image: placeholder(title: p.title, seed: p.collection ?? p.id))
        item.playingIndicatorLocation = .trailing
        item.isPlaying = player.hasQueue && player.performance?.id == p.id
        loadImage(p.artworkUrl, into: item, api: model.api)
        item.handler = { [weak self] _, done in self?.play(performanceID: p.id, track: 0, done: done) }
        performanceItems.add(item, for: p.id)
        return item
    }

    private func themeItem(_ pick: ThemePick) -> CPListItem {
        // One line: the date, then why it's here (the performance title misleads for recitals). The production
        // note follows only when the line stays short enough to read on a car screen.
        var detail = [Fmt.shortDate(pick.performance.date), pick.reason ?? pick.productionNote ?? pick.hint]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        if pick.reason != nil, let note = pick.productionNote, detail.count + note.count <= 110 {
            detail += " " + note
        }
        let item = CPListItem(text: pick.workTitle, detailText: detail,
                              image: placeholder(title: pick.workTitle,
                                                 seed: pick.performance.collection ?? pick.performance.id))
        item.playingIndicatorLocation = .trailing
        item.isPlaying = player.hasQueue && player.performance?.id == pick.performance.id
        loadImage(pick.performance.artworkUrl, into: item, api: model.api)
        item.handler = { [weak self] _, done in
            guard let self else { done(); return }
            let api = self.model.api
            let epoch = self.account.epoch
            Task {
                do {
                    try await OptoberfestStore.play(pick, api: api, player: self.player)
                    done()
                    guard epoch == self.account.epoch else { return }
                    if let message = self.player.errorMessage { self.presentAlert(message) } else { self.showNowPlaying() }
                } catch {
                    done()
                    self.presentAlert(Self.message(for: error))
                }
            }
        }
        performanceItems.add(item, for: pick.performance.id)
        return item
    }

    /// Fetch the full performance and start it on the shared PlayerController, then show Now Playing.
    private func play(performanceID: String, track: Int, done: @escaping () -> Void) {
        let api = model.api
        if api.isDemo {
            // Bundled demo recordings have no audio; don't disturb whatever is playing.
            done()
            presentAlert(PlayerController.offlineMessage)
            return
        }
        let epoch = account.epoch
        Task {
            do {
                let perf = try await api.performance(id: performanceID)
                guard !perf.tracks.isEmpty else { throw CarPlayError.noTracks }
                guard epoch == account.epoch else { done(); return }
                player.play(perf, from: track, api: api)
                done()
                if let message = player.errorMessage {
                    presentAlert(message)
                } else {
                    showNowPlaying()
                }
            } catch {
                done()
                presentAlert(Self.message(for: error))
            }
        }
    }

    private func showNowPlaying() {
        // The shared template may be in the stack only once.
        if interface.topTemplate === nowPlaying || nowPlayingTransitionInFlight { return }
        if interface.templates.contains(where: { $0 === nowPlaying }) {
            nowPlayingTransitionInFlight = true
            interface.pop(to: nowPlaying, animated: true) { [weak self] _, _ in
                self?.nowPlayingTransitionInFlight = false
            }
        } else {
            nowPlayingTransitionInFlight = true
            // Leave room above Now Playing for its Tracks list (audio apps get maxDepth templates in all).
            let pushNowPlaying = { [weak self] in
                guard let self else { return }
                self.interface.pushTemplate(self.nowPlaying, animated: true) { [weak self] _, _ in
                    self?.nowPlayingTransitionInFlight = false
                }
            }
            if interface.templates.count + 2 > Self.maxDepth {
                interface.popToRootTemplate(animated: false) { _, _ in pushNowPlaying() }
            } else {
                pushNowPlaying()
            }
        }
    }

    private func configureNowPlaying() {
        nowPlaying.add(self)
        refreshNowPlayingControls()
    }

    private func refreshNowPlayingControls() {
        // Up Next lists the performance's tracks; radio (seeded or live) has none.
        nowPlaying.isUpNextButtonEnabled = !radio.isActive
        nowPlaying.upNextTitle = "Tracks"
        nowPlaying.isAlbumArtistButtonEnabled = false
    }

    /// The tracks of the playing performance; selecting one jumps there.
    private func pushTracks() {
        guard let perf = player.performance, player.hasQueue, !radio.isActive,
              interface.templates.count < Self.maxDepth else { return }
        let max = CPListTemplate.maximumItemCount
        let items = player.tracks.prefix(max).enumerated().map { offset, t -> CPListItem in
            let item = CPListItem(text: t.title, detailText: t.durationS.map { Fmt.clock($0) })
            item.playingIndicatorLocation = .trailing
            item.isPlaying = offset == player.currentIndex
            item.handler = { [weak self] _, done in
                guard let self else { done(); return }
                if self.player.performance?.id == perf.id {
                    self.player.jump(to: offset)
                    done()
                    self.interface.popTemplate(animated: true, completion: nil)
                } else {
                    self.play(performanceID: perf.id, track: offset, done: done)
                }
            }
            return item
        }
        let template = CPListTemplate(title: perf.title, sections: [CPListSection(items: items)])
        interface.pushTemplate(template, animated: true, completion: nil)
    }

    // MARK: - States

    private func showLoading(_ t: CPListTemplate) {
        t.updateSections([])
        t.emptyViewTitleVariants = ["Loading…"]
        t.emptyViewSubtitleVariants = []
    }

    private func show(_ sections: [CPListSection], in t: CPListTemplate, empty: String) {
        t.emptyViewTitleVariants = [empty]
        t.emptyViewSubtitleVariants = []
        t.updateSections(Self.clamp(sections))
    }

    private enum ListOrder { case alphabetical, ranked }

    /// Show a browse list within the car's item budget. If it fits: alphabetical lists get one section per letter
    /// (with the side index), ranked lists stay in rank order. If not: one row per letter, each pushing that
    /// letter's items (A–Z), built only when opened. `elements` arrive in display order; `key` gives the letter
    /// and the A–Z order of a ranked list's letter page.
    private func showPaged<T>(_ elements: [T], key: @escaping (T) -> String, order: ListOrder, noun: String,
                              in template: CPListTemplate, empty: String,
                              row: @escaping (T) -> CPListItem) {
        let groups = Self.letterGroups(elements, key: key)
        if elements.count <= CPListTemplate.maximumItemCount {
            if order == .alphabetical, groups.count > 1, groups.count <= CPListTemplate.maximumSectionCount {
                show(groups.map {
                    CPListSection(items: $0.items.map(row), header: $0.letter, sectionIndexTitle: $0.letter)
                }, in: template, empty: empty)
            } else {
                show([CPListSection(items: elements.map(row))], in: template, empty: empty)
            }
            return
        }
        let title = template.title ?? ""
        let letters = groups.map { group -> CPListItem in
            let shown = min(group.items.count, CPListTemplate.maximumItemCount)
            let detail = shown < group.items.count ? "First \(shown) of \(group.items.count) \(noun)"
                : "\(group.items.count) \(noun)"
            let item = CPListItem(text: group.letter, detailText: detail, image: nil,
                                  accessoryImage: nil, accessoryType: .disclosureIndicator)
            item.handler = { [weak self] _, done in
                guard let self else { done(); return }
                let page = CPListTemplate(title: title.isEmpty ? group.letter : "\(title): \(group.letter)",
                                          sections: [])
                let items = order == .alphabetical ? group.items
                    : group.items.sorted { key($0).localizedStandardCompare(key($1)) == .orderedAscending }
                // Build only what the car will show (rows load images and register for indicators).
                let rows = items.prefix(CPListTemplate.maximumItemCount).map(row)
                self.show([CPListSection(items: rows)], in: page, empty: empty)
                self.push(page, done: done)
            }
            return item
        }
        show([CPListSection(items: letters)], in: template, empty: empty)
    }

    /// Push a list, staying inside the audio-app depth limit (at the limit, start again from the tab's root).
    private func push(_ template: CPTemplate, done: @escaping () -> Void) {
        guard !nowPlayingTransitionInFlight else { done(); return }
        guard interface.templates.count >= Self.maxDepth else {
            interface.pushTemplate(template, animated: true) { _, _ in done() }
            return
        }
        interface.popToRootTemplate(animated: false) { [weak self] _, _ in
            guard let self else { done(); return }
            self.interface.pushTemplate(template, animated: true) { _, _ in done() }
        }
    }

    private func showError(_ error: any Error, in t: CPListTemplate, retry: @escaping @MainActor () async -> Void) {
        let retryItem = CPListItem(text: "Try Again", detailText: Self.message(for: error),
                                   image: UIImage(systemName: "arrow.clockwise"))
        retryItem.handler = { [weak self] _, done in
            done()
            guard let self else { return }
            self.showLoading(t)
            Task {
                let generation = self.model.generation
                await self.model.checkServer()
                // A switch to/from demo data reloads every list via observeModel; otherwise retry this one.
                if self.model.generation == generation { await retry() }
            }
        }
        t.emptyViewTitleVariants = ["Can’t reach the Sethster server"]
        t.emptyViewSubtitleVariants = [Self.message(for: error)]
        t.updateSections([CPListSection(items: [retryItem], header: "Can’t reach the Sethster server",
                                        sectionIndexTitle: nil)])
    }

    private func presentAlert(_ message: String) {
        let alert = CPAlertTemplate(titleVariants: [message], actions: [
            CPAlertAction(title: "OK", style: .cancel) { [weak self] _ in
                self?.interface.dismissTemplate(animated: true, completion: nil)
            },
        ])
        // CarPlay allows one presented template at a time, and dismissal is asynchronous: present only after
        // the previous alert is gone.
        guard interface.presentedTemplate != nil else {
            interface.presentTemplate(alert, animated: true, completion: nil)
            return
        }
        interface.dismissTemplate(animated: false) { [weak self] _, _ in
            guard let self, self.active else { return }
            self.interface.presentTemplate(alert, animated: true, completion: nil)
        }
    }

    /// Trim to CarPlay's section and total-item limits (the system may lower these while driving).
    private static func clamp(_ sections: [CPListSection]) -> [CPListSection] {
        var budget = CPListTemplate.maximumItemCount
        var out: [CPListSection] = []
        for s in sections.prefix(CPListTemplate.maximumSectionCount) where budget > 0 {
            let items = Array(s.items.prefix(budget))
            budget -= items.count
            out.append(CPListSection(items: items, header: s.header, sectionIndexTitle: s.sectionIndexTitle))
        }
        return out
    }

    // MARK: - Images

    /// The app's generated duotone cover, rendered at CarPlay's list-image size.
    private func placeholder(title: String, seed: String) -> UIImage? {
        let key = seed + "|" + title
        if let img = placeholderCache[key] { return img }
        let size = CPListItem.maximumImageSize.width
        let scale = interface.carTraitCollection.displayScale
        let img = ArtworkRenderer.placeholder(title: title, seed: seed, size: size, scale: scale > 0 ? scale : 2)
        placeholderCache[key] = img
        return img
    }

    private enum ThumbnailMode: Equatable { case artwork, portrait }

    private func loadImage(_ path: String?, into item: CPListItem, api: any SethsterAPI,
                           mode: ThumbnailMode = .artwork) {
        guard let url = api.resolve(path) else { return }
        let headers = path?.hasPrefix("http") == true ? [:] : api.authHeaders
        let side = CPListItem.maximumImageSize.width * max(interface.carTraitCollection.displayScale, 2)
        Task {
            guard let img = await ImageLoader.shared.image(for: url, headers: headers) else { return }
            item.setImage(Self.squareThumbnail(img, side: side, mode: mode))
        }
    }

    private static func squareThumbnail(_ img: UIImage, side: CGFloat, mode: ThumbnailMode) -> UIImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: side, height: side), format: format).image { _ in
            let s = img.size
            guard s.width > 0, s.height > 0 else { return }
            if mode == .artwork {
                let k = max(side / s.width, side / s.height)
                let w = s.width * k, h = s.height * k
                img.draw(in: CGRect(x: (side - w) / 2, y: (side - h) / 2, width: w, height: h))
            } else {
                let short = min(s.width, s.height)
                let long = max(s.width, s.height)
                let keptLong = short * (1 + (long / short - 1) / 2)
                let crop: CGRect
                if s.width >= s.height {
                    crop = CGRect(x: (s.width - keptLong) / 2, y: 0, width: keptLong, height: s.height)
                } else {
                    let removed = s.height - keptLong
                    crop = CGRect(x: 0, y: removed / 3, width: s.width, height: keptLong)
                }
                // Map the retained rectangle non-uniformly into the square. The renderer clips the discarded
                // area, leaving half the original aspect correction to cropping and half to stretching.
                img.draw(in: CGRect(x: -crop.minX * side / crop.width,
                                    y: -crop.minY * side / crop.height,
                                    width: s.width * side / crop.width,
                                    height: s.height * side / crop.height))
            }
        }
    }

    // MARK: - Helpers

    private static func sortKey(_ name: String) -> String {
        (name.split(separator: " ").last.map(String.init) ?? name).folding(options: .diacriticInsensitive, locale: nil)
    }

    private static let rotationPreviewCount = 8
    private static let radioPreviewCount = 8
    /// CarPlay audio apps may stack at most five templates, the root included.
    private static let maxDepth = 5

    /// A–Z key for a name: people by last name (as `sortKey`); ensembles, venues and titles without a leading
    /// article.
    private static func nameKey(_ name: String, lastName: Bool = false) -> String {
        if lastName { return sortKey(name) }
        var n = name.trimmingCharacters(in: .whitespaces)
        for article in ["The ", "A ", "An "] where n.hasPrefix(article) && n.count > article.count {
            n = String(n.dropFirst(article.count)); break
        }
        return n.folding(options: .diacriticInsensitive, locale: nil)
    }

    /// Group in input order by first letter (A–Z; anything else under "#", listed last).
    private static func letterGroups<T>(_ elements: [T], key: (T) -> String) -> [(letter: String, items: [T])] {
        var groups: [String: [T]] = [:]
        for e in elements {
            let first = key(e).folding(options: [.diacriticInsensitive, .caseInsensitive], locale: nil)
                .uppercased().first
            let letter = first.map { $0.isASCII && $0.isLetter ? String($0) : "#" } ?? "#"
            groups[letter, default: []].append(e)
        }
        return groups.keys.sorted { ($0 == "#" ? "~" : $0) < ($1 == "#" ? "~" : $1) }.map { ($0, groups[$0]!) }
    }

    /// Every performance, a page of 1,000 at a time (the server's cap), for the Venues and Years indexes.
    private static func allPerformances(_ api: any SethsterAPI) async throws -> [PerformanceSummary] {
        var out: [PerformanceSummary] = []
        while true {
            let page = try await api.performances(PerformanceQuery(sort: "title", limit: 1000, offset: out.count))
            out += page.items
            if page.items.isEmpty || out.count >= (page.total ?? out.count) { return out }
        }
    }

    private static func workThenDate(_ a: PerformanceSummary, _ b: PerformanceSummary) -> Bool {
        let t = a.title.compare(b.title, options: [.caseInsensitive, .diacriticInsensitive, .numeric])
        if t != .orderedSame { return t == .orderedAscending }
        switch (a.date, b.date) {
        case let (x?, y?): return x < y
        case (_?, nil): return true
        default: return false
        }
    }

    private static func uniquePerformances(_ performances: [PerformanceSummary]) -> [PerformanceSummary] {
        var seen = Set<String>()
        return performances.filter { seen.insert($0.id).inserted }
    }

    private static func message(for error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}

/// Rows on screen for each id, held weakly: a row goes away with the template that shows it.
private struct RowIndex {
    private var rows: [String: NSHashTable<CPListItem>] = [:]

    mutating func add(_ item: CPListItem, for id: String) {
        rows[id, default: .weakObjects()].add(item)
    }

    mutating func remove(_ items: [CPListItem]) {
        for table in rows.values { items.forEach(table.remove) }
    }

    func forEach(_ body: (String, CPListItem) -> Void) {
        for (id, table) in rows { table.allObjects.forEach { body(id, $0) } }
    }
}

private enum CarPlayError: LocalizedError {
    case noTracks
    var errorDescription: String? { "This performance has no tracks." }
}

extension CarPlayController: CPTabBarTemplateDelegate {
    nonisolated func tabBarTemplate(_ tabBarTemplate: CPTabBarTemplate, didSelect selectedTemplate: CPTemplate) {
        MainActor.assumeIsolated {
            // Pick up stations created on the phone (or by a preset here) since the tab last loaded.
            guard active, showingContent, selectedTemplate === radioTab else { return }
            Task { await loadRadio() }
        }
    }
}

extension CarPlayController: CPNowPlayingTemplateObserver {
    nonisolated func nowPlayingTemplateUpNextButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {
        MainActor.assumeIsolated { pushTracks() }
    }

    nonisolated func nowPlayingTemplateAlbumArtistButtonTapped(_ nowPlayingTemplate: CPNowPlayingTemplate) {}
}

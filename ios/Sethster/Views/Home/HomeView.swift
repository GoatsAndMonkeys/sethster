import SwiftUI

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @Environment(AccountStore.self) private var account
    @State private var showSettings = false

    var body: some View {
        Loader(load: { try await $0.home() }) { home in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    masthead
                    if model.isOfflineFallback { offlineBanner } else if model.api.isDemo, !model.browsingDemo { demoBanner }

                    OptoberfestSection()

                    if let hero = home.recent.first {
                        NavigationLink(value: Route.performance(hero.id)) { HeroCard(p: hero) }
                            .buttonStyle(.plain)
                            .padding(.horizontal)
                            .padding(.top, 8)
                    }

                    if home.recent.count > 1 {
                        SectionTitle("Recently Completed")
                        shelf(Array(home.recent.dropFirst()))
                    }

                    if let s = home.spotlight {
                        SectionTitle(s.isRemembering ? "Remembering" : "Today's Spotlight")
                        NavigationLink(value: s.route) { SpotlightCard(s: s) }
                            .buttonStyle(.plain)
                            .padding(.horizontal)
                        if !s.items.isEmpty { shelf(s.items).padding(.top, 14) }
                    }

                    if History.client(for: model.api) != nil {
                        // On This Day card + calendar shelves (docs/CALENDAR.md)
                        OnThisDaySection()
                        CalendarShelvesSection()
                    } else if !home.onThisDay.isEmpty {
                        SectionTitle("On This Day", trailing: Date.now.formatted(.dateTime.day().month(.wide)))
                        VStack(spacing: 0) {
                            ForEach(home.onThisDay) { p in
                                NavigationLink(value: Route.performance(p.id)) {
                                    PerformanceRow(p: p).padding(.horizontal).padding(.vertical, 8)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }

                    if !home.composers.isEmpty {
                        SectionTitle("Composers")
                        ScrollView(.horizontal, showsIndicators: false) {
                            LazyHStack(alignment: .top, spacing: 18) {
                                ForEach(home.composers) { c in
                                    NavigationLink(value: Route.composer(c.id)) { ComposerBubble(c: c) }
                                        .buttonStyle(.plain)
                                }
                            }
                            .padding(.horizontal)
                        }
                    }
                    Spacer(minLength: 40)
                }
            }
            .refreshable { model.reload() }
        }
        .background(Color.paper)
        .sheet(isPresented: $showSettings) { SettingsView() }
        .navigationTitle("Home")
        .toolbar(.hidden, for: .navigationBar)
    }

    private var masthead: some View {
        HStack(alignment: .center, spacing: 10) {
            Image("Logo").resizable().scaledToFit().frame(width: 40, height: 40)
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            Text("Sethster").font(.display(34, weight: .bold))
            Spacer()
            Button { showSettings = true } label: {
                Image(systemName: "gearshape").font(.uiTitle3).foregroundStyle(.secondary)
                    .overlay(alignment: .topTrailing) {
                        if account.pendingApprovals > 0 {
                            Text("\(account.pendingApprovals)").font(.uiCaption2.weight(.bold)).foregroundStyle(.white)
                                .padding(.horizontal, 5).frame(minWidth: 17, minHeight: 17)
                                .background(Color.red, in: Capsule())
                                .offset(x: 9, y: -7)
                        }
                    }
            }
            .accessibilityLabel(account.pendingApprovals > 0 ? "Settings, \(account.pendingApprovals) waiting for approval" : "Settings")
        }
        .padding(.horizontal)
        .padding(.top, 8)
        .padding(.bottom, 4)
    }

    private var demoBanner: some View {
        Label("Showing demo data. Set your server in Settings.", systemImage: "info.circle")
            .font(.uiFootnote)
            .foregroundStyle(.secondary)
            .padding(.horizontal).padding(.bottom, 6)
    }

    /// Shown when the server is unreachable and the app fell back to the bundled demo recordings.
    private var offlineBanner: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "wifi.exclamationmark").foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 6) {
                Text("Can’t reach the Sethster server — showing demo recordings")
                    .font(.uiFootnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 16) {
                    Button {
                        Task { await model.checkServer() }
                    } label: {
                        HStack(spacing: 4) {
                            Text("Retry")
                            if model.isCheckingServer { ProgressView().controlSize(.mini) }
                        }
                    }
                    .disabled(model.isCheckingServer)
                    Button("Settings") { showSettings = true }
                }
                .font(.uiFootnote.weight(.semibold))
                .buttonStyle(.borderless)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .padding(.horizontal).padding(.bottom, 8)
        .accessibilityElement(children: .contain)
    }

    private func shelf(_ items: [PerformanceSummary]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(alignment: .top, spacing: 14) {
                ForEach(items) { p in
                    NavigationLink(value: Route.performance(p.id)) { PerformanceCard(p: p) }
                        .buttonStyle(.plain)
                }
            }
            .padding(.horizontal)
        }
    }
}

/// Large feature card for the newest performance.
struct HeroCard: View {
    let p: PerformanceSummary

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            // The image sits in an overlay so a wide scaledToFill picture can't widen the card (in a flexible
            // frame it did, pushing Home off the left edge of the screen).
            Color.clear
                .frame(maxWidth: .infinity)
                .frame(height: 260)
                .overlay { RemoteImage(path: p.artworkUrl) { GeneratedCover(title: "", seed: p.collection ?? p.id) } }
                .clipped()
            LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 4) {
                Text("LATEST")
                    .font(.uiCaption2.weight(.bold)).tracking(1.6).foregroundStyle(.white.opacity(0.8))
                Text(p.title).font(.display(30, weight: .semibold)).foregroundStyle(.white).lineLimit(2)
                if let sub = p.subtitle { Text(sub).font(.uiSubheadline).foregroundStyle(.white.opacity(0.9)).lineLimit(1) }
                Text([Fmt.date(p.date), p.venue?.name].compactMap { $0 }.joined(separator: " · "))
                    .font(.uiCaption).foregroundStyle(.white.opacity(0.75)).lineLimit(1)
            }
            .padding(18)
        }
        .frame(height: 260)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        // Hit testing ignores clipping: without this the overflowing cover took taps over the masthead gear.
        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
    }
}

/// Today's Spotlight: one composer, performer, venue or city with a few broadcasts, or "Remembering <name>"
/// when someone in the archive died recently. Tapping opens that person, venue or city.
struct SpotlightCard: View {
    let s: HomeSpotlight

    private var kindLabel: String {
        switch s.kind {
        case "composer": "Composer"
        case "venue": "Venue"
        case "city": "City"
        default: "Performer"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            art.frame(width: 76, height: 76)
            VStack(alignment: .leading, spacing: 4) {
                Text(kindLabel.uppercased())
                    .font(.lexend(11, weight: .semibold, relativeTo: .caption2)).tracking(1.3)
                    .foregroundStyle(s.isRemembering ? Color.ember : Color.graphite)
                Text(s.name).font(.display(24, weight: .semibold)).lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                if let sub = s.subtitle, !sub.isEmpty {
                    Text(sub).font(.uiFootnote).foregroundStyle(.secondary).lineLimit(2)
                }
                if let b = s.blurb, !b.isEmpty {
                    Text(b).font(.uiSubheadline).foregroundStyle(.secondary).lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.uiFootnote.weight(.semibold)).foregroundStyle(.tertiary)
                .padding(.top, 4)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.rule, lineWidth: 0.75))
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel([s.title ?? s.name, s.subtitle].compactMap { $0 }.joined(separator: ", "))
    }

    @ViewBuilder private var art: some View {
        switch s.kind {
        case "composer", "performer":
            Portrait(path: s.imageUrl, name: s.name, size: 76)
        default:
            ArtworkView(path: s.imageUrl ?? s.items.first?.artworkUrl, title: "", seed: s.id, cornerRadius: 10)
        }
    }
}

struct ComposerBubble: View {
    let c: ComposerSummary
    var body: some View {
        VStack(spacing: 6) {
            Portrait(path: c.imageUrl, name: c.name, size: 84)
            Text(c.name.split(separator: " ").last.map(String.init) ?? c.name)
                .font(.display(15, weight: .medium)).lineLimit(1)
            if let n = c.performanceCount {
                Text("\(n) \(n == 1 ? "performance" : "performances")").font(.uiCaption2).foregroundStyle(.secondary)
            }
        }
        .frame(width: 92)
    }
}

struct CollectionTile: View {
    let c: CollectionInfo
    var body: some View {
        let (a, b) = Palette.pair(for: c.name)
        VStack(alignment: .leading, spacing: 2) {
            Spacer()
            Text(c.name).font(.display(19, weight: .semibold)).foregroundStyle(.white).lineLimit(2)
            if let n = c.count { Text("\(n) recordings").font(.uiCaption).foregroundStyle(.white.opacity(0.8)) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, minHeight: 84, alignment: .leading)
        .background(LinearGradient(colors: [b, a], startPoint: .topTrailing, endPoint: .bottomLeading),
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

#Preview {
    NavigationStack { HomeView().withRoutes() }
        .environment(AppModel.preview)
        .environment(PlayerController())
}


/// October only: today's spooky pick (Play plays just that work) and a "More for Optoberfest" shelf.
struct OptoberfestSection: View {
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var store = OptoberfestStore.shared
    @State private var playError: String?
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        // A VStack (not Group): an empty Group has no view, so its .task would never run.
        VStack(alignment: .leading, spacing: 0) {
            if let pick = store.today {
                SectionTitle(OptoberfestStore.headline,
                             trailing: OptoberfestStore.themeDate.formatted(.dateTime.day().month(.wide)))
                card(pick).padding(.horizontal)
                if !store.more.isEmpty {
                    SectionTitle("More for Optoberfest")
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(alignment: .top, spacing: 14) {
                            ForEach(store.more) { m in
                                NavigationLink(value: Route.performance(m.performance.id)) { PerformanceCard(p: m.performance) }
                                    .buttonStyle(.plain)
                            }
                        }
                        .padding(.horizontal)
                    }
                }
            }
        }
        .task(id: model.generation) { await store.load(api: model.api) }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await store.load(api: model.api) } }   // new day → new pick
        }
    }

    private func card(_ pick: ThemePick) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(OptoberfestStore.isHalloween() ? "TONIGHT'S TALE" : "TODAY'S SPOOKY PICK")
                .font(.uiCaption2.weight(.bold)).tracking(1.6).foregroundStyle(Color.ember)
            NavigationLink(value: Route.performance(pick.performance.id)) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(pick.workTitle).font(.display(24, weight: .semibold)).lineLimit(2)
                    if let c = pick.composer { Text(c).font(.uiSubheadline).foregroundStyle(.secondary) }
                    Text([pick.fromLabel, Fmt.date(pick.performance.date), pick.performance.venue?.name]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.uiCaption).foregroundStyle(.secondary).lineLimit(2)
                    if let reason = pick.reason {
                        Text(reason).font(.uiFootnote).foregroundStyle(.secondary).lineLimit(3)
                            .padding(.top, 2)
                    }
                    if let note = pick.productionNote {
                        Text(note).font(.uiFootnote).italic().foregroundStyle(.secondary).lineLimit(3)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if let hint = pick.hint {
                Text("Listen for: \(hint)").font(.uiFootnote).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                Button {
                    Task {
                        do { try await OptoberfestStore.play(pick, api: model.api, player: player); playError = nil }
                        catch { playError = "Couldn't start playback." }
                    }
                } label: { Label("Play", systemImage: "play.fill") }
                    .buttonStyle(.borderedProminent).tint(Color.ember)
                NavigationLink(value: Route.linerNotes(pick.performance.id)) { Label("Liner notes", systemImage: "book") }
                    .buttonStyle(.bordered)
            }
            .font(.uiSubheadline.weight(.semibold))
            if let playError { Text(playError).font(.uiFootnote).foregroundStyle(.red) }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

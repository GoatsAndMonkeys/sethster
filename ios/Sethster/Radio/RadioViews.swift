import SwiftUI

// MARK: - Radio tab

struct RadioView: View {
    enum Section: String, CaseIterable, Identifiable { case live = "Live", stations = "Stations"; var id: String { rawValue } }
    /// Opens on Live each time the Radio tab appears (user request); Stations is one tap away.
    @State private var section: Section = .live

    var body: some View {
        VStack(spacing: 0) {
            Picker("Radio", selection: $section) {
                ForEach(Section.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal).padding(.vertical, 8)
            switch section {
            case .stations: SeededStationsView()
            case .live: LiveStationsView()
            }
        }
        .background(Color.paper)
        .navigationTitle("Radio")
        .onAppear { section = .live }
    }
}

// MARK: - Seeded stations

struct SeededStationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(RadioPlayer.self) private var radio
    @State private var query = ""
    @State private var type: RadioSeedType?
    @State private var seeds: [RadioSeed] = []
    @State private var recent: [RadioStation] = []
    @State private var error: String?
    @State private var loading = false

    var body: some View {
        List {
            if RadioClient(api: model.api) == nil {
                ContentUnavailableView("Collection radio needs the server", systemImage: "antenna.radiowaves.left.and.right.slash",
                                       description: Text("Stations shuffle arias and highlights from the family recordings. Live stations still work."))
            } else {
                if query.isEmpty, !recent.isEmpty {
                    SwiftUI.Section("Your stations") {
                        ForEach(recent) { st in
                            Button { radio.start(station: st.id, name: st.name, api: model.api); radio.showNowPlaying = true } label: {
                                StationRow(title: st.name, subtitle: st.playCount.map { "\($0) played" },
                                           icon: "dot.radiowaves.left.and.right", isOn: radio.stationId == st.id)
                            }
                            .buttonStyle(.plain)
                            .swipeActions {
                                Button("Delete", role: .destructive) { delete(st) }
                            }
                        }
                    }
                }
                SwiftUI.Section {
                    Picker("Kind", selection: $type) {
                        Text("All").tag(RadioSeedType?.none)
                        ForEach(RadioSeedType.allCases) { Text($0.label).tag(Optional($0)) }
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
                    if let error { Text(error).font(.uiFootnote).foregroundStyle(Color.ember) }
                    ForEach(seeds) { s in
                        Button { radio.start(seed: s, api: model.api); radio.showNowPlaying = true } label: {
                            SeedRow(seed: s, isOn: radio.stationId == s.stationId)
                        }
                        .buttonStyle(.plain)
                    }
                    if seeds.isEmpty, !loading, error == nil {
                        Text(query.isEmpty ? "No stations yet." : "No composer, performer or ensemble matches “\(query)”.")
                            .font(.uiFootnote).foregroundStyle(.secondary)
                    }
                } header: {
                    Text(query.isEmpty ? "Start a station" : "Matches")
                } footer: {
                    Text("A station plays single arias, duets, ensembles, overtures and movements, favouring famous numbers, never the same work twice in a row.")
                }
            }
        }
        .scrollContentBackground(.hidden)
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                    prompt: "Composer, singer, conductor, orchestra…")
        .task(id: "\(model.generation)|\(query)|\(type?.rawValue ?? "")") { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        guard let client = RadioClient(api: model.api) else { return }
        if !query.isEmpty { try? await Task.sleep(for: .milliseconds(250)) }
        guard !Task.isCancelled else { return }
        loading = true
        defer { loading = false }
        do {
            async let s = client.seeds(q: query.isEmpty ? nil : query, type: type)
            async let r = client.stations()
            seeds = try await s
            recent = (try? await r) ?? recent
            error = nil
        } catch is CancellationError {
        } catch let e as URLError where e.code == .cancelled {
        } catch {
            self.error = RadioPlayer.describe(error)
        }
    }

    private func delete(_ st: RadioStation) {
        guard let client = RadioClient(api: model.api) else { return }
        recent.removeAll { $0.id == st.id }
        Task { try? await client.deleteStation(st.id) }
    }
}

private struct SeedRow: View {
    let seed: RadioSeed
    var isOn = false
    var body: some View {
        HStack(spacing: 12) {
            Portrait(path: seed.imageUrl, name: seed.name, size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(seed.name) Radio").font(.display(16, weight: .medium)).lineLimit(1)
                Text([seed.subtitle, countLabel].compactMap { $0 }.joined(separator: " · "))
                    .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 6)
            Image(systemName: isOn ? "waveform" : seed.type.icon)
                .foregroundStyle(isOn ? AnyShapeStyle(.tint) : AnyShapeStyle(.tertiary))
                .symbolEffect(.variableColor.iterative, isActive: isOn)
        }
        .contentShape(Rectangle())
    }
    private var countLabel: String? {
        guard let n = seed.excerptCount else { return nil }
        let f = seed.famousCount ?? 0
        return f > 0 ? "\(n) excerpts, \(f) famous" : "\(n) excerpts"
    }
}

private struct StationRow: View {
    let title: String
    let subtitle: String?
    let icon: String
    var isOn = false
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.uiTitle3).frame(width: 44, height: 44)
                .background(Circle().fill(.quaternary)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.display(16, weight: .medium)).lineLimit(1)
                if let subtitle { Text(subtitle).font(.uiCaption).foregroundStyle(.secondary) }
            }
            Spacer()
            if isOn { Image(systemName: "waveform").foregroundStyle(.tint).symbolEffect(.variableColor.iterative) }
        }
        .contentShape(Rectangle())
    }
}

// MARK: - Live stations

struct LiveStationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(RadioPlayer.self) private var radio
    @State private var stations: [LiveStation] = BundledLiveStations.all
    @State private var query = ""
    @State private var operaOnly = false

    private var shown: [LiveStation] {
        stations.filter { s in
            (!operaOnly || s.airsOpera == true || s.tags.contains("opera"))
                && (query.isEmpty || [s.name, s.broadcaster ?? "", s.country ?? "", s.city ?? "", s.description ?? ""]
                    .joined(separator: " ").localizedCaseInsensitiveContains(query))
        }
    }

    var body: some View {
        List {
            Toggle("Concert & opera broadcasts", isOn: $operaOnly).font(.uiSubheadline)
            ForEach(shown) { s in
                Button { radio.start(live: s, api: model.api); radio.showNowPlaying = true } label: {
                    LiveRow(station: s, isOn: radio.live?.id == s.id)
                }
                .buttonStyle(.plain)
            }
        }
        .scrollContentBackground(.hidden)
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Station, country…")
        .task(id: model.generation) {
            if let c = RadioClient(api: model.api), let s = try? await c.live(), !s.isEmpty { stations = s }
        }
    }
}

private struct LiveRow: View {
    let station: LiveStation
    var isOn = false
    var body: some View {
        HStack(spacing: 12) {
            StationLogo(url: station.logoUrl, name: station.name, size: 48)
            VStack(alignment: .leading, spacing: 2) {
                Text(station.name).font(.display(16, weight: .medium)).lineLimit(1)
                Text([station.city, station.country].compactMap { $0 }.joined(separator: ", "))
                    .font(.uiCaption).foregroundStyle(.secondary)
                if let d = station.description {
                    Text(d).font(.uiCaption).foregroundStyle(.tertiary).lineLimit(2)
                }
            }
            Spacer(minLength: 6)
            if isOn {
                Image(systemName: "waveform").foregroundStyle(.tint).symbolEffect(.variableColor.iterative)
            } else if station.airsOpera == true {
                Image(systemName: "theatermasks").foregroundStyle(.tertiary).accessibilityLabel("Airs opera")
            }
        }
        .contentShape(Rectangle())
    }
}

/// A station logo from an absolute URL (no auth header), with an initials fallback.
struct StationLogo: View {
    let url: String?
    let name: String
    var size: CGFloat = 44
    var corner: CGFloat = 8
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image).resizable().scaledToFit().padding(size * 0.06).background(.white)
            } else {
                GeneratedCover(title: "", seed: name)
                Text(Fmt.initials(name)).font(.custom(Font.lexendFamily, fixedSize: size * 0.3).weight(.medium))
                    .foregroundStyle(.white)
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: corner, style: .continuous).strokeBorder(.primary.opacity(0.08), lineWidth: 0.5) }
        .task(id: url) {
            guard let s = url, let u = URL(string: s) else { image = nil; return }
            image = await ImageLoader.shared.image(for: u, headers: [:])
        }
    }
}

// MARK: - Mini player

/// The tab-bar accessory: Radio when it's on, otherwise the performance player.
struct AnyMiniPlayer: View {
    @Environment(RadioPlayer.self) private var radio
    var body: some View {
        if radio.isActive { RadioMiniPlayer() } else { MiniPlayer() }
    }
}

struct RadioMiniPlayer: View {
    @Environment(RadioPlayer.self) private var radio

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if let live = radio.live {
                    StationLogo(url: live.logoUrl, name: live.name, size: 34, corner: 5)
                } else {
                    ArtworkView(path: radio.artworkPath, title: radio.title, seed: radio.stationId ?? "radio", cornerRadius: 5)
                        .frame(width: 34, height: 34)
                }
            }
            VStack(alignment: .leading, spacing: 0) {
                Text(radio.title).font(.uiSubheadline.weight(.medium)).lineLimit(1)
                Text(radio.isLive ? radio.subtitle : (radio.stationName ?? radio.subtitle))
                    .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            Button { radio.togglePlayPause() } label: {
                Image(systemName: radio.isPlaying ? (radio.isLive ? "stop.fill" : "pause.fill") : "play.fill")
                    .font(.uiTitle3).frame(width: 36, height: 36).contentShape(Rectangle())
            }
            .accessibilityLabel(radio.isPlaying ? "Pause" : "Play")
            if !radio.isLive {
                Button { radio.skip() } label: {
                    Image(systemName: "forward.fill").font(.uiBody).frame(width: 32, height: 36).contentShape(Rectangle())
                }
                .accessibilityLabel("Skip")
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 10)
        .contentShape(Rectangle())
        .onTapGesture { radio.showNowPlaying = true }
    }
}

// MARK: - Now Playing (Radio)

struct RadioNowPlayingView: View {
    @Environment(RadioPlayer.self) private var radio
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @State private var scrub: Double = 0
    @State private var scrubbing = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    art.padding(.top, 20)
                    header
                    if !radio.isLive { scrubber }
                    controls
                    if let msg = radio.errorMessage {
                        Label(msg, systemImage: "exclamationmark.circle").font(.uiFootnote).foregroundStyle(Color.ember)
                            .padding(.horizontal)
                    }
                    if let ex = radio.current { tieIns(ex) }
                    if let live = radio.live { liveInfo(live) }
                    if !radio.upcoming.isEmpty { upNext }
                }
                .padding(.bottom, 40)
            }
            .background(Color.paper)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text(radio.isLive ? "Live Radio" : (radio.stationName ?? "Radio")).font(.uiHeadline).lineLimit(1)
                }
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Button(role: .destructive) { radio.stop(); dismiss() } label: { Image(systemName: "power") }
                        .accessibilityLabel("Turn radio off")
                }
            }
            .withRoutes()
        }
    }

    @ViewBuilder private var art: some View {
        if let live = radio.live {
            StationLogo(url: live.logoUrl, name: live.name, size: 240, corner: 16)
                .shadow(color: .black.opacity(0.2), radius: 20, y: 10)
        } else {
            ArtworkView(path: radio.artworkPath, title: radio.current?.performance?.title ?? radio.title,
                        seed: radio.current?.performance?.collection ?? radio.stationId ?? "radio", cornerRadius: 14)
                .frame(maxWidth: 300)
                .shadow(color: .black.opacity(0.25), radius: 24, y: 12)
                .scaleEffect(radio.isPlaying ? 1 : 0.93)
                .animation(.spring(duration: 0.45), value: radio.isPlaying)
        }
    }

    private var header: some View {
        VStack(spacing: 6) {
            if let live = radio.live {
                Eyebrow("Live · \(live.name)")
                if let np = radio.liveNow, !np.isEmpty {
                    ForEach(Array(np.lines.enumerated()), id: \.offset) { i, line in
                        Text(line).font(i == 0 ? .display(22, weight: .semibold) : .uiSubheadline)
                            .foregroundStyle(i == 0 ? .primary : .secondary).multilineTextAlignment(.center)
                    }
                } else {
                    Text(live.name).font(.display(24, weight: .semibold))
                    if let d = live.description { Text(d).font(.uiSubheadline).foregroundStyle(.secondary).multilineTextAlignment(.center) }
                }
            } else if let ex = radio.current {
                HStack(spacing: 6) {
                    if let h = ex.highlight {
                        Label(h.tier == 1 ? "Famous" : "Highlight", systemImage: "star.fill")
                            .font(.uiCaption2.weight(.semibold)).foregroundStyle(Color.ember)
                    }
                    if let k = ex.kindLabel { Eyebrow(k) }
                }
                Text(ex.title).font(.display(24, weight: .semibold)).multilineTextAlignment(.center).lineLimit(3)
                Text([ex.composer?.name, ex.work?.title].compactMap { $0 }.joined(separator: " · "))
                    .font(.display(17)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                if !ex.performerLine.isEmpty {
                    Text(ex.performerLine).font(.uiSubheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                if let p = ex.performance {
                    Text([Fmt.date(p.date), p.venue?.name].compactMap { $0 }.joined(separator: " · "))
                        .font(.uiCaption).foregroundStyle(.tertiary)
                }
            } else {
                Text(radio.isLoading ? "Tuning in…" : (radio.stationName ?? "Radio")).font(.display(22, weight: .semibold))
                if radio.isLoading { ProgressView() }
            }
        }
        .padding(.horizontal)
    }

    private var scrubber: some View {
        let duration = max(radio.duration, 1)
        return VStack(spacing: 4) {
            Slider(value: Binding(get: { scrubbing ? scrub : min(radio.elapsed, duration) }, set: { scrub = $0 }),
                   in: 0...duration) { editing in
                if editing { scrub = radio.elapsed; scrubbing = true } else { radio.seek(to: scrub); scrubbing = false }
            }
            HStack {
                Text(Fmt.clock(scrubbing ? scrub : radio.elapsed))
                Spacer()
                if radio.isBuffering { ProgressView().controlSize(.mini) }
                Spacer()
                Text("-" + Fmt.clock(duration - (scrubbing ? scrub : radio.elapsed)))
            }
            .font(.uiCaption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 24)
    }

    private var controls: some View {
        HStack(spacing: 36) {
            Button { radio.togglePlayPause() } label: {
                Image(systemName: radio.isPlaying ? (radio.isLive ? "stop.circle.fill" : "pause.circle.fill") : "play.circle.fill")
                    .font(.system(size: 64)).symbolRenderingMode(.hierarchical)
            }
            .accessibilityLabel(radio.isPlaying ? "Pause" : "Play")
            if !radio.isLive {
                Button { radio.skip() } label: { Image(systemName: "forward.end.fill").font(.uiTitle2) }
                    .accessibilityLabel("Skip")
                    .disabled(radio.current == nil)
            }
            RoutePicker().frame(width: 32, height: 32)
        }
        .buttonStyle(.plain)
        .foregroundStyle(.primary)
    }

    /// Liner notes for what's playing, the work's Versions/Compare view, and the same number elsewhere.
    @ViewBuilder private func tieIns(_ ex: RadioExcerpt) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if let p = ex.performance {
                NavigationLink(value: Route.linerNotes(p.id)) {
                    TieInRow(icon: "text.book.closed", title: "Liner notes", detail: p.title)
                }
                Divider().padding(.leading, 52)
                NavigationLink(value: Route.performance(p.id)) {
                    TieInRow(icon: "opticaldisc", title: "Whole performance", detail: p.subtitle ?? p.title)
                }
            }
            if let w = ex.work {
                Divider().padding(.leading, 52)
                NavigationLink(value: Route.work(w.id)) {
                    TieInRow(icon: "square.stack.3d.up", title: "Versions & compare",
                             detail: (ex.versionCount ?? 0) > 1 ? "\(ex.versionCount!) performances of \(w.title ?? "this work")"
                                : (w.title ?? "This work"))
                }
            }
            ForEach(ex.performers.filter { $0.certainty != "maybe" }.prefix(4)) { p in
                Divider().padding(.leading, 52)
                NavigationLink(value: Route.person(p.id)) {
                    TieInRow(icon: p.type == "ensemble" ? "person.3" : "person", title: p.name, detail: p.role)
                }
            }
        }
        .buttonStyle(.plain)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.card))
        .padding(.horizontal)

        if !ex.sameNumber.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Eyebrow("Also in the collection").padding(.horizontal, 24)
                ForEach(ex.sameNumber, id: \.excerptId) { o in
                    Button { radio.playNext(excerptId: o.excerptId) } label: {
                        HStack {
                            Image(systemName: "arrow.triangle.swap").foregroundStyle(.tint)
                            Text(o.title ?? ex.title).font(.uiSubheadline)
                            Spacer()
                            Text(o.performanceId).font(.uiCaption).foregroundStyle(.tertiary).lineLimit(1)
                        }
                        .padding(.horizontal, 24)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        if !ex.reasons.isEmpty {
            Text(ex.reasons.joined(separator: " · ")).font(.uiCaption2).foregroundStyle(.tertiary).padding(.horizontal, 24)
        }
    }

    @ViewBuilder private func liveInfo(_ live: LiveStation) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if let h = live.homepage, let u = URL(string: h) {
                Button { openURL(u) } label: {
                    TieInRow(icon: "safari", title: "Station website", detail: live.broadcaster ?? live.name)
                }
            }
            if let fmt = live.format {
                Divider().padding(.leading, 52)
                TieInRow(icon: "waveform", title: fmt.uppercased() + (live.bitrateKbps.map { " · \($0) kbps" } ?? ""),
                         detail: [live.city, live.country].compactMap { $0 }.joined(separator: ", "), chevron: false)
            }
        }
        .buttonStyle(.plain)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.card))
        .padding(.horizontal)
    }

    private var upNext: some View {
        VStack(alignment: .leading, spacing: 8) {
            Eyebrow("Up next").padding(.horizontal, 24)
            ForEach(radio.upcoming) { ex in
                VStack(alignment: .leading, spacing: 2) {
                    Text(ex.title).font(.uiBody).lineLimit(1)
                    Text([ex.composer?.name, ex.performerLine].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "))
                        .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
                }
                .padding(.horizontal, 24)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct TieInRow: View {
    let icon: String
    let title: String
    let detail: String?
    var chevron = true
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).frame(width: 28).foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.uiBody)
                if let detail, !detail.isEmpty { Text(detail).font(.uiCaption).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer()
            if chevron { Image(systemName: "chevron.right").font(.uiCaption).foregroundStyle(.tertiary) }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .contentShape(Rectangle())
    }
}

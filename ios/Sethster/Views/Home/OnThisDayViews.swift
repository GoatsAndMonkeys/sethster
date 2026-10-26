import SwiftUI

/// Home: "On this day in 1958 …" card (cast, venue, play button, a cited context note), widened to this week
/// when nothing matches; premieres and birthdays on this date as fallbacks. Live server only.
struct OnThisDaySection: View {
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var data: OnThisDayResponse?
    @State private var web: WebLink?
    @State private var starting: String?

    var body: some View {
        Group {
            if let d = data, !(d.items.isEmpty && d.premieres.isEmpty && d.birthdays.isEmpty) {
                VStack(alignment: .leading, spacing: 0) {
                    SectionTitle(d.mode == "week" ? "This Week" : "On This Day", trailing: d.label)
                    if let first = d.items.first {
                        hero(first).padding(.horizontal)
                        ForEach(d.items.dropFirst()) { c in
                            NavigationLink(value: Route.performance(c.performance.id)) {
                                HStack(alignment: .firstTextBaseline, spacing: 10) {
                                    Text(String(c.year)).font(.display(17, weight: .semibold)).foregroundStyle(Color.ember)
                                        .frame(width: 48, alignment: .leading)
                                    PerformanceRow(p: c.performance)
                                }
                                .padding(.horizontal).padding(.vertical, 8)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    if d.items.isEmpty || !d.premieres.isEmpty || !d.birthdays.isEmpty {
                        fallbacks(d).padding(.horizontal).padding(.top, d.items.isEmpty ? 0 : 14)
                    }
                }
                .inAppBrowser($web)
                .linksInAppBrowser($web)
            }
        }
        .task(id: model.generation) { await load() }
    }

    private func load() async {
        guard let client = History.client(for: model.api) else { data = nil; return }
        data = try? await client.onThisDay()
    }

    private func hero(_ c: OnThisDayCard) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            NavigationLink(value: Route.performance(c.performance.id)) {
                HStack(alignment: .top, spacing: 14) {
                    ArtworkView(path: c.performance.artworkUrl, title: c.performance.title,
                                seed: c.performance.collection ?? c.performance.id)
                        .frame(width: 96, height: 96)
                    VStack(alignment: .leading, spacing: 4) {
                        Eyebrow([c.headline, c.yearsAgo.map { "\($0) years ago" }].compactMap { $0 }.joined(separator: " · "))
                        Text(c.performance.title).font(.display(22, weight: .semibold)).lineLimit(2)
                            .multilineTextAlignment(.leading)
                        if !c.cast.isEmpty || c.conductor != nil {
                            Text((c.cast.prefix(3).map { $0.person.name } + [c.conductor.map { "cond. \($0.name)" }].compactMap { $0 })
                                .joined(separator: ", "))
                                .font(.uiSubheadline).foregroundStyle(Color.graphite).lineLimit(2)
                                .multilineTextAlignment(.leading)
                        }
                        Text([Fmt.date(c.date), c.venue?.name].compactMap { $0 }.joined(separator: " · "))
                            .font(.uiCaption).foregroundStyle(Color.graphite).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if let note = c.context {
                VStack(alignment: .leading, spacing: 6) {
                    Text(note.text).font(.uiCallout).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(note.sources.enumerated()), id: \.offset) { _, source in
                            let title = source.title ?? source.id ?? "source"
                            if let url = source.citation.liveURL ?? source.citation.archiveURL {
                                Link(destination: url) {
                                    Text("From \(title)").font(.uiCaption2).underline()
                                }
                            } else {
                                Text("From \(title)").font(.uiCaption2).foregroundStyle(Color.graphite)
                            }
                        }
                        if note.check == "unchecked" {
                            Text("unverified").font(.uiCaption2).foregroundStyle(Color.graphite)
                        }
                    }
                }
            }

            HStack(spacing: 10) {
                Button {
                    Task { await play(c) }
                } label: {
                    HStack(spacing: 6) {
                        if starting == c.id { ProgressView().controlSize(.mini) } else { Image(systemName: "play.fill") }
                        Text("Play")
                    }
                }
                .buttonStyle(.borderedProminent).tint(Color.ember)
                .disabled(c.play == nil || starting != nil)
                NavigationLink(value: Route.linerNotes(c.performance.id)) {
                    Label("Liner notes", systemImage: "book.pages")
                }
                .buttonStyle(.bordered)
            }
            .font(.uiFootnote.weight(.semibold))
        }
        .padding(14)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func play(_ c: OnThisDayCard) async {
        starting = c.id
        defer { starting = nil }
        guard let perf = try? await model.api.performance(id: c.performance.id) else { return }
        let sorted = perf.tracks.sorted { $0.index < $1.index }
        let idx = sorted.firstIndex { $0.id == c.play?.id } ?? 0
        player.play(perf, from: idx, api: model.api)
    }

    @ViewBuilder private func fallbacks(_ d: OnThisDayResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if d.items.isEmpty {
                Text("No recordings in the collection from this date.")
                    .font(.uiFootnote).foregroundStyle(Color.graphite)
            }
            ForEach(d.premieres.prefix(3)) { p in
                NavigationLink(value: Route.work(p.workId)) {
                    fallbackRow(icon: "theatermasks", title: "Premiered \(Fmt.year(p.date) ?? p.date): \(p.title ?? "")",
                                detail: [p.composer?.name, p.place, p.yearsAgo.map { "\($0) years ago" }]
                                    .compactMap { $0 }.joined(separator: " · "))
                }
                .buttonStyle(.plain)
            }
            ForEach(d.birthdays.prefix(4)) { b in
                NavigationLink(value: b.kind == "composer" ? Route.composer(b.id) : Route.person(b.id)) {
                    fallbackRow(icon: "gift", title: "Born on this day: \(b.name)",
                                detail: [Fmt.year(b.date), b.years.map { b.alive == true ? "turns \($0)" : "\($0) years ago" }]
                                    .compactMap { $0 }.joined(separator: " · "))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func fallbackRow(icon: String, title: String, detail: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: icon).foregroundStyle(Color.ember).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.uiSubheadline).multilineTextAlignment(.leading)
                if !detail.isEmpty { Text(detail).font(.uiCaption).foregroundStyle(Color.graphite) }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.uiCaption).foregroundStyle(Color.graphite)
        }
        .contentShape(Rectangle())
    }
}

/// Home shelves from /calendar: In Memoriam, composer of the month, Ring season, anniversaries.
struct CalendarShelvesSection: View {
    @Environment(AppModel.self) private var model
    @State private var shelves: [CalendarShelf] = []
    @State private var web: WebLink?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(shelves) { s in
                SectionTitle(s.title, trailing: s.subtitle)
                if let src = s.sources.first, !src.citation.isEmpty {
                    if let url = src.citation.liveURL ?? src.citation.archiveURL {
                        Link(destination: url) {
                            Text(src.title ?? "Source")
                                .font(.uiCaption).foregroundStyle(Color.graphite).underline().lineLimit(1)
                        }
                        .padding(.horizontal).padding(.bottom, 8)
                    } else {
                        Text(src.title ?? "Source")
                            .font(.uiCaption).foregroundStyle(Color.graphite).lineLimit(1)
                            .padding(.horizontal).padding(.bottom, 8)
                    }
                }
                ScrollView(.horizontal, showsIndicators: false) {
                    LazyHStack(alignment: .top, spacing: 14) {
                        ForEach(s.items) { p in
                            NavigationLink(value: Route.performance(p.id)) { PerformanceCard(p: p) }
                                .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal)
                }
            }
        }
        .inAppBrowser($web)
        .linksInAppBrowser($web)
        .task(id: model.generation) {
            guard let client = History.client(for: model.api) else { shelves = []; return }
            shelves = (try? await client.calendar())?.shelves ?? []
        }
    }
}

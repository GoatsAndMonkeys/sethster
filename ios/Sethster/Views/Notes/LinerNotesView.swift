import SwiftUI

/// The reading view for a performance, laid out like a record's liner notes. Built from PerformanceDetail
/// (always available, also offline) plus `/performances/{id}/liner-notes` when the server has it; sections the
/// server doesn't provide are simply left out.
struct LinerNotesView: View {
    let id: String
    struct Payload: Sendable { let perf: PerformanceDetail; let notes: LinerNotes? }

    var body: some View {
        Loader(load: { api in
            async let p = api.performance(id: id)
            async let n = Self.optional { try await api.linerNotes(performanceId: id) }
            return Payload(perf: try await p, notes: await n)
        }) { LinerNotesContent(perf: $0.perf, notes: $0.notes) }
            .navigationTitle("Liner Notes")
            .navigationBarTitleDisplayMode(.inline)
    }

    /// nil for a missing endpoint or any failure: liner-note extras never block the page.
    static func optional<T: Sendable>(_ f: @Sendable () async throws -> T) async -> T? {
        try? await f()
    }
}

struct LinerNotesContent: View {
    let perf: PerformanceDetail
    let notes: LinerNotes?
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var web: WebLink?
    @State private var showSources = false

    private func open(_ url: URL) { web = WebLink(url: url) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                masthead
                performanceSection
                programNotesSection
                workSection
                HistoricalContextSection(performanceId: perf.id)
                timelineSection
                broadcastSection
                castSection
                sourcesSection
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(.bottom, 48)
        }
        .background(Color.paper)
        .inAppBrowser($web)
        .linksInAppBrowser($web)
    }

    // MARK: Masthead

    private var masthead: some View {
        VStack(alignment: .leading, spacing: 8) {
            Eyebrow(["Liner Notes", perf.collection].compactMap { $0 }.joined(separator: " · "))
            Text(perf.title).font(.uiLargeTitle).fixedSize(horizontal: false, vertical: true)
            if !perf.composers.isEmpty {
                Text(perf.composers.map(\.name).joined(separator: " · ")).font(.uiTitle3).foregroundStyle(Color.ember)
            }
            HStack(spacing: 6) {
                Text([Fmt.date(perf.date), perf.venue.map { [$0.name, $0.city].compactMap { $0 }.joined(separator: ", ") }]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.uiSubheadline).foregroundStyle(Color.graphite)
            }
            if let b = perf.broadcaster ?? perf.eventName {
                Text(b).font(.uiFootnote).foregroundStyle(Color.graphite)
            }
            if let c = confidence {
                Label("\(c)% confident in these notes and credits", systemImage: "checkmark.seal")
                    .font(.uiFootnote.weight(.medium)).foregroundStyle(c >= 80 ? Color.ink : Color.graphite)
                    .padding(.top, 2)
            }
            Rectangle().fill(Color.ink).frame(height: 2).padding(.top, 10)
        }
        .padding(.horizontal).padding(.top, 16)
    }

    /// How sure we are of who performed and when: each credit weighted by its evidence, the date counted as one more.
    private var confidence: Int? {
        func w(_ s: EvidenceStatus) -> Double {
            switch s { case .confirmed: 1; case .productionOnly: 0.7; case .inferred: 0.5; case .unknown: 0.3; case .conflict: 0.2 }
        }
        let scores = perf.credits.map { w($0.status) } + (perf.dateStatus.map { [w($0)] } ?? [])
        guard !scores.isEmpty else { return nil }
        return Int((scores.reduce(0, +) / Double(scores.count) * 100).rounded())
    }

    // MARK: The Performance

    /// Client-side fallback when the server has no liner notes: credits whose notes tell a cast-change story.
    private var localCastChanges: [LinerFact] {
        perf.credits.compactMap { c in
            guard let n = c.notes, ["replac", "substitut", "indispos", "ill ", "illness", "stepped in", "debut", "cancel"]
                .contains(where: { n.lowercased().contains($0) }) else { return nil }
            return LinerFact(label: "\(c.person.name), \(c.role ?? "")", text: n, cites: c.sourceIds)
        }
    }

    @ViewBuilder private var performanceSection: some View {
        let serverFacts = (notes?.performance?.facts ?? []).filter { !["Summary", "Date", "Venue", "Date evidence"].contains($0.label ?? "") }
        let changes = notes?.performance?.castChanges.nilIfEmpty ?? localCastChanges
        SectionTitle("The Performance")
        VStack(alignment: .leading, spacing: 16) {
            if let s = perf.summary, !s.isEmpty {
                Text(s).font(.uiReading).lineSpacing(5)
            }
            ForEach(Array(serverFacts.enumerated()), id: \.offset) { _, f in fact(f) }
            if !changes.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    Eyebrow("On this date")
                    ForEach(Array(changes.enumerated()), id: \.offset) { _, f in fact(f, headline: true) }
                }
            }
            if let e = perf.dateEvidence, !e.isEmpty {
                DisclosureGroup {
                    Text(e).font(.uiFootnote).foregroundStyle(Color.graphite).fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 4)
                } label: { Eyebrow("How we know the date") }
                .tint(Color.graphite)
            }
        }
        .padding(.horizontal)
    }

    // MARK: Program notes

    @ViewBuilder private var programNotesSection: some View {
        let programs = notes?.programNotes?.items ?? []
        if !programs.isEmpty {
            SectionTitle("Program Notes")
            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(programs.enumerated()), id: \.offset) { i, p in
                    VStack(alignment: .leading, spacing: 10) {
                        NavigationLink(value: Route.programNote(perf.id, p.n ?? i + 1)) {
                            HStack(alignment: .firstTextBaseline) {
                                Label(p.title ?? "Program \(i + 1)", systemImage: "book.pages").font(.uiTitle3)
                                    .multilineTextAlignment(.leading)
                                Spacer()
                                Image(systemName: "chevron.right").font(.uiCaption).foregroundStyle(Color.graphite)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(14)
                    .background(Color.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: The Work

    @ViewBuilder private var workSection: some View {
        let works = (notes?.work?.items ?? []).filter { ($0.synopsis ?? $0.description) != nil || $0.premiere != nil }
        let program = perf.program.filter { !($0.notes ?? "").isEmpty }
        let groupCredit = workGroupCredit(works)
        if !works.isEmpty || !program.isEmpty {
            SectionTitle(perf.program.count > 1 ? "The Works" : "The Work")
            VStack(alignment: .leading, spacing: 20) {
                if !works.isEmpty {
                    ForEach(Array(works.enumerated()), id: \.offset) { _, w in
                        VStack(alignment: .leading, spacing: 8) {
                            if let c = w.composer { Eyebrow(c.name) }
                            if let t = w.title { Text(t).font(.uiTitle3) }
                            if let d = w.description, !d.isEmpty { Text(d).font(.uiFootnote).foregroundStyle(Color.graphite) }
                            let premiere = w.premiere.map { [$0.date.map { Fmt.date($0) }, $0.place].compactMap { $0 }.joined(separator: ", ") }
                            let facts = [premiere.flatMap { $0.isEmpty ? nil : "Premiere: \($0)" },
                                         w.librettists.isEmpty ? nil : "Libretto: " + w.librettists.joined(separator: ", ")].compactMap { $0 }
                            if !facts.isEmpty {
                                Text(facts.joined(separator: " · ")).font(.uiFootnote).foregroundStyle(Color.graphite)
                            }
                            if let s = w.synopsis, !s.isEmpty {
                                if let h = w.synopsisHeading { Eyebrow(h).padding(.top, 4) }
                                ExpandableText(text: s, lineLimit: 5, font: .uiReading)
                            }
                            if let credit = workSummaryCredit(w), credit != groupCredit {
                                Text("Summary from \(credit)").font(.uiCaption).foregroundStyle(Color.graphite)
                            }
                        }
                    }
                    if let groupCredit {
                        Text("Summaries from \(groupCredit)").font(.uiCaption).foregroundStyle(Color.graphite)
                    }
                } else {
                    ForEach(Array(program.enumerated()), id: \.offset) { _, item in
                        VStack(alignment: .leading, spacing: 6) {
                            if let c = item.composer { Eyebrow(c.name) }
                            Text(item.title).font(.uiTitle3)
                            if let n = item.notes { ExpandableText(text: n, lineLimit: 5, font: .uiCallout) }
                        }
                    }
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: Timeline

    @ViewBuilder private var timelineSection: some View {
        let tracks = perf.tracks.sorted { $0.index < $1.index }
        if !tracks.isEmpty {
            SectionTitle("Timeline", trailing: Fmt.longDuration(tracks.compactMap(\.durationS).reduce(0, +)))
            let starts = tracks.indices.map { i in tracks.prefix(i).compactMap(\.durationS).reduce(0, +) }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(tracks.enumerated()), id: \.element.id) { i, t in
                    if perf.program.count > 1, t.workId != nil, i == 0 || tracks[i - 1].workId != t.workId,
                       let w = perf.work(t.workId) {
                        Text(w.title).font(.uiHeadline).padding(.top, 12).padding(.bottom, 4)
                    }
                    Button { player.play(perf, from: i, api: model.api) } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            Text(Fmt.clock(starts[i])).font(.uiCaption.monospacedDigit()).foregroundStyle(Color.graphite)
                                .frame(width: 56, alignment: .trailing)
                            Rectangle().fill(t.segment.isMusic ? Color.ink : Color.rule).frame(width: 2, height: 16)
                            Text(t.shortTitle).font(t.segment.isMusic ? .uiCallout : .uiFootnote)
                                .foregroundStyle(t.segment.isMusic ? Color.ink : Color.graphite)
                                .multilineTextAlignment(.leading)
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 5)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(t.shortTitle), starts at \(Fmt.clock(starts[i])). Play.")
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: From the broadcast

    private func trackPosition(_ id: String?, _ index: Int?) -> Int? {
        let tracks = perf.tracks.sorted { $0.index < $1.index }
        return tracks.firstIndex { t in id.map { $0 == t.id } ?? (index == t.index) }
    }

    @ViewBuilder private var broadcastSection: some View {
        let quotes = notes?.broadcast?.quotes ?? []
        let features = notes?.broadcast?.intermissionFeatures ?? []
        if !quotes.isEmpty || !features.isEmpty {
            SectionTitle("From the Broadcast")
            VStack(alignment: .leading, spacing: 16) {
                ForEach(Array(quotes.enumerated()), id: \.offset) { _, q in
                    Button {
                        if let i = trackPosition(q.trackId, q.trackIndex) {
                            if player.performance?.id != perf.id { player.play(perf, from: i, api: model.api) }
                            player.seek(track: i, to: q.start ?? 0)
                        }
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("“\(q.text)”").font(.lexend(19, weight: .light, relativeTo: .title3))
                                .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                            Text(["Track \(q.trackIndex.map(String.init) ?? "?")", q.start.map { Fmt.clock($0) }, "play from here"]
                                .compactMap { $0 }.joined(separator: " · "))
                                .font(.uiCaption).foregroundStyle(Color.graphite)
                        }
                        .padding(.leading, 12)
                        .overlay(alignment: .leading) { Rectangle().fill(Color.ember).frame(width: 2) }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                if !quotes.isEmpty {
                    Text("Source: the off-air broadcast recording (machine transcript; may contain errors).")
                        .font(.uiCaption2).foregroundStyle(Color.graphite)
                }
                if !features.isEmpty {
                    Eyebrow("Intermission features")
                    ForEach(Array(features.enumerated()), id: \.offset) { _, f in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .firstTextBaseline) {
                                Text(f.title ?? "Intermission").font(.uiHeadline)
                                Spacer()
                                Text(Fmt.clock(f.durationS)).font(.uiCaption.monospacedDigit()).foregroundStyle(Color.graphite)
                            }
                            if let l = f.firstLine { Text("“\(l)”").font(.uiFootnote).foregroundStyle(Color.graphite).lineLimit(2) }
                        }
                    }
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: Cast

    @ViewBuilder private var castSection: some View {
        if !perf.credits.isEmpty {
            SectionTitle("Cast")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(perf.credits.enumerated()), id: \.offset) { _, c in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(c.role ?? "").font(.uiFootnote).foregroundStyle(Color.graphite)
                            .frame(width: 118, alignment: .leading)
                        VStack(alignment: .leading, spacing: 4) {
                            NavigationLink(value: Route.person(c.person.id)) {
                                Text(c.person.name).font(.uiCallout.weight(.medium)).multilineTextAlignment(.leading)
                            }
                            .buttonStyle(.plain)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 7)
                    Rectangle().fill(Color.rule).frame(height: 0.5)
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: Sources

    @ViewBuilder private var sourcesSection: some View {
        let server = notes?.sources?.items ?? []
        let supplemental = supplementalCitations
        if !server.isEmpty || !perf.sources.isEmpty || !perf.resources.isEmpty || !supplemental.isEmpty {
            SectionTitle("Sources")
            DisclosureGroup(isExpanded: $showSources) {
            VStack(alignment: .leading, spacing: 14) {
                if !server.isEmpty {
                    ForEach(server) { s in
                        VStack(alignment: .leading, spacing: 4) {
                            CitationRow(citation: s.citation, open: open, prefix: Self.kindLabel(s.kind, s.type))
                        }
                    }
                } else {
                    ForEach(perf.sources) { s in
                        VStack(alignment: .leading, spacing: 4) {
                            CitationRow(citation: Citation(source: s), open: open, prefix: Self.kindLabel(nil, s.type))
                        }
                    }
                }
                ForEach(perf.resources) { r in
                    CitationRow(citation: Citation(resource: r), open: open, prefix: r.kind?.capitalized ?? "Link")
                }
                ForEach(Array(supplemental.enumerated()), id: \.offset) { _, citation in
                    CitationRow(citation: citation, open: open)
                }
            }
            .padding(.top, 8)
            } label: {
                Text("\((server.isEmpty ? perf.sources.count : server.count) + perf.resources.count + supplemental.count) sources")
                    .font(.uiFootnote).foregroundStyle(Color.graphite)
            }
            .tint(Color.graphite)
            .padding(.horizontal)
        }
    }

    static func kindLabel(_ kind: String?, _ type: String?) -> String {
        if let type { return type.replacingOccurrences(of: "_", with: " ").capitalized }
        switch kind {
        case "wikipedia": return "Wikipedia"
        case "wikidata": return "Wikidata"
        case "program": return "Program"
        case "broadcast": return "Broadcast"
        case "record": return "Research record"
        default: return "Source"
        }
    }

    // MARK: Helpers

    private func fact(_ f: LinerFact, headline: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let l = f.label {
                if headline { Text(l).font(.uiHeadline) } else { Eyebrow(l) }
            }
            if headline || f.text.count < 180 {
                Text(MarkdownView.inline(f.text)).font(.uiCallout).fixedSize(horizontal: false, vertical: true)
            } else {
                ExpandableText(text: f.text, lineLimit: 3, font: .uiCallout)
            }
        }
    }

    private func workSummaryCredit(_ work: LinerNotes.WorkItem) -> String? {
        let labels = work.cites.compactMap { id -> String? in
            if let source = notes?.source(id) {
                return sourceCredit(kind: source.kind, title: source.title, url: source.url)
            }
            if let source = perf.source(id) {
                return sourceCredit(kind: nil, title: source.title, url: source.url)
            }
            return nil
        }
        let unique = Array(Set(labels)).sorted()
        if !unique.isEmpty { return unique.joined(separator: " and ") }
        return work.wikipediaUrl == nil ? nil : "Wikipedia"
    }

    private func sourceCredit(kind: String?, title: String?, url: String?) -> String? {
        switch kind {
        case "wikipedia": return "Wikipedia"
        case "wikidata": return "Wikidata"
        default: break
        }
        let host = url.flatMap { URL(string: $0)?.host() }?.replacingOccurrences(of: "www.", with: "")
        if host?.contains("wikipedia.org") == true { return "Wikipedia" }
        if host?.contains("wikidata.org") == true { return "Wikidata" }
        return title ?? host
    }

    private func workGroupCredit(_ works: [LinerNotes.WorkItem]) -> String? {
        let credits = works.compactMap(workSummaryCredit)
        let counts = Dictionary(grouping: credits, by: { $0 }).mapValues(\.count)
        return counts.sorted { lhs, rhs in
            lhs.value == rhs.value ? lhs.key < rhs.key : lhs.value > rhs.value
        }.first?.key
    }

    private var supplementalCitations: [Citation] {
        let primary = (notes?.sources?.items ?? []).map(\.citation)
            + perf.sources.map(Citation.init(source:))
            + perf.resources.map(Citation.init(resource:))
        var seen = Set(primary.compactMap { $0.url ?? $0.waybackUrl })
        let candidates = (notes?.programNotes?.items ?? []).compactMap { $0.source?.citation }
            + (notes?.work?.items ?? []).compactMap { Citation.wikipedia($0.wikipediaUrl) }
        return candidates.filter { citation in
            guard let key = citation.url ?? citation.waybackUrl, !seen.contains(key) else { return false }
            seen.insert(key)
            return true
        }
    }
}

// MARK: - Program note (full Markdown)

struct ProgramNoteView: View {
    let performanceId: String
    let n: Int
    @State private var web: WebLink?

    var body: some View {
        Loader(load: { try await $0.programNote(performanceId: performanceId, n: n) }) { note in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Eyebrow("Program Notes")
                    if let t = note.title { Text(t).font(.uiTitle) }
                    if let c = note.source?.citation, !c.isEmpty {
                        CitationRow(citation: c, open: { web = WebLink(url: $0) })
                    }
                    Rectangle().fill(Color.ink).frame(height: 2)
                    MarkdownView(markdown: note.markdown ?? "")
                }
                .padding()
                .frame(maxWidth: 680, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .background(Color.paper)
            .linksInAppBrowser($web)
        }
        .inAppBrowser($web)
        .navigationTitle("Program Notes")
        .navigationBarTitleDisplayMode(.inline)
    }
}

import SwiftUI

struct PerformanceView: View {
    let id: String
    var body: some View {
        Loader(load: { try await $0.performance(id: id) }) { PerformanceContent(perf: $0) }
            .navigationBarTitleDisplayMode(.inline)
    }
}

struct PerformanceContent: View {
    let perf: PerformanceDetail
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @Environment(RotationStore.self) private var rotation
    @State private var selectedCredit: CreditSelection?
    @State private var web: WebLink?
    @State private var showDateEvidence = false

    private var isCurrent: Bool { player.performance?.id == perf.id }

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                if let s = perf.summary, !s.isEmpty {
                    ExpandableText(text: s, lineLimit: 4, font: .display(16))
                        .foregroundStyle(.primary.opacity(0.85))
                        .padding(.horizontal).padding(.top, 20)
                }
                program.id("program")
                credits.id("credits")
                tracks.id("tracks")
                resources.id("resources")
                if FeatureFlags.askDad { openQuestions.id("questions") }
                footer
            }
            .padding(.bottom, 40)
        }
        .task {
            // Debug/screenshot aid: launch argument `-scrollTo credits|tracks|resources|questions`.
            if let target = UserDefaults.standard.string(forKey: "scrollTo") {
                try? await Task.sleep(for: .milliseconds(400))
                proxy.scrollTo(target, anchor: .top)
            }
        }
        }
        .background(Color.paper)
        .navigationTitle(perf.title)
        .sheet(item: $selectedCredit) { sel in
            CreditDetailSheet(credit: sel.credit, perf: perf)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .inAppBrowser($web)
    }

    // MARK: Header

    private var header: some View {
        let (a, _) = Palette.pair(for: perf.collection ?? perf.id)
        return VStack(spacing: 14) {
            ArtworkView(path: perf.artworkUrl, title: perf.title, seed: perf.collection ?? perf.id, cornerRadius: 12)
                .frame(width: 230, height: 230)
                .shadow(color: .black.opacity(0.28), radius: 20, y: 10)
                .padding(.top, 12)

            VStack(spacing: 6) {
                Eyebrow([perf.collection, Fmt.kind(perf.kind)].compactMap { $0 }.joined(separator: " · "))
                Text(perf.title)
                    .font(.display(32, weight: .semibold))
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 6) {
                    ForEach(perf.composers) { c in
                        NavigationLink(value: Route.composer(c.id)) {
                            Text(c.name).font(.display(18, weight: .medium)).foregroundStyle(.tint)
                        }
                        .buttonStyle(.plain)
                        if c.id != perf.composers.last?.id { Text("·").foregroundStyle(.secondary) }
                    }
                }
                .lineLimit(1).minimumScaleFactor(0.7)

                if let sub = perf.subtitle, !sub.isEmpty {
                    Text(sub).font(.uiSubheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal)

            VStack(spacing: 3) {
                Button { showDateEvidence.toggle() } label: {
                    HStack(spacing: 6) {
                        Text(Fmt.date(perf.date)).font(.uiSubheadline.weight(.medium))
                        if let s = perf.dateStatus, s != .confirmed { EvidenceBadge(status: s) }
                        Image(systemName: "info.circle").font(.uiCaption).foregroundStyle(.tertiary)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityHint("Shows how the date was established")

                if let v = perf.venue {
                    NavigationLink(value: Route.venue(v.id)) {
                        Text([v.name, v.city].compactMap { $0 }.joined(separator: ", "))
                            .font(.uiSubheadline).foregroundStyle(.secondary).underline(color: .secondary.opacity(0.3))
                    }
                    .buttonStyle(.plain)
                }
                if let b = perf.broadcaster ?? perf.eventName {
                    Text(b).font(.uiCaption).foregroundStyle(.tertiary).multilineTextAlignment(.center).lineLimit(3)
                        .padding(.horizontal, 24)
                }
            }

            if showDateEvidence { dateEvidence }

            HStack(spacing: 12) {
                Button {
                    if isCurrent { player.togglePlayPause() }
                    else { player.play(perf, api: model.api) }
                } label: {
                    Label(isCurrent && player.isPlaying ? "Pause" : (isCurrent ? "Resume" : "Play"),
                          systemImage: isCurrent && player.isPlaying ? "pause.fill" : "play.fill")
                        .font(.uiHeadline)
                        .foregroundStyle(Color.paper)
                        .frame(minWidth: 130)
                        .padding(.vertical, 4)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .controlSize(.large)
                .disabled(perf.tracks.isEmpty)

                VStack(alignment: .leading, spacing: 1) {
                    Text("\(perf.tracks.count) tracks").font(.uiFootnote.weight(.medium))
                    if let d = Fmt.longDuration(perf.durationS ?? perf.tracks.compactMap(\.durationS).reduce(0, +)) {
                        Text(d).font(.uiFootnote).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.top, 4)

            Button {
                Task { await rotation.toggle(perf) }
            } label: {
                let included = rotation.contains(perf.id)
                Label(included ? "In Rotation" : "Add to Rotation",
                      systemImage: included ? "checkmark.circle.fill" : "plus.circle")
                    .font(.uiSubheadline.weight(.semibold))
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.capsule)
            .tint(rotation.contains(perf.id) ? Color.green : Color.ember)
            .task { if rotation.items.isEmpty { await rotation.refresh() } }

            NavigationLink(value: Route.linerNotes(perf.id)) {
                Label("Liner Notes", systemImage: "text.book.closed")
                    .font(.uiSubheadline.weight(.medium))
            }
            .buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)

            if let r = perf.confirmedRatio { ConfidenceMeter(ratio: r) }
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, 8)
        .background(
            LinearGradient(colors: [a.opacity(0.30), Color.paper.opacity(0)], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea(edges: .top)
        )
    }

    private var dateEvidence: some View {
        VStack(alignment: .leading, spacing: 8) {
            Eyebrow("How we know the date")
            if let e = perf.dateEvidence { Text(e).font(.uiFootnote).fixedSize(horizontal: false, vertical: true) }
            if perf.candidateDates.count > 1 {
                Text("Candidate dates: " + perf.candidateDates.map { Fmt.date($0) }.joined(separator: ", "))
                    .font(.uiFootnote).foregroundStyle(.secondary)
            }
            if let bd = perf.broadcastDate, bd != perf.date {
                Text("Broadcast: \(Fmt.date(bd))").font(.uiFootnote).foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .padding(.horizontal)
        .transition(.opacity.combined(with: .move(edge: .top)))
    }

    // MARK: Program

    @ViewBuilder private var program: some View {
        if !perf.program.isEmpty {
            SectionTitle("Program")
            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(perf.program.enumerated()), id: \.offset) { _, item in
                    ProgramRow(item: item)
                }
            }
            .padding(.horizontal)
        }
    }

    // MARK: Credits

    private var creditGroups: [CreditGroup] { CreditGroup.make(perf) }

    @ViewBuilder private var credits: some View {
        if !perf.credits.isEmpty {
            SectionTitle("Cast & Credits", trailing: "\(perf.credits.count)")
            VStack(alignment: .leading, spacing: 0) {
                let groups = creditGroups
                ForEach(groups) { group in
                    if groups.count > 1 || group.title != nil, let t = group.title {
                        Text(t).font(.display(17, weight: .semibold))
                            .padding(.horizontal).padding(.top, 16).padding(.bottom, 2)
                    }
                    ForEach(group.sections, id: \.title) { section in
                        Eyebrow(section.title).padding(.horizontal).padding(.top, 12).padding(.bottom, 4)
                        ForEach(section.credits, id: \.offset) { entry in
                            Button { selectedCredit = CreditSelection(credit: entry.credit) } label: {
                                CreditRow(credit: entry.credit)
                                    .padding(.horizontal).padding(.vertical, 7)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    // MARK: Tracks

    @ViewBuilder private var tracks: some View {
        if !perf.tracks.isEmpty {
            SectionTitle("Tracks", trailing: Fmt.longDuration(perf.tracks.compactMap(\.durationS).reduce(0, +)))
            let multiWork = perf.program.count > 1
            let sorted = perf.tracks.sorted { $0.index < $1.index }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(sorted.enumerated()), id: \.element.id) { i, track in
                    if multiWork, track.workId != nil, i == 0 || sorted[i - 1].workId != track.workId,
                       let w = perf.work(track.workId) {
                        Text(w.title).font(.display(16, weight: .semibold))
                            .padding(.horizontal).padding(.top, 14).padding(.bottom, 4)
                    }
                    Button { player.play(perf, from: i, api: model.api) } label: {
                        TrackRow(track: track, isCurrent: isCurrent && player.currentIndex == i, isPlaying: player.isPlaying)
                            .padding(.horizontal).padding(.vertical, 9)
                    }
                    .buttonStyle(.plain)
                    if i < sorted.count - 1 { Divider().padding(.leading, 52) }
                }
            }
        }
    }

    // MARK: Resources & sources

    @ViewBuilder private var resources: some View {
        if !perf.resources.isEmpty || !perf.sources.isEmpty {
            SectionTitle("Sources & Programs")
            VStack(alignment: .leading, spacing: 10) {
                ForEach(perf.resources) { r in ResourceRow(resource: r) { web = WebLink(url: $0) } }
                if !perf.sources.isEmpty {
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: 12) {
                            ForEach(perf.sources) { s in SourceRow(source: s) { web = WebLink(url: $0) } }
                        }
                        .padding(.top, 8)
                    } label: {
                        Text("All research sources (\(perf.sources.count))").font(.uiSubheadline.weight(.medium))
                    }
                    .padding(.top, 6)
                }
            }
            .padding(.horizontal)
        }
    }

    @ViewBuilder private var openQuestions: some View {
        if !perf.openQuestions.isEmpty {
            SectionTitle("Open Questions", trailing: "\(perf.openQuestions.count)")
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(perf.openQuestions.enumerated()), id: \.offset) { _, q in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Image(systemName: "questionmark.circle").foregroundStyle(Color.ember)
                        ExpandableText(text: q, lineLimit: 3, font: .uiSubheadline)
                    }
                }
                Text("Know the answer? Use the Ask Dad tab.").font(.uiCaption).foregroundStyle(.secondary)
            }
            .padding(.horizontal)
        }
    }

    @ViewBuilder private var footer: some View {
        if let f = perf.folder {
            Label(f, systemImage: "folder").font(.uiCaption.monospaced()).foregroundStyle(.tertiary)
                .padding(.horizontal).padding(.top, 28)
        }
    }
}

// MARK: - Program row

struct ProgramRow: View {
    let item: ProgramItem
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let c = item.composer {
                NavigationLink(value: Route.composer(c.id)) { Eyebrow(c.name) }.buttonStyle(.plain)
            }
            Group {
                if let w = item.workId {
                    NavigationLink(value: Route.work(w)) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(item.title).font(.display(20, weight: .medium)).multilineTextAlignment(.leading)
                            Image(systemName: "chevron.right").font(.uiCaption).foregroundStyle(.tertiary)
                        }
                    }
                    .buttonStyle(.plain)
                } else {
                    Text(item.title).font(.display(20, weight: .medium))
                }
            }
            if let cat = item.catalogue, !cat.isEmpty { Text(cat).font(.uiCaption).foregroundStyle(.secondary) }
            if let n = item.notes, !n.isEmpty {
                ExpandableText(text: n, lineLimit: 2, font: .uiFootnote).foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Credits

struct CreditSelection: Identifiable {
    let credit: Credit
    let id = UUID()
}

struct CreditGroup: Identifiable {
    struct Section { let title: String; let credits: [(offset: Int, credit: Credit)] }
    let id: String
    let title: String?
    let sections: [Section]

    private static let productionWords = ["director", "design", "production", "producer", "host", "commentator",
                                          "interview", "announcer", "presenter", "engineer", "choreograph", "dramaturg",
                                          "lighting", "costume", "set ", "broadcast", "narrator (radio)", "quiz", "panel"]
    private static let ensembleWords = ["orchestra", "chorus", "choir", "ensemble", "band", "quartet"]

    static func isProduction(_ c: Credit) -> Bool {
        let r = (c.role ?? "").lowercased()
        if r == "chorus master" || r.contains("chorus director") { return false }
        return productionWords.contains { r.contains($0) }
    }

    static func rank(_ c: Credit) -> Int {
        let r = (c.role ?? "").lowercased()
        if r == "conductor" || r.hasPrefix("conductor") { return 0 }
        if ensembleWords.contains(where: { r.contains($0) }) || r.contains("chorus master") { return 2 }
        return 1
    }

    static func make(_ perf: PerformanceDetail) -> [CreditGroup] {
        let known = Set(perf.program.compactMap(\.workId))
        let indexed = Array(perf.credits.enumerated())
        var groups: [CreditGroup] = []
        func build(id: String, title: String?, _ entries: [(offset: Int, element: Credit)]) {
            guard !entries.isEmpty else { return }
            let perf = entries.filter { !isProduction($0.element) }
                .sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }
                .map { (offset: $0.offset, credit: $0.element) }
            let prod = entries.filter { isProduction($0.element) }.map { (offset: $0.offset, credit: $0.element) }
            var sections: [Section] = []
            if !perf.isEmpty { sections.append(Section(title: "Performers", credits: perf)) }
            if !prod.isEmpty { sections.append(Section(title: "Production & Broadcast", credits: prod)) }
            groups.append(CreditGroup(id: id, title: title, sections: sections))
        }
        let single = perf.program.count <= 1
        if single {
            build(id: "all", title: nil, indexed)
        } else {
            for item in perf.program {
                guard let w = item.workId else { continue }
                build(id: w, title: item.title, indexed.filter { $0.element.workId == w })
            }
            build(id: "whole", title: "Whole Program", indexed.filter { $0.element.workId == nil || !known.contains($0.element.workId!) })
        }
        return groups
    }
}

struct CreditRow: View {
    let credit: Credit
    var body: some View {
        HStack(spacing: 12) {
            Portrait(path: credit.person.imageUrl, name: credit.person.name, size: 40)
            VStack(alignment: .leading, spacing: 1) {
                if let role = credit.role {
                    Text(role).font(.display(14, weight: .light)).foregroundStyle(.secondary).lineLimit(2)
                }
                Text(credit.person.name).font(.uiBody.weight(.medium)).lineLimit(2)
            }
            Spacer(minLength: 8)
            StarRating(rating: credit.shownRating)
        }
        .contentShape(Rectangle())
    }
}

/// 1–5 stars (how well backed up) over "NN% sure".
struct StarRating: View {
    let rating: CreditRating
    var body: some View {
        VStack(alignment: .trailing, spacing: 2) {
            HStack(spacing: 1) {
                ForEach(1...5, id: \.self) { i in
                    Image(systemName: i <= rating.stars ? "star.fill" : "star")
                        .font(.system(size: 10))
                        .foregroundStyle(i <= rating.stars ? Color.ember : Color.graphite.opacity(0.4))
                }
            }
            Text("\(rating.confidence)% sure").font(.uiCaption2).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(rating.stars) of 5 stars, \(rating.confidence) percent sure")
    }
}

struct EvidenceLegend: View {
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach([EvidenceStatus.confirmed, .productionOnly, .inferred, .conflict], id: \.self) {
                    EvidenceBadge(status: $0)
                }
            }
        }
        .scrollClipDisabled()
    }
}

struct CreditDetailSheet: View {
    let credit: Credit
    let perf: PerformanceDetail
    @Environment(\.dismiss) private var dismiss
    @State private var web: WebLink?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(spacing: 14) {
                        Portrait(path: credit.person.imageUrl, name: credit.person.name, size: 64)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(credit.person.name).font(.display(24, weight: .semibold))
                            if let r = credit.role { Text(r).font(.display(16, weight: .light)).foregroundStyle(.secondary) }
                            if let w = perf.work(credit.workId), perf.program.count > 1 {
                                Text(w.title).font(.uiCaption).foregroundStyle(.secondary)
                            }
                        }
                    }

                    NavigationLink(value: Route.person(credit.person.id)) {
                        Label("All performances by \(credit.person.name)", systemImage: "person.crop.circle")
                            .font(.uiSubheadline.weight(.medium))
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        EvidenceBadge(status: credit.status)
                        Text(credit.status.explanation).font(.uiFootnote).foregroundStyle(.secondary)
                    }

                    if let notes = credit.notes, !notes.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Eyebrow("Notes")
                            Text(notes).font(.uiBody).fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    let sources = credit.sourceIds.compactMap { perf.source($0) }
                    if !sources.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Eyebrow("Sources (\(sources.count))")
                            ForEach(sources) { s in SourceRow(source: s) { web = WebLink(url: $0) } }
                        }
                    }
                }
                .padding()
            }
            .withRoutes()
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .inAppBrowser($web)
        }
    }
}

// MARK: - Sources & resources

struct ResourceRow: View {
    let resource: Resource
    let open: (URL) -> Void

    var body: some View {
        let citation = Citation(resource: resource)
        let liveURL = citation.liveURL
        let bestURL = liveURL ?? citation.archiveURL
        VStack(alignment: .leading, spacing: 6) {
            if let k = resource.kind { Eyebrow(k) }
            if let bestURL {
                Button { open(bestURL) } label: {
                    Text(resource.title ?? resource.url).font(.display(16, weight: .medium)).underline()
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
            } else {
                Text(resource.title ?? resource.url).font(.display(16, weight: .medium))
            }
            if let d = resource.description {
                Text(d).font(.uiFootnote).foregroundStyle(.secondary).multilineTextAlignment(.leading)
            }
            if let host = liveURL?.host() {
                Text(host.replacingOccurrences(of: "www.", with: "")).font(.uiCaption2).foregroundStyle(.tertiary)
            }
        }
        .padding(12)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .contextMenu {
            if let bestURL { Button("Copy Link", systemImage: "doc.on.doc") { UIPasteboard.general.url = bestURL } }
        }
    }
}

struct SourceRow: View {
    let source: Source
    let open: (URL) -> Void

    var body: some View {
        let citation = Citation(source: source)
        let liveURL = citation.liveURL
        let bestURL = liveURL ?? citation.archiveURL
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                if let t = source.type { Eyebrow(t.replacingOccurrences(of: "_", with: " ")) }
                if source.dateSpecific == true {
                    Text("date-specific").font(.uiCaption2).foregroundStyle(Color.ink)
                }
            }
            if let bestURL {
                Button { open(bestURL) } label: {
                    Text(source.title ?? source.url ?? "Untitled source").font(.uiSubheadline.weight(.medium)).underline()
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
            } else {
                Text(source.title ?? "Untitled source").font(.uiSubheadline.weight(.medium))
            }
            if let e = source.excerpt, !e.isEmpty {
                Text("“\(e)”").font(.display(14, weight: .light)).foregroundStyle(.secondary)
                    .padding(.leading, 10)
                    .overlay(alignment: .leading) { Rectangle().fill(.tint.opacity(0.5)).frame(width: 2) }
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

#Preview {
    NavigationStack { PerformanceContent(perf: MockAPIClient.shared.details[0]).withRoutes() }
        .environment(AppModel.preview)
        .environment(PlayerController())
        .environment(RotationStore(model: AppModel.preview))
}

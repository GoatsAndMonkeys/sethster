import SwiftUI

// MARK: - Shared header

struct EntityHeader: View {
    let imagePath: String?
    let name: String
    var eyebrow: String?
    var years: String?
    var bio: String?
    var wikipedia: String?
    var tagline: String?
    var imageCredit: String?
    @State private var web: WebLink?

    var body: some View {
        VStack(spacing: 10) {
            Portrait(path: imagePath, name: name, size: 128)
                .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
                .padding(.top, 8)
            if let eyebrow { Eyebrow(eyebrow) }
            Text(name).font(.display(32, weight: .semibold)).multilineTextAlignment(.center)
            if let years, !years.isEmpty {
                Text(years).font(.display(17)).foregroundStyle(.secondary)
            }
            if let tagline, !tagline.isEmpty {
                Text(tagline).font(.uiSubheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            if let bio, !bio.isEmpty {
                ExpandableText(text: bio, lineLimit: 5, font: .display(16))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
            }
            if let c = Citation.wikipedia(wikipedia) {
                CitationRow(citation: c, open: { web = WebLink(url: $0) })
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let imageCredit, !imageCredit.isEmpty, imagePath != nil {
                Text("Portrait: \(imageCredit)").font(.uiCaption2).foregroundStyle(.tertiary).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal)
        .inAppBrowser($web)
    }
}

struct PerformanceList: View {
    let items: [PerformanceSummary]
    var showComposer = true
    var body: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(items) { p in
                NavigationLink(value: Route.performance(p.id)) {
                    PerformanceRow(p: p, showComposer: showComposer).padding(.horizontal).padding(.vertical, 8)
                }
                .buttonStyle(.plain)
                Divider().padding(.leading, 84)
            }
        }
    }
}

// MARK: - Composer

struct ComposerView: View {
    let id: String
    struct Payload: Sendable { let composer: ComposerDetail; let performances: [PerformanceSummary] }

    var body: some View {
        Loader(load: { api in
            async let c = api.composer(id: id)
            async let p = api.performances(PerformanceQuery(composer: id, sort: "-date"))
            return Payload(composer: try await c, performances: try await p.items)
        }) { data in
            let c = data.composer
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    EntityHeader(imagePath: c.imageUrl, name: c.name, eyebrow: "Composer", years: c.years?.value,
                                 bio: c.bioLong ?? c.bio, wikipedia: c.wikipediaUrl, tagline: c.description,
                                 imageCredit: c.imageCredit)
                    if !c.works.isEmpty {
                        SectionTitle("Works", trailing: "\(c.works.count)")
                        VStack(spacing: 0) {
                            ForEach(c.works) { w in
                                NavigationLink(value: Route.work(w.id)) {
                                    HStack {
                                        Text(w.title).font(.display(17, weight: .medium)).multilineTextAlignment(.leading)
                                        Spacer()
                                        if let n = w.performanceCount {
                                            Text("\(n)").font(.uiSubheadline.monospacedDigit()).foregroundStyle(.secondary)
                                        }
                                        Image(systemName: "chevron.right").font(.uiCaption).foregroundStyle(.tertiary)
                                    }
                                    .padding(.horizontal).padding(.vertical, 11)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                Divider().padding(.leading)
                            }
                        }
                    }
                    if !data.performances.isEmpty {
                        SectionTitle("Performances", trailing: "\(data.performances.count)")
                        PerformanceList(items: data.performances, showComposer: false)
                    }
                }
                .padding(.bottom, 40)
            }
            .background(Color.paper)
            .navigationTitle(c.name)
        }
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Work (compare performances)

struct WorkView: View {
    let id: String
    @State private var web: WebLink?

    var body: some View {
        Loader(load: { try await $0.work(id: id) }) { w in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 6) {
                        if let c = w.composer {
                            NavigationLink(value: Route.composer(c.id)) { Eyebrow(c.name) }.buttonStyle(.plain)
                        }
                        Text(w.title).font(.display(32, weight: .semibold))
                        if let d = w.description, !d.isEmpty {
                            ExpandableText(text: d, lineLimit: 5, font: .display(16)).padding(.top, 4)
                            if let c = Citation.wikipedia(w.wikipediaUrl) {
                                CitationRow(citation: c, open: { web = WebLink(url: $0) }).padding(.top, 4)
                            }
                        }
                    }
                    .padding(.horizontal).padding(.top, 12)

                    VersionsSection(work: w)
                }
                .padding(.bottom, 40)
            }
            .background(Color.paper)
            .navigationTitle(w.title)
        }
        .inAppBrowser($web)
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// A performance seen side-by-side with others of the same work: year first, then who and where.
struct ComparisonCard: View {
    let p: PerformanceSummary
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 6) {
                Text(Fmt.year(p.date) ?? "—").font(.display(26, weight: .semibold)).monospacedDigit()
                ArtworkView(path: p.artworkUrl, title: p.title, seed: p.collection ?? p.id, cornerRadius: 6)
                    .frame(width: 64, height: 64)
            }
            .frame(width: 72)
            VStack(alignment: .leading, spacing: 4) {
                if let sub = p.subtitle { Text(sub).font(.uiBody.weight(.medium)).lineLimit(3) }
                Text(Fmt.date(p.date)).font(.uiSubheadline).foregroundStyle(.secondary)
                if let v = p.venue { Text([v.name, v.city].compactMap { $0 }.joined(separator: ", ")).font(.uiCaption).foregroundStyle(.secondary) }
                HStack(spacing: 10) {
                    if let c = p.collection { Label(c, systemImage: "antenna.radiowaves.left.and.right").font(.uiCaption2) }
                    if let d = Fmt.longDuration(p.durationS) { Label(d, systemImage: "clock").font(.uiCaption2) }
                }
                .foregroundStyle(.tertiary)
                if let r = p.confirmedRatio { ConfidenceMeter(ratio: r).padding(.top, 2) }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .contentShape(Rectangle())
    }
}

// MARK: - Person

struct PersonView: View {
    let id: String
    var body: some View {
        Loader(load: { try await $0.person(id: id) }) { p in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    EntityHeader(imagePath: p.imageUrl, name: p.name, eyebrow: p.kind == "ensemble" ? "Ensemble" : roleSummary(p),
                                 years: p.years?.value, bio: p.bioLong ?? p.bio, wikipedia: p.wikipediaUrl,
                                 tagline: p.description, imageCredit: p.imageCredit)
                    SectionTitle("Performances", trailing: "\(p.credits.count)")
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(p.credits.enumerated()), id: \.offset) { _, c in
                            NavigationLink(value: Route.performance(c.performance.id)) {
                                HStack(spacing: 12) {
                                    ArtworkView(path: c.performance.artworkUrl, title: c.performance.title,
                                                seed: c.performance.collection ?? c.performance.id, cornerRadius: 6)
                                        .frame(width: 56, height: 56)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(c.performance.title).font(.display(17, weight: .medium)).lineLimit(2)
                                        if let r = c.role { Text(r).font(.display(14, weight: .light)).foregroundStyle(.secondary).lineLimit(1) }
                                        Text([Fmt.shortDate(c.performance.date), c.performance.venue?.name].compactMap { $0 }
                                            .joined(separator: " · ")).font(.uiCaption).foregroundStyle(.tertiary).lineLimit(1)
                                    }
                                    Spacer(minLength: 4)
                                    if let s = c.status { EvidenceBadge(status: s, compact: true) }
                                }
                                .padding(.horizontal).padding(.vertical, 8)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            Divider().padding(.leading, 84)
                        }
                    }
                }
                .padding(.bottom, 40)
            }
            .background(Color.paper)
            .navigationTitle(p.name)
        }
        .navigationBarTitleDisplayMode(.inline)
    }

    private func roleSummary(_ p: PersonDetail) -> String {
        let roles = p.credits.compactMap { $0.role?.lowercased() }
        if roles.contains(where: { $0.hasPrefix("conductor") }) { return "Conductor" }
        if roles.contains(where: { ["host", "presenter", "announcer", "commentator"].contains(where: $0.contains) }) { return "Broadcaster" }
        return "Performer"
    }

}

// MARK: - Venue / collection / all

struct VenueView: View {
    let id: String
    var body: some View {
        Loader(load: { try await $0.venue(id: id) }) { v in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 4) {
                        Eyebrow("Venue")
                        Text(v.name).font(.display(32, weight: .semibold))
                        if let c = v.city { Text(c).font(.display(17)).foregroundStyle(.secondary) }
                    }
                    .padding(.horizontal).padding(.top, 12)
                    SectionTitle("Performances", trailing: "\(v.performances.count)")
                    PerformanceList(items: v.performances)
                }
            }
            .background(Color.paper)
            .navigationTitle(v.name)
        }
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct CollectionView: View {
    let name: String
    var body: some View {
        FilteredPerformances(title: name, query: PerformanceQuery(collection: name, sort: "-date"))
    }
}

struct AllPerformancesView: View {
    var body: some View {
        FilteredPerformances(title: "All Performances", query: PerformanceQuery(sort: "-date", limit: 1000))
    }
}

struct FilteredPerformances: View {
    let title: String
    let query: PerformanceQuery
    @State private var filter = ""

    var body: some View {
        Loader(load: { try await $0.performances(query).items }) { items in
            let shown = filter.isEmpty ? items : items.filter {
                $0.title.localizedCaseInsensitiveContains(filter) || ($0.subtitle ?? "").localizedCaseInsensitiveContains(filter)
                    || $0.composers.contains { $0.name.localizedCaseInsensitiveContains(filter) }
            }
            List(shown) { p in
                NavigationLink(value: Route.performance(p.id)) { PerformanceRow(p: p) }
            }
            .listStyle(.plain)
            .overlay { if shown.isEmpty { ContentUnavailableView.search(text: filter) } }
            .searchable(text: $filter, prompt: "Filter")
        }
        .navigationTitle(title)
    }
}

import SwiftUI

/// Liner notes: historical context from `/performances/{id}/context` — when the piece was born, the night of the
/// recording, where each performer was in their career, and a timeline of the premiere, this recording and the
/// other versions in the collection. Sources are collected in a disclosure at the end. Hidden when unavailable.
struct HistoricalContextSection: View {
    let performanceId: String
    @Environment(AppModel.self) private var model
    @State private var ctx: HistoricalContext?
    @State private var web: WebLink?
    @State private var showSources = false

    var body: some View {
        Group {
            if let ctx, !ctx.isEmpty { content(ctx) }
        }
        .inAppBrowser($web)
        .task(id: "\(performanceId)-\(model.generation)") {
            guard let client = History.client(for: model.api) else { ctx = nil; return }
            ctx = try? await client.context(performanceId: performanceId)
        }
    }

    @ViewBuilder private func content(_ c: HistoricalContext) -> some View {
        let pieces = c.piece?.items ?? []
        if !pieces.isEmpty {
            SectionTitle(c.piece?.title ?? "When the Piece Was Born")
            VStack(alignment: .leading, spacing: 18) {
                ForEach(pieces, id: \.workId) { p in
                    VStack(alignment: .leading, spacing: 10) {
                        if pieces.count > 1 || p.title != c.title {
                            Text(p.title ?? "").font(.uiTitle3)
                        }
                        ForEach(Array(p.facts.enumerated()), id: \.offset) { _, f in fact(f) }
                        ForEach(Array(p.paragraphs.enumerated()), id: \.offset) { _, para in paragraph(para) }
                    }
                }
            }
            .padding(.horizontal)
        }

        if let r = c.recording, !(r.facts.isEmpty && r.paragraphs.isEmpty) {
            SectionTitle(r.title ?? "The Night of the Recording", trailing: r.season)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(r.facts.enumerated()), id: \.offset) { _, f in fact(f) }
                ForEach(Array(r.paragraphs.enumerated()), id: \.offset) { _, para in paragraph(para) }
                if !r.sameSeason.isEmpty {
                    Eyebrow("Also in the collection that season")
                    ForEach(r.sameSeason, id: \.performanceId) { o in
                        NavigationLink(value: Route.performance(o.performanceId)) {
                            HStack {
                                Text(o.title ?? o.performanceId).font(.uiSubheadline)
                                Spacer()
                                Text(Fmt.shortDate(o.date)).font(.uiCaption).foregroundStyle(Color.graphite)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(.horizontal)
        }

        let performers = (c.performers?.items ?? []).filter { $0.stage != nil }
        if !performers.isEmpty {
            SectionTitle(c.performers?.title ?? "Where the Performers Were")
            VStack(alignment: .leading, spacing: 12) {
                ForEach(performers, id: \.person.id) { p in performer(p) }
                Text("Career stage is computed from birth, debut and retirement years in Wikidata and Wikipedia.")
                    .font(.uiCaption2).foregroundStyle(Color.graphite)
            }
            .padding(.horizontal)
        }

        let timelines = (c.timeline?.items ?? []).filter { $0.events.count >= 2 }
        if !timelines.isEmpty {
            SectionTitle(c.timeline?.title ?? "Timeline")
            VStack(alignment: .leading, spacing: 18) {
                ForEach(timelines, id: \.workId) { t in timeline(t, showTitle: timelines.count > 1) }
            }
            .padding(.horizontal)
        }

        let sectionSources = c.sources?.items ?? []
        if !sectionSources.isEmpty {
            DisclosureGroup("Sources", isExpanded: $showSources) {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(sectionSources) { source in
                        CitationRow(citation: source.citation, open: { web = WebLink(url: $0) })
                    }
                }
                .padding(.top, 8)
            }
            .font(.uiFootnote)
            .tint(Color.graphite)
            .padding(.horizontal)
            .padding(.top, 18)
        }
    }

    // MARK: Pieces

    private func fact(_ f: CtxFact) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            if let l = f.label { Eyebrow(l) }
            Text(f.text).font(.uiBody).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func paragraph(_ p: CtxParagraph) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let t = p.title { Eyebrow(t) }
            Text(p.text).font(.uiReading).lineSpacing(5).fixedSize(horizontal: false, vertical: true)
            if p.check == "unchecked" {
                Text("not yet verified").font(.uiCaption2).foregroundStyle(Color.graphite)
            }
        }
    }

    // MARK: Performers

    private func performer(_ p: CtxPerformer) -> some View {
        NavigationLink(value: Route.person(p.person.id)) {
            HStack(alignment: .top, spacing: 12) {
                Portrait(path: p.person.imageUrl, name: p.person.name, size: 44)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(p.person.name).font(.uiHeadline)
                        if let s = p.stage { StagePill(stage: s, estimated: p.estimated == true) }
                    }
                    if let r = p.role { Text(r).font(.uiCaption).foregroundStyle(Color.graphite) }
                    if let t = p.text {
                        Text(t).font(.uiFootnote).foregroundStyle(Color.graphite)
                            .fixedSize(horizontal: false, vertical: true).multilineTextAlignment(.leading)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: Timeline

    private func timeline(_ t: CtxTimeline, showTitle: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if showTitle, let title = t.title { Text(title).font(.uiTitle3).padding(.bottom, 8) }
            ForEach(Array(t.events.enumerated()), id: \.offset) { i, e in
                let row = HStack(alignment: .top, spacing: 12) {
                    Text(Fmt.year(e.date) ?? e.date)
                        .font(.display(16, weight: e.isCurrent == true ? .bold : .medium))
                        .foregroundStyle(e.kind == "premiere" || e.isCurrent == true ? Color.ember : Color.ink)
                        .frame(width: 48, alignment: .trailing)
                    VStack(spacing: 0) {
                        Circle()
                            .fill(e.isCurrent == true || e.kind == "premiere" ? Color.ember : Color.graphite.opacity(0.5))
                            .frame(width: e.isCurrent == true ? 12 : 8, height: e.isCurrent == true ? 12 : 8)
                            .padding(.top, 5)
                        if i < t.events.count - 1 {
                            Rectangle().fill(Color.rule).frame(width: 1.5).frame(minHeight: 26)
                        }
                    }
                    .frame(width: 14)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.label ?? e.kind.capitalized).font(e.isCurrent == true ? .uiHeadline : .uiSubheadline)
                        Text(Fmt.date(e.date)).font(.uiCaption).foregroundStyle(Color.graphite)
                    }
                    .padding(.bottom, 10)
                    Spacer(minLength: 0)
                }
                if let pid = e.performanceId, e.isCurrent != true {
                    NavigationLink(value: Route.performance(pid)) { row.contentShape(Rectangle()) }.buttonStyle(.plain)
                } else {
                    row
                }
            }
        }
    }
}

struct StagePill: View {
    let stage: String
    var estimated = false

    var body: some View {
        let label = switch stage {
        case "early": "Early career"
        case "late": "Late career"
        default: "Peak years"
        }
        Text(estimated ? label + "*" : label)
            .font(.lexend(10, weight: .semibold, relativeTo: .caption2))
            .foregroundStyle(stage == "peak" ? Color.paper : Color.ember)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(stage == "peak" ? Color.ember : Color.clear, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.ember, lineWidth: stage == "peak" ? 0 : 1))
            .accessibilityLabel(estimated ? "\(label), estimated from age" : label)
    }
}

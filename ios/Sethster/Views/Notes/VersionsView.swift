import SwiftUI

/// "Versions" table for a Work: every performance of it in the collection (date, venue, conductor, leads,
/// duration, confidence). Uses `/works/{id}/versions` when the server has it, otherwise the Work's
/// performance list. "Compare" picks two rows for a side-by-side of their casts.
struct VersionsSection: View {
    let work: WorkDetail
    @Environment(AppModel.self) private var model
    @State private var serverVersions: [WorkVersion]?
    @State private var newestFirst = false
    @State private var comparing = false
    @State private var picked: [String] = []
    @State private var compare: ComparePair?

    struct ComparePair: Identifiable { let a: String; let b: String; var id: String { a + b } }

    private var versions: [WorkVersion] {
        let base = serverVersions ?? work.performances.map(WorkVersion.init)
        return base.sorted { a, b in
            let (x, y) = (a.date ?? "", b.date ?? "")
            return newestFirst ? x > y : x < y
        }
    }

    var body: some View {
        let rows = versions
        VStack(alignment: .leading, spacing: 0) {
            SectionTitle("Versions", trailing: "\(rows.count)")
            HStack {
                Picker("Order", selection: $newestFirst) {
                    Text("Oldest").tag(false)
                    Text("Newest").tag(true)
                }
                .pickerStyle(.segmented).frame(maxWidth: 200)
                Spacer()
                if rows.count >= 2, Compare.client(for: model.api) != nil {
                    // every version side by side: sections, tempi, cast; switch between them while playing
                    NavigationLink { CompareView(workId: work.id) } label: { Text("Side by side") }
                        .font(.uiSubheadline.weight(.medium))
                        .buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)
                }
                if rows.count >= 2 {
                    Button(comparing ? "Cancel" : "Compare") {
                        withAnimation(.snappy) { comparing.toggle(); picked = [] }
                    }
                    .font(.uiSubheadline.weight(.medium))
                    .buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)
                }
            }
            .padding(.horizontal).padding(.bottom, 8)
            if comparing {
                Text(picked.count < 2 ? "Choose two versions to compare their casts (\(picked.count) of 2)." : "")
                    .font(.uiFootnote).foregroundStyle(Color.graphite).padding(.horizontal).padding(.bottom, 6)
            }
            header
            ForEach(rows) { v in
                Group {
                    if comparing {
                        Button { toggle(v.id) } label: { row(v, selected: picked.contains(v.id)) }
                    } else {
                        NavigationLink(value: Route.performance(v.id)) { row(v, selected: false) }
                    }
                }
                .buttonStyle(.plain)
                Rectangle().fill(Color.rule).frame(height: 0.5).padding(.leading)
            }
        }
        .task(id: "\(work.id)|\(model.generation)") {
            serverVersions = try? await model.api.versions(workId: work.id).items.map(WorkVersion.init).nilIfEmpty
        }
        .sheet(item: $compare) { pair in
            NavigationStack { CastCompareView(workId: work.id, a: pair.a, b: pair.b).withRoutes() }
                .presentationDragIndicator(.visible)
        }
    }

    private func toggle(_ id: String) {
        if let i = picked.firstIndex(of: id) { picked.remove(at: i) } else { picked.append(id) }
        if picked.count == 2 {
            compare = ComparePair(a: picked[0], b: picked[1])
            comparing = false; picked = []
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("Date").frame(width: 76, alignment: .leading)
            Text("Cast").frame(maxWidth: .infinity, alignment: .leading)
            Text("Length").frame(width: 70, alignment: .trailing)
        }
        .font(.uiCaption2.weight(.semibold)).foregroundStyle(Color.graphite).textCase(.uppercase)
        .padding(.horizontal).padding(.vertical, 6)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.ink).frame(height: 1).padding(.horizontal) }
        .accessibilityHidden(true)
    }

    private func row(_ v: WorkVersion, selected: Bool) -> some View {
        let cast = v.cast
        return HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(Fmt.year(v.date) ?? "—").font(.uiHeadline.monospacedDigit())
                Text(Fmt.shortDate(v.date).split(separator: " ").dropLast().joined(separator: " "))
                    .font(.uiCaption2).foregroundStyle(Color.graphite)
            }
            .frame(width: 76, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                if let c = v.conductor { Text(c).font(.uiCallout.weight(.medium)).lineLimit(1) }
                if !cast.isEmpty { Text(cast).font(.uiFootnote).lineLimit(2) }
                if let venue = v.venue { Text(venue).font(.uiCaption).foregroundStyle(Color.graphite).lineLimit(1) }
                if let r = v.confidence { ConfidenceMeter(ratio: r, short: true) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            VStack(alignment: .trailing, spacing: 6) {
                Text(Fmt.longDuration(v.durationS) ?? "—").font(.uiCaption.monospacedDigit())
                    .lineLimit(1).minimumScaleFactor(0.8)
                    .foregroundStyle(Color.graphite)
                if comparing {
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.uiTitle3).foregroundStyle(selected ? Color.ember : Color.graphite)
                }
            }
            .frame(width: 70, alignment: .trailing)
        }
        .padding(.horizontal).padding(.vertical, 10)
        .background(selected ? Color.ember.opacity(0.08) : .clear)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

extension Array {
    var nilIfEmpty: Self? { isEmpty ? nil : self }
}

// MARK: - Cast compare

/// Two performances of one work side by side, role by role. Differences are marked.
struct CastCompareView: View {
    let workId: String
    let a: String
    let b: String
    @Environment(\.dismiss) private var dismiss
    struct Payload: Sendable { let a: PerformanceDetail; let b: PerformanceDetail }

    var body: some View {
        Loader(load: { api in
            async let x = api.performance(id: a)
            async let y = api.performance(id: b)
            return Payload(a: try await x, b: try await y)
        }) { content($0.a, $0.b) }
            .navigationTitle("Compare Casts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
    }

    private func credits(_ p: PerformanceDetail) -> [Credit] {
        p.credits.filter { c in p.program.count <= 1 || c.workId == workId || c.workId == nil }
    }

    /// Roles in a sensible order: conductor, then the cast in billing order, then ensembles and production.
    private func roles(_ x: PerformanceDetail, _ y: PerformanceDetail) -> [String] {
        var seen = Set<String>()
        let all = (credits(x) + credits(y)).filter { !CreditGroup.isProduction($0) }
        let ordered = all.enumerated().sorted { (CreditGroup.rank($0.element), $0.offset) < (CreditGroup.rank($1.element), $1.offset) }
        return ordered.compactMap { $0.element.role }.filter { seen.insert($0.lowercased()).inserted }
    }

    private func who(_ p: PerformanceDetail, _ role: String) -> [Credit] {
        credits(p).filter { $0.role?.lowercased() == role.lowercased() }
    }

    private func content(_ x: PerformanceDetail, _ y: PerformanceDetail) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 12) {
                    Color.clear.frame(width: 96, height: 1)
                    column(x)
                    column(y)
                }
                .padding(.horizontal).padding(.vertical, 12)
                Rectangle().fill(Color.ink).frame(height: 2).padding(.horizontal)
                ForEach(roles(x, y), id: \.self) { role in
                    let l = who(x, role), r = who(y, role)
                    let same = Set(l.map(\.person.id)) == Set(r.map(\.person.id))
                    HStack(alignment: .top, spacing: 12) {
                        Text(role).font(.uiFootnote).foregroundStyle(Color.graphite).frame(width: 96, alignment: .leading)
                        cell(l, differs: !same)
                        cell(r, differs: !same)
                    }
                    .padding(.horizontal).padding(.vertical, 8)
                    .accessibilityElement(children: .combine)
                    Rectangle().fill(Color.rule).frame(height: 0.5).padding(.horizontal)
                }
                HStack(spacing: 6) {
                    Rectangle().fill(Color.ember).frame(width: 3, height: 14)
                    Text("Marks a role sung or played by someone different.").font(.uiCaption).foregroundStyle(Color.graphite)
                }
                .padding()
            }
        }
        .background(Color.paper)
    }

    private func column(_ p: PerformanceDetail) -> some View {
        NavigationLink(value: Route.performance(p.id)) {
            VStack(alignment: .leading, spacing: 2) {
                Text(Fmt.year(p.date) ?? "—").font(.uiTitle2.monospacedDigit())
                Text(Fmt.date(p.date)).font(.uiCaption).foregroundStyle(Color.graphite)
                if let v = p.venue?.name { Text(v).font(.uiCaption2).foregroundStyle(Color.graphite).lineLimit(2) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
    }

    private func cell(_ credits: [Credit], differs: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if credits.isEmpty { Text("—").font(.uiCallout).foregroundStyle(Color.graphite) }
            ForEach(Array(credits.enumerated()), id: \.offset) { _, c in
                VStack(alignment: .leading, spacing: 2) {
                    Text(c.person.name).font(.uiCallout.weight(.medium)).fixedSize(horizontal: false, vertical: true)
                    if c.status != .confirmed { EvidenceBadge(status: c.status) }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 6)
        .overlay(alignment: .leading) { if differs { Rectangle().fill(Color.ember).frame(width: 3) } }
    }
}

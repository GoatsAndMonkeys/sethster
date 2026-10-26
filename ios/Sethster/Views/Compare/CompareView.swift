import SwiftUI

/// Every version of a piece side by side: cast / conductor / venue / year differences, then the
/// auto-segmented sections with per-version durations (relative tempo). Tapping a cell plays that
/// version from that section; while one version plays, "Switch" jumps to the equivalent moment in another.
struct CompareView: View {
    let workId: String
    var currentPerformanceId: String? = nil
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var data: CompareResponse?
    @State private var error: String?
    @State private var switcher = VersionSwitcher.shared

    private let labelWidth: CGFloat = 150
    private let colWidth: CGFloat = 112

    var body: some View {
        Group {
            if let d = data { content(d) }
            else if let error { ContentUnavailableView("Couldn't load", systemImage: "wifi.exclamationmark", description: Text(error)) }
            else { ProgressView().controlSize(.large).frame(maxWidth: .infinity, maxHeight: .infinity) }
        }
        .navigationTitle("Compare Versions")
        .navigationBarTitleDisplayMode(.inline)
        .background(Color.paper)
        .task(id: "\(workId)|\(model.generation)") { await load() }
    }

    private func load() async {
        guard let c = Compare.client(for: model.api) else { error = "Compare needs the Sethster server (not available in demo mode)."; return }
        do { data = try await c.compare(workId: workId); error = nil }
        catch { self.error = error.localizedDescription }
    }

    // MARK: - Layout

    private func content(_ d: CompareResponse) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header(d)
                ScrollView(.horizontal, showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 0) {
                        columnHeaders(d)
                        Rectangle().fill(Color.ink).frame(height: 2)
                        totalsRow(d)
                        if d.sections.rows.isEmpty {
                            Text("No section maps yet for these recordings.")
                                .font(.uiFootnote).foregroundStyle(Color.graphite).padding(.vertical, 12)
                        }
                        ForEach(groupedRows(d), id: \.0) { group, rows in
                            if let group { Eyebrow(group).padding(.top, 14).padding(.bottom, 4) }
                            ForEach(rows) { row in sectionRow(row, d) }
                        }
                        Eyebrow("Cast").padding(.top, 22).padding(.bottom, 4)
                        ForEach(d.differences.roles) { role in roleRow(role, d) }
                    }
                    .padding(.horizontal)
                }
                legend.padding()
            }
        }
    }

    private func header(_ d: CompareResponse) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(d.canonical.title).font(.uiTitle2)
            Text([d.canonical.composer?.name, d.canonical.catalogue].compactMap { $0 }.joined(separator: " · "))
                .font(.uiSubheadline).foregroundStyle(Color.graphite)
            let diff = d.differences
            Text(summary(d, diff)).font(.uiFootnote).foregroundStyle(Color.graphite)
            if isPlayingThisWork(d), let cur = player.performance {
                Text("Playing the \(d.versions.first { $0.performanceId == cur.id }?.year.map(String.init) ?? "") version. Switch jumps to the same moment in another.")
                    .font(.uiCaption).foregroundStyle(Color.ember)
            }
        }
        .padding()
    }

    private func summary(_ d: CompareResponse, _ diff: CompareDifferences) -> String {
        var parts = ["\(d.versions.count) versions"]
        if let y = diff.years { parts.append(y.span > 0 ? "\(y.min)–\(y.max)" : "\(y.min)") }
        if diff.conductorDiffers { parts.append("different conductors") }
        if diff.venueDiffers { parts.append("\(diff.venues.count) venues") }
        return parts.joined(separator: " · ")
    }

    private func columnHeaders(_ d: CompareResponse) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Color.clear.frame(width: labelWidth, height: 1)
            ForEach(d.versions) { v in
                VStack(alignment: .leading, spacing: 3) {
                    Text(v.year.map(String.init) ?? "—").font(.uiTitle3.monospacedDigit())
                    if let c = v.conductor { Text(c.name).font(.uiCaption.weight(.medium)).lineLimit(2) }
                    if let venue = v.venue?.name { Text(venue).font(.uiCaption2).foregroundStyle(Color.graphite).lineLimit(2) }
                    if let lead = v.leadSingers.first { Text(lead.person.name).font(.uiCaption2).lineLimit(1) }
                    columnAction(v, d)
                }
                .frame(width: colWidth, alignment: .leading)
                .padding(.vertical, 8)
                .background(v.performanceId == (player.performance?.id ?? currentPerformanceId) ? Color.ember.opacity(0.08) : .clear)
            }
        }
    }

    @ViewBuilder private func columnAction(_ v: CompareVersion, _ d: CompareResponse) -> some View {
        let playingThis = player.performance?.id == v.performanceId
        if playingThis {
            Label("Playing", systemImage: "waveform").font(.uiCaption2).foregroundStyle(Color.ember)
        } else if isPlayingThisWork(d) {
            Button("Switch") { Task { await switcher.switchTo(v.performanceId, player: player, api: model.api) } }
                .font(.uiCaption).buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.mini)
                .disabled(switcher.busy)
        } else {
            Button { Task { await play(v, at: nil) } } label: { Label("Play", systemImage: "play.fill") }
                .font(.uiCaption).buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.mini)
        }
    }

    private func totalsRow(_ d: CompareResponse) -> some View {
        let durs = d.versions.compactMap(\.musicS)
        let med = durs.isEmpty ? nil : durs.sorted()[durs.count / 2]
        return HStack(spacing: 8) {
            Text("Total music").font(.uiFootnote.weight(.semibold)).frame(width: labelWidth, alignment: .leading)
            ForEach(d.versions) { v in
                durationCell(v.musicS, relative: med.flatMap { m in v.musicS.map { $0 / m } }, review: false)
                    .frame(width: colWidth, alignment: .leading)
            }
        }
        .padding(.vertical, 8)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.rule).frame(height: 0.5) }
    }

    private func groupedRows(_ d: CompareResponse) -> [(String?, [SectionRow])] {
        var out: [(String?, [SectionRow])] = []
        for r in d.sections.rows {
            if let last = out.last, last.0 == r.group { out[out.count - 1].1.append(r) }
            else { out.append((r.group, [r])) }
        }
        return out
    }

    private func sectionRow(_ row: SectionRow, _ d: CompareResponse) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(cleanLabel(row.label ?? row.key)).font(.uiFootnote).lineLimit(3)
                .frame(width: labelWidth, alignment: .leading)
            ForEach(d.versions) { v in
                Group {
                    if let cell = row.cells[v.performanceId] {
                        Button { Task { await play(v, at: cell.start) } } label: {
                            durationCell(cell.durationS, relative: cell.relative, review: cell.needsReview ?? false)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Play \(cleanLabel(row.label ?? "")) in the \(v.year.map(String.init) ?? "") version")
                    } else {
                        Text("—").font(.uiFootnote).foregroundStyle(Color.graphite)
                    }
                }
                .frame(width: colWidth, alignment: .leading)
            }
        }
        .padding(.vertical, 6)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.rule).frame(height: 0.5) }
    }

    /// Duration with a small bar: wider = slower than the median version of this section.
    private func durationCell(_ seconds: Double?, relative: Double?, review: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 3) {
                Text(Fmt.clock(seconds)).font(.uiFootnote.monospacedDigit())
                if review { Image(systemName: "questionmark.circle").font(.uiCaption2).foregroundStyle(Color.graphite) }
            }
            if let r = relative {
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.rule).frame(width: 80, height: 4)
                    Capsule().fill(r > 1.08 ? Color.ember : (r < 0.92 ? Color.ink : Color.graphite))
                        .frame(width: max(4, min(80, 40 * r)), height: 4)
                }
                Text(r >= 1 ? String(format: "+%.0f%%", (r - 1) * 100) : String(format: "−%.0f%%", (1 - r) * 100))
                    .font(.uiCaption2.monospacedDigit()).foregroundStyle(Color.graphite)
            }
        }
        .contentShape(Rectangle())
    }

    private func roleRow(_ role: RoleDifference, _ d: CompareResponse) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(role.role).font(.uiFootnote).foregroundStyle(Color.graphite).lineLimit(2)
                .frame(width: labelWidth, alignment: .leading)
            ForEach(d.versions) { v in
                let names = role.byVersion[v.performanceId] ?? []
                Text(names.isEmpty ? "—" : names.map(\.name).joined(separator: ", "))
                    .font(.uiFootnote.weight(names.isEmpty ? .regular : .medium)).lineLimit(3)
                    .frame(width: colWidth, alignment: .leading)
                    .padding(.leading, 6)
                    .overlay(alignment: .leading) { if role.differs { Rectangle().fill(Color.ember).frame(width: 3) } }
            }
        }
        .padding(.vertical, 6)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.rule).frame(height: 0.5) }
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Rectangle().fill(Color.ember).frame(width: 3, height: 14)
                Text("Marks a role sung or played by someone different.").font(.uiCaption).foregroundStyle(Color.graphite)
            }
            Text("Bars compare each section with the median version: longer and red = slower (or applause included). Sections come from automatic alignment; ? marks one whose boundaries are unconfirmed.")
                .font(.uiCaption).foregroundStyle(Color.graphite)
        }
    }

    // MARK: - Actions

    private func isPlayingThisWork(_ d: CompareResponse) -> Bool {
        guard let cur = player.performance else { return false }
        return d.versions.contains { $0.performanceId == cur.id }
    }

    private func cleanLabel(_ s: String) -> String {
        // "Act One- 'Come in quest'ora bruna' (Amelia)" -> "'Come in quest'ora bruna' (Amelia)"
        s.replacingOccurrences(of: #"^(?:Act|Atto|Akt)\s+\w+\s*[-:]\s*"#, with: "", options: .regularExpression)
    }

    private func play(_ v: CompareVersion, at point: SectionPoint?) async {
        let api = model.api, epoch = AppServices.account.epoch
        do {
            let perf = try await api.performance(id: v.performanceId)
            guard epoch == AppServices.account.epoch else { return }
            let sorted = perf.tracks.sorted { $0.index < $1.index }
            let target = point?.trackId ?? v.firstTrackId
            let pos = target.flatMap { t in sorted.firstIndex { $0.id == t } } ?? 0
            player.play(perf, from: pos, api: api)
            if let t = point?.t, t > 0.5 {
                for _ in 0..<60 where player.currentDuration <= 0 { try? await Task.sleep(for: .milliseconds(250)) }
                if epoch == AppServices.account.epoch { player.seek(to: t) }
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

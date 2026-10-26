import SwiftUI

/// Jumps the player to the equivalent moment in another version of the piece, and remembers the pair so
/// an A/B toggle can flip back and forth. Shared by Now Playing and the Compare view.
@Observable
@MainActor
final class VersionSwitcher {
    static let shared = VersionSwitcher()

    /// The two performances of the last switch (A/B toggle flips between them).
    private(set) var pair: (a: String, b: String)?
    private(set) var busy = false
    private(set) var lastMessage: String?
    /// "moment" (exact, via the alignment map when there is one) or "section" (start of the same number).
    var mode: String = "moment"

    func partner(of pid: String) -> String? {
        guard let p = pair else { return nil }
        return p.a == pid ? p.b : (p.b == pid ? p.a : nil)
    }

    /// Switch `player` from what it is playing now to `target`, at the matching point.
    func switchTo(_ target: String, player: PlayerController, api: any SethsterAPI) async {
        guard let client = Compare.client(for: api), let cur = player.performance, let track = player.currentTrack,
              cur.id != target, !busy else { return }
        busy = true
        defer { busy = false }
        let epoch = AppServices.account.epoch
        do {
            let jump = try await client.jump(from: cur.id, to: target, trackId: track.id, t: player.currentTime, mode: mode)
            let perf = try await api.performance(id: target)
            // A sign-out or server switch while waiting tore playback down; don't bring it back.
            guard epoch == AppServices.account.epoch else { return }
            let sorted = perf.tracks.sorted { $0.index < $1.index }
            let pos = sorted.firstIndex { $0.id == jump.trackId } ?? 0
            pair = (cur.id, target)
            player.play(perf, from: pos, api: api)
            // wait for the new item to load before seeking (AVPlayer ignores seeks on unready items)
            for _ in 0..<60 where player.currentDuration <= 0 { try? await Task.sleep(for: .milliseconds(250)) }
            if jump.t > 0.5, epoch == AppServices.account.epoch { player.seek(to: jump.t) }
            lastMessage = Self.describe(jump)
        } catch {
            lastMessage = "Couldn't switch: \(error.localizedDescription)"
        }
    }

    static func describe(_ j: JumpTarget) -> String {
        switch j.method {
        case "alignment": "Matched to the same moment"
        case "section": "Matched to the same section" + (j.section?.title.map { ": \($0)" } ?? "")
        case "proportional": "Approximate position (no section map)"
        default: "From the start of the work"
        }
    }
}

/// Now Playing panel: the section playing now, and other versions of the piece with a Switch button and an A/B toggle.
struct VersionSwitchPanel: View {
    let perf: PerformanceDetail
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var switcher = VersionSwitcher.shared
    @State private var sections: SectionsResponse?
    @State private var compare: CompareResponse?
    @State private var showCompare = false

    private var workId: String? { player.currentTrack?.workId ?? perf.program.first?.workId }

    var body: some View {
        let others = (compare?.versions ?? []).filter { $0.performanceId != perf.id }
        let current = player.currentTrack.flatMap { sections?.section(at: $0.id, t: player.currentTime, tracks: perf.tracks) }
        VStack(alignment: .leading, spacing: 10) {
            if let s = current {
                VStack(alignment: .leading, spacing: 2) {
                    Eyebrow([s.group, "Now"].compactMap { $0 }.joined(separator: " · "))
                    Text(s.displayTitle).font(.uiSubheadline.weight(.medium)).lineLimit(2)
                    if s.needsReview { Text("Section boundaries unconfirmed").font(.uiCaption2).foregroundStyle(.tertiary) }
                }
            }
            if !others.isEmpty {
                HStack {
                    Eyebrow("Other versions")
                    Spacer()
                    Picker("Match", selection: $switcher.mode) {
                        Text("Same moment").tag("moment")
                        Text("Section start").tag("section")
                    }
                    .pickerStyle(.menu).font(.uiCaption).controlSize(.small)
                }
                if let partner = switcher.partner(of: perf.id), others.contains(where: { $0.performanceId == partner }) {
                    Button {
                        Task { await switcher.switchTo(partner, player: player, api: model.api) }
                    } label: {
                        Label("A/B: back to \(label(for: partner, in: others))", systemImage: "arrow.left.arrow.right")
                            .font(.uiSubheadline.weight(.medium))
                    }
                    .buttonStyle(.borderedProminent).buttonBorderShape(.capsule).tint(Color.ember)
                    .disabled(switcher.busy)
                }
                ForEach(others.prefix(6)) { v in
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(v.year.map(String.init) ?? Fmt.year(v.date) ?? "—").font(.uiCallout.monospacedDigit().weight(.medium))
                            Text([v.conductor?.name, v.venue?.name].compactMap { $0 }.joined(separator: " · "))
                                .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        Button("Switch") { Task { await switcher.switchTo(v.performanceId, player: player, api: model.api) } }
                            .buttonStyle(.bordered).buttonBorderShape(.capsule).controlSize(.small)
                            .disabled(switcher.busy)
                            .accessibilityLabel("Switch to the \(v.year.map(String.init) ?? "") version")
                    }
                }
                if workId != nil {
                    Button { showCompare = true } label: {
                        Label("Compare all versions", systemImage: "rectangle.split.3x1").font(.uiFootnote)
                    }
                }
                if let msg = switcher.lastMessage {
                    Text(msg).font(.uiCaption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(isPresented: $showCompare) {
            if let w = workId {
                NavigationStack {
                    CompareView(workId: w, currentPerformanceId: perf.id).withRoutes()
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showCompare = false } } }
                }
                .presentationDragIndicator(.visible)
            }
        }
        .task(id: perf.id) {
            guard let c = Compare.client(for: model.api) else { return }
            // Debug/screenshot aid: launch argument `-openCompare 1`.
            if UserDefaults.standard.bool(forKey: "openCompare") {
                try? await Task.sleep(for: .milliseconds(2500)); showCompare = true
            }
            sections = try? await c.sections(performanceId: perf.id)
        }
        .task(id: "\(perf.id)|\(workId ?? "")") {
            guard let c = Compare.client(for: model.api), let w = workId else { compare = nil; return }
            compare = try? await c.compare(workId: w)
        }
    }

    private func label(for pid: String, in versions: [CompareVersion]) -> String {
        guard let v = versions.first(where: { $0.performanceId == pid }) else { return "the other version" }
        return [v.year.map(String.init), v.conductor.map { $0.name.split(separator: " ").last.map(String.init) ?? $0.name }].compactMap { $0 }.joined(separator: " ")
    }
}

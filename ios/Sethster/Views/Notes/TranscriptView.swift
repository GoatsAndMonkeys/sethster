import SwiftUI

/// "Broadcast" transcript in Now Playing: the announcer and intermission speech for the current performance,
/// following playback by timestamp. Tapping a line seeks there (jumping tracks if needed). Hidden when the
/// server has no transcript for this performance (404) or it holds no lines.
struct TranscriptPanel: View {
    let perf: PerformanceDetail
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @State private var lines: [TranscriptLine] = []
    @State private var showAll = false

    var body: some View {
        // A VStack (not a Group) so the view exists, and its .task runs, while there are no lines yet.
        VStack(spacing: 0) {
            Color.clear.frame(height: 0)
            if !lines.isEmpty {
                content
            }
        }
        .task(id: "\(perf.id)|\(model.generation)") {
            lines = []
            guard let t = try? await model.api.transcript(performanceId: perf.id) else { return }
            lines = t.segments.filter { !$0.text.trimmingCharacters(in: .whitespaces).isEmpty }
        }
    }

    private var tracks: [Track] { perf.tracks.sorted { $0.index < $1.index } }

    private func trackPosition(_ line: TranscriptLine) -> Int? {
        tracks.firstIndex { line.belongs(to: $0) }
    }

    /// Lines for the current track, or for every track when "All" is on.
    private var shown: [(line: TranscriptLine, track: Int)] {
        lines.compactMap { l in trackPosition(l).map { (l, $0) } }
            .filter { showAll || $0.track == player.currentIndex }
            .sorted { ($0.track, $0.line.start) < ($1.track, $1.line.start) }
    }

    private var activeID: String? {
        let t = player.currentTime
        return shown.last { $0.track == player.currentIndex && $0.line.start <= t + 0.25 }?.line.id
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Eyebrow("Broadcast")
                Spacer()
                Picker("Show", selection: $showAll) {
                    Text("This track").tag(false)
                    Text("All").tag(true)
                }
                .pickerStyle(.segmented).frame(width: 180)
            }
            let items = shown
            if items.isEmpty {
                Text("No speech on this track.").font(.uiFootnote).foregroundStyle(Color.graphite)
                    .padding(.vertical, 12)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(items.enumerated()), id: \.element.line.id) { i, item in
                                if showAll, i == 0 || items[i - 1].track != item.track {
                                    Text(tracks[item.track].shortTitle).font(.uiCaption.weight(.semibold))
                                        .foregroundStyle(Color.graphite).padding(.top, 8)
                                }
                                line(item.line, track: item.track, active: item.line.id == activeID)
                                    .id(item.line.id)
                            }
                        }
                    }
                    .frame(maxHeight: 260)
                    .onChange(of: activeID) { _, id in
                        guard let id else { return }
                        withAnimation(.easeInOut(duration: 0.3)) { proxy.scrollTo(id, anchor: .center) }
                    }
                }
            }
            Text("Machine transcript of the off-air recording. It may contain errors.")
                .font(.uiCaption2).foregroundStyle(Color.graphite)
        }
        .padding(16)
        .background(Color.card.opacity(0.9), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .padding(.horizontal)
    }

    private func line(_ l: TranscriptLine, track: Int, active: Bool) -> some View {
        Button { player.seek(track: track, to: l.start) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(Fmt.clock(l.start)).font(.uiCaption2.monospacedDigit()).foregroundStyle(Color.graphite)
                    .frame(width: 42, alignment: .trailing)
                VStack(alignment: .leading, spacing: 1) {
                    Text(l.text)
                        .font(active ? .uiCallout.weight(.medium) : .uiCallout)
                        .foregroundStyle(active ? Color.ember : Color.ink.opacity(0.75))
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(l.text), at \(Fmt.clock(l.start))")
        .accessibilityHint("Plays from here")
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

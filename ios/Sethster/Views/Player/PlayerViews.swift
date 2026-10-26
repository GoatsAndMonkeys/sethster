import AVKit
import SwiftUI

struct MiniPlayer: View {
    @Environment(PlayerController.self) private var player

    var body: some View {
        if let perf = player.performance {
            HStack(spacing: 10) {
                ArtworkView(path: perf.artworkUrl, title: perf.title, seed: perf.collection ?? perf.id, cornerRadius: 5)
                    .frame(width: 34, height: 34)
                VStack(alignment: .leading, spacing: 0) {
                    Text(player.currentTrack?.shortTitle ?? perf.title)
                        .font(.uiSubheadline.weight(.medium)).lineLimit(1)
                    Text(perf.title).font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button { player.togglePlayPause() } label: {
                    Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                        .font(.uiTitle3).frame(width: 36, height: 36).contentShape(Rectangle())
                }
                .accessibilityLabel(player.isPlaying ? "Pause" : "Play")
                Button { player.next() } label: {
                    Image(systemName: "forward.fill").font(.uiBody).frame(width: 32, height: 36).contentShape(Rectangle())
                }
                .accessibilityLabel("Next track")
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 10)
            .contentShape(Rectangle())
            .onTapGesture { player.showNowPlaying = true }
        }
    }
}

struct NowPlayingView: View {
    @Environment(PlayerController.self) private var player
    @Environment(\.dismiss) private var dismiss
    @State private var scrub: Double = 0

    var body: some View {
        if let perf = player.performance {
            ScrollViewReader { proxy in
            ScrollView {
                    VStack(spacing: 22) {
                        ArtworkView(path: perf.artworkUrl, title: perf.title, seed: perf.collection ?? perf.id, cornerRadius: 14)
                            .frame(maxWidth: 340)
                            .shadow(color: .black.opacity(0.25), radius: 24, y: 12)
                            .scaleEffect(player.isPlaying ? 1 : 0.92)
                            .animation(.spring(duration: 0.45), value: player.isPlaying)
                            .padding(.top, 28)

                        VStack(spacing: 6) {
                            if let track = player.currentTrack {
                                Eyebrow("Track \(track.index) of \(player.tracks.count)"
                                        + (track.segment.isMusic ? "" : " · \(track.segment.rawValue)"))
                                Text(track.shortTitle)
                                    .font(.display(24, weight: .semibold))
                                    .multilineTextAlignment(.center)
                                    .lineLimit(3)
                            }
                            Text(perf.title).font(.display(18)).foregroundStyle(.secondary)
                            if let sub = perf.subtitle {
                                Text(sub).font(.uiSubheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                            }
                            Text([Fmt.date(perf.date), perf.venue?.name].compactMap { $0 }.joined(separator: " · "))
                                .font(.uiCaption).foregroundStyle(.tertiary)
                        }
                        .padding(.horizontal)

                        scrubber
                        controls

                        if let msg = player.errorMessage {
                            Label(msg, systemImage: "exclamationmark.circle")
                                .font(.uiFootnote).foregroundStyle(Color.ember)
                                .padding(.horizontal)
                        }

                        HStack {
                            Text("Performance \(Fmt.clock(player.performanceElapsed)) / \(Fmt.clock(player.performanceDuration))")
                                .font(.uiCaption.monospacedDigit()).foregroundStyle(.secondary)
                            Spacer()
                            RoutePicker().frame(width: 32, height: 32)
                        }
                        .padding(.horizontal, 24)

                        VersionSwitchPanel(perf: perf).id("versions")   // current section + switch / A-B to other versions

                        TranscriptPanel(perf: perf).id("transcript")

                        queue
                    }
                    .padding(.bottom, 40)
            }
            .task {
                // Debug/screenshot aid: launch argument `-scrollTo transcript`.
                if let target = UserDefaults.standard.string(forKey: "scrollTo"), ["transcript", "versions"].contains(target) {
                    try? await Task.sleep(for: .milliseconds(3000))
                    withAnimation { proxy.scrollTo(target, anchor: .top) }
                }
            }
            }
            .background(backdrop(perf))
        } else {
            ContentUnavailableView("Nothing playing", systemImage: "music.note")
        }
    }

    private func backdrop(_ perf: PerformanceDetail) -> some View {
        let (a, _) = Palette.pair(for: perf.collection ?? perf.id)
        return LinearGradient(colors: [a.opacity(0.35), Color(.systemBackground)], startPoint: .top, endPoint: .center)
            .ignoresSafeArea()
    }

    private var scrubber: some View {
        @Bindable var player = player
        let duration = max(player.currentDuration, 1)
        return VStack(spacing: 4) {
            Slider(value: Binding(
                get: { player.isScrubbing ? scrub : min(player.currentTime, duration) },
                set: { scrub = $0 }),
                   in: 0...duration) { editing in
                if editing { scrub = player.currentTime; player.isScrubbing = true }
                else { player.seek(to: scrub); player.isScrubbing = false }
            }
            HStack {
                Text(Fmt.clock(player.isScrubbing ? scrub : player.currentTime))
                Spacer()
                if player.isBuffering { ProgressView().controlSize(.mini) }
                Spacer()
                Text("-" + Fmt.clock(duration - (player.isScrubbing ? scrub : player.currentTime)))
            }
            .font(.uiCaption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 24)
    }

    private var controls: some View {
        HStack(spacing: 30) {
            Button { player.previous() } label: { Image(systemName: "backward.end.fill").font(.uiTitle2) }
                .accessibilityLabel("Previous track")
            Button { player.skip(by: -15) } label: { Image(systemName: "gobackward.15").font(.uiTitle2) }
                .accessibilityLabel("Back 15 seconds")
            Button { player.togglePlayPause() } label: {
                Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 64)).symbolRenderingMode(.hierarchical)
            }
            .accessibilityLabel(player.isPlaying ? "Pause" : "Play")
            Button { player.skip(by: 30) } label: { Image(systemName: "goforward.30").font(.uiTitle2) }
                .accessibilityLabel("Forward 30 seconds")
            Button { player.next() } label: { Image(systemName: "forward.end.fill").font(.uiTitle2) }
                .accessibilityLabel("Next track")
                .disabled(player.currentIndex + 1 >= player.tracks.count)
        }
        .buttonStyle(.plain)
        .foregroundStyle(.primary)
    }

    private var queue: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow("Tracks").padding(.horizontal, 24).padding(.bottom, 6)
            ForEach(Array(player.tracks.enumerated()), id: \.element.id) { i, track in
                Button { player.jump(to: i) } label: {
                    TrackRow(track: track, isCurrent: i == player.currentIndex, isPlaying: player.isPlaying, showNote: false)
                        .padding(.horizontal, 24).padding(.vertical, 8)
                }
                .buttonStyle(.plain)
                .id("track-\(i)")
            }
        }
    }
}

/// One line of a track list; non-music segments (announcer, intermission, applause) are styled quietly.
struct TrackRow: View {
    let track: Track
    var isCurrent = false
    var isPlaying = false
    var showNote = true

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Group {
                if isCurrent {
                    Image(systemName: isPlaying ? "waveform" : "pause.fill")
                        .symbolEffect(.variableColor.iterative, isActive: isPlaying)
                        .foregroundStyle(.tint)
                } else if track.segment.isMusic {
                    Text("\(track.index)").monospacedDigit().foregroundStyle(.secondary)
                } else {
                    Image(systemName: track.segmentIcon).foregroundStyle(.tertiary)
                }
            }
            .font(.uiFootnote)
            .frame(width: 24, alignment: .trailing)

            VStack(alignment: .leading, spacing: 2) {
                Text(track.shortTitle)
                    .font(track.segment.isMusic ? .uiBody : .lexend(14, weight: .light, relativeTo: .subheadline))
                    .foregroundStyle(track.segment.isMusic ? (isCurrent ? AnyShapeStyle(.tint) : AnyShapeStyle(.primary)) : AnyShapeStyle(.secondary))
                    .lineLimit(2)
                if showNote, let note = track.detailNote {
                    Text(note).font(.uiCaption).foregroundStyle(.tertiary).lineLimit(2)
                }
            }
            Spacer(minLength: 8)
            Text(Fmt.clock(track.durationS)).font(.uiCaption.monospacedDigit()).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }
}

struct RoutePicker: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let v = AVRoutePickerView()
        v.prioritizesVideoDevices = false
        return v
    }
    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {}
}

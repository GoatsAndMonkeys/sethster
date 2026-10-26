import AVFoundation
import MediaPlayer
import Observation
import UIKit

/// Plays a whole performance as one continuous queue (AVQueuePlayer pre-buffers the next item, which
/// keeps act/movement boundaries as close to gapless as separate files allow). Publishes state for the
/// mini-player and Now Playing sheet, and drives the lock screen / Control Center.
@Observable
@MainActor
final class PlayerController {
    private(set) var performance: PerformanceDetail?
    private(set) var tracks: [Track] = []
    private(set) var currentIndex: Int = 0
    private(set) var isPlaying = false
    private(set) var isBuffering = false
    private(set) var currentTime: Double = 0
    private(set) var currentDuration: Double = 0
    private(set) var errorMessage: String?
    private(set) var artwork: UIImage?
    var isScrubbing = false
    var showNowPlaying = false

    var currentTrack: Track? { tracks.indices.contains(currentIndex) ? tracks[currentIndex] : nil }
    var hasQueue: Bool { performance != nil && !tracks.isEmpty }

    /// Elapsed time across the whole performance.
    var performanceElapsed: Double {
        tracks.prefix(currentIndex).compactMap(\.durationS).reduce(0, +) + currentTime
    }
    var performanceDuration: Double { tracks.compactMap(\.durationS).reduce(0, +) }

    @ObservationIgnored private let player = AVQueuePlayer()
    @ObservationIgnored private var itemIndex: [ObjectIdentifier: Int] = [:]
    @ObservationIgnored private var timeObserver: Any?
    @ObservationIgnored private var api: (any SethsterAPI)?
    @ObservationIgnored private var commandsConfigured = false
    @ObservationIgnored private var lastInfoKey = ""
    @ObservationIgnored private var mpArtwork: MPMediaItemArtwork?

    init() {
        player.automaticallyWaitsToMinimizeStalling = true
        player.actionAtItemEnd = .advance
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
                                                      queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil,
                                               queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let shouldResume = ((note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt) ?? 0)
                & AVAudioSession.InterruptionOptions.shouldResume.rawValue != 0
            MainActor.assumeIsolated {
                guard let self, let raw, let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
                if type == .ended, shouldResume { self.player.play() }
                self.tick()
            }
        }
    }

    static let offlineMessage = "Not available offline. Demo recordings have no audio; connect to the Sethster server to listen."

    // MARK: - Public controls

    func play(_ perf: PerformanceDetail, from index: Int = 0, api: any SethsterAPI) {
        AppServices.radio.stop()   // one player at a time
        self.api = api
        errorMessage = nil
        guard !perf.tracks.isEmpty else { errorMessage = "This performance has no tracks."; return }
        if perf.tracks.allSatisfy({ AppServices.rotation.localURL(trackID: $0.id) == nil
            && api.resolve($0.streamUrl) == nil }) {
            errorMessage = Self.offlineMessage
        }
        if performance?.id != perf.id { artwork = nil; mpArtwork = nil; loadArtwork(perf, api: api) }
        performance = perf
        tracks = perf.tracks.sorted { $0.index < $1.index }
        configureSessionAndCommands()
        enqueue(from: min(max(index, 0), tracks.count - 1))
        // Demo / offline: nothing playable was queued, so stay paused instead of "buffering" forever.
        if player.items().isEmpty { player.pause() } else { player.play() }
        tick()
    }

    func togglePlayPause() { isPlaying ? pause() : resume() }

    func pause() { player.pause(); tick() }

    func resume() {
        if player.currentItem == nil, hasQueue { enqueue(from: currentIndex) }
        try? AVAudioSession.sharedInstance().setActive(true)
        player.play(); tick()
    }

    func next() {
        guard currentIndex + 1 < tracks.count else { return }
        player.advanceToNextItem()
        if player.currentItem == nil { enqueue(from: currentIndex + 1) }
        player.play(); tick()
    }

    func previous() {
        if currentTime > 4 || currentIndex == 0 { seek(to: 0); return }
        jump(to: currentIndex - 1)
    }

    func jump(to index: Int) {
        guard tracks.indices.contains(index) else { return }
        enqueue(from: index); player.play(); tick()
    }

    func seek(to seconds: Double) {
        currentTime = seconds
        player.seek(to: CMTime(seconds: seconds, preferredTimescale: 600), toleranceBefore: .zero,
                    toleranceAfter: .zero) { @Sendable [weak self] _ in
            Task { @MainActor in self?.tick(); self?.updateNowPlayingInfo(force: true) }
        }
    }

    /// Seek to `seconds` within the track at queue position `track`, switching tracks first if needed
    /// (waits up to 15 s for the new item to load before seeking).
    func seek(track: Int, to seconds: Double) {
        guard tracks.indices.contains(track) else { return }
        if track == currentIndex, player.currentItem != nil { seek(to: seconds); if !isPlaying { resume() }; return }
        jump(to: track)
        guard let item = player.currentItem else { return }
        Task {
            for _ in 0..<60 where item.status == .unknown { try? await Task.sleep(for: .milliseconds(250)) }
            guard item.status == .readyToPlay, player.currentItem === item else { return }
            seek(to: seconds)
        }
    }

    func skip(by delta: Double) { seek(to: min(max(0, currentTime + delta), max(currentDuration - 1, 0))) }

    func stop() {
        player.pause(); player.removeAllItems()
        performance = nil; tracks = []; itemIndex = [:]; currentIndex = 0
        isPlaying = false; currentTime = 0; currentDuration = 0; errorMessage = nil
        artwork = nil; mpArtwork = nil; lastInfoKey = ""; api = nil; showNowPlaying = false
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    // MARK: - Queue

    private func enqueue(from index: Int) {
        player.pause()
        player.removeAllItems()
        itemIndex = [:]
        guard let api else { return }
        for i in index..<tracks.count {
            let local = AppServices.rotation.localURL(trackID: tracks[i].id)
            guard let url = local ?? api.mediaURL(tracks[i].streamUrl) else { continue }
            let headers = local == nil ? api.authHeaders(for: url) : [:]
            let asset = AVURLAsset(url: url, options: headers.isEmpty ? nil : ["AVURLAssetHTTPHeaderFieldsKey": headers])
            let item = AVPlayerItem(asset: asset)
            item.preferredForwardBufferDuration = 30
            itemIndex[ObjectIdentifier(item)] = i
            if player.canInsert(item, after: nil) { player.insert(item, after: nil) }
        }
        currentIndex = index
        currentTime = 0
        currentDuration = tracks[index].durationS ?? 0
    }

    private func tick() {
        if let item = player.currentItem, let i = itemIndex[ObjectIdentifier(item)] {
            currentIndex = i
            if item.status == .failed {
                errorMessage = item.error?.localizedDescription ?? "Could not play this track."
                // AVPlayer hides the HTTP status; a revoked session surfaces through the shared auth path.
                if (item.asset as? AVURLAsset)?.url.isFileURL == false { AppServices.account.verifySession() }
            } else if item.status == .readyToPlay, errorMessage != Self.offlineMessage {
                errorMessage = nil
            }
            let d = item.duration.seconds
            currentDuration = d.isFinite && d > 0 ? d : (tracks[i].durationS ?? 0)
        } else if hasQueue, player.items().isEmpty, currentIndex == tracks.count - 1, currentTime > 0 {
            // Finished the last track.
            currentTime = 0
        }
        if !isScrubbing {
            let t = player.currentTime().seconds
            currentTime = t.isFinite ? t : 0
        }
        isPlaying = player.timeControlStatus != .paused
        isBuffering = player.timeControlStatus == .waitingToPlayAtSpecifiedRate
        updateNowPlayingInfo()
    }

    // MARK: - System integration

    private func configureSessionAndCommands() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, policy: .longFormAudio)
        try? session.setActive(true)
        guard !commandsConfigured else { return }
        commandsConfigured = true
        let cc = MPRemoteCommandCenter.shared()
        cc.playCommand.addTarget { @Sendable [weak self] _ in Task { @MainActor in self?.resume() }; return .success }
        cc.pauseCommand.addTarget { @Sendable [weak self] _ in Task { @MainActor in self?.pause() }; return .success }
        cc.togglePlayPauseCommand.addTarget { @Sendable [weak self] _ in
            Task { @MainActor in self?.togglePlayPause() }; return .success
        }
        cc.nextTrackCommand.addTarget { @Sendable [weak self] _ in Task { @MainActor in self?.next() }; return .success }
        cc.previousTrackCommand.addTarget { @Sendable [weak self] _ in Task { @MainActor in self?.previous() }; return .success }
        cc.changePlaybackPositionCommand.addTarget { @Sendable [weak self] event in
            guard let e = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let t = e.positionTime
            Task { @MainActor in self?.seek(to: t) }
            return .success
        }
        cc.skipForwardCommand.isEnabled = false
        cc.skipBackwardCommand.isEnabled = false
    }

    private func updateNowPlayingInfo(force: Bool = false) {
        guard let perf = performance, let track = currentTrack else { return }
        let key = "\(perf.id)|\(currentIndex)|\(isPlaying)|\(Int(currentDuration))"
        guard force || key != lastInfoKey else { return }
        lastInfoKey = key
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: track.shortTitle,
            MPMediaItemPropertyArtist: perf.subtitle ?? perf.composers.map(\.name).joined(separator: ", "),
            MPMediaItemPropertyAlbumTitle: "\(perf.title) · \(Fmt.shortDate(perf.date))",
            MPMediaItemPropertyComposer: perf.composers.map(\.name).joined(separator: ", "),
            MPMediaItemPropertyPlaybackDuration: currentDuration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: currentTime,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyPlaybackQueueIndex: currentIndex,
            MPNowPlayingInfoPropertyPlaybackQueueCount: tracks.count,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
        ]
        if mpArtwork == nil, let img = artwork ?? ArtworkRenderer.placeholder(title: perf.title, seed: perf.collection ?? perf.id) {
            mpArtwork = Self.makeArtwork(img)
        }
        if let mpArtwork { info[MPMediaItemPropertyArtwork] = mpArtwork }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        MPNowPlayingInfoCenter.default().playbackState = isPlaying ? .playing : .paused
    }

    /// Built outside the main actor: MediaPlayer calls the request handler on a background queue.
    nonisolated private static func makeArtwork(_ img: UIImage) -> MPMediaItemArtwork {
        MPMediaItemArtwork(boundsSize: img.size) { @Sendable _ in img }
    }

    private func loadArtwork(_ perf: PerformanceDetail, api: any SethsterAPI) {
        guard let url = api.resolve(perf.artworkUrl) else { return }
        let headers = api.authHeaders(for: url)
        Task {
            if let img = await ImageLoader.shared.image(for: url, headers: headers) {
                if self.performance?.id == perf.id { self.artwork = img; self.mpArtwork = nil; self.updateNowPlayingInfo(force: true) }
            }
        }
    }
}

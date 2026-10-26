import AVFoundation
import MediaPlayer
import Observation
import UIKit

/// Plays Radio: a seeded station (server-picked excerpts = track + start/end, logged as plays/skips so the
/// shuffle learns) or a live internet stream (with ICY / server-side "now playing"). Separate from
/// PlayerController (which plays one whole performance); starting either one stops the other.
@Observable
@MainActor
final class RadioPlayer {
    // seeded station
    private(set) var stationId: String?
    private(set) var stationName: String?
    private(set) var current: RadioExcerpt?
    private(set) var upcoming: [RadioExcerpt] = []
    private(set) var played: [RadioExcerpt] = []
    // live
    private(set) var live: LiveStation?
    private(set) var liveNow: LiveNowPlaying?
    // shared state
    private(set) var isLoading = false
    private(set) var isPlaying = false
    private(set) var isBuffering = false
    private(set) var elapsed: Double = 0
    private(set) var duration: Double = 0
    private(set) var errorMessage: String?
    private(set) var artwork: UIImage?
    var showNowPlaying = false

    var isActive: Bool { stationId != nil || live != nil }
    var isLive: Bool { live != nil }
    var title: String { live?.name ?? current?.title ?? stationName ?? "Radio" }
    var subtitle: String {
        if let live { return liveNow?.lines.first ?? live.broadcaster ?? "Live" }
        return current.map { [$0.composer?.name, $0.performerLine].compactMap { $0?.isEmpty == false ? $0 : nil }
            .joined(separator: " · ") } ?? (isLoading ? "Tuning in…" : "")
    }
    var artworkPath: String? { live?.logoUrl ?? current?.performance?.artworkUrl }

    @ObservationIgnored private let player = AVPlayer()
    @ObservationIgnored private var api: (any SethsterAPI)?
    @ObservationIgnored private var client: RadioClient?
    @ObservationIgnored private var timeObserver: Any?
    @ObservationIgnored private var endObserver: NSObjectProtocol?
    @ObservationIgnored private var pendingStart: Double?
    @ObservationIgnored private var playId: Int?
    @ObservationIgnored private var playToken = 0
    @ObservationIgnored private var failures = 0
    @ObservationIgnored private var commandsConfigured = false
    @ObservationIgnored private var nowPlayingTask: Task<Void, Never>?
    @ObservationIgnored private var metaDelegate: RadioMetadataDelegate?
    @ObservationIgnored private var mpArtwork: MPMediaItemArtwork?
    @ObservationIgnored private var lastInfoKey = ""

    init() {
        player.automaticallyWaitsToMinimizeStalling = true
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
                                                      queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    // MARK: - Seeded stations

    /// Starts (or resumes) the station `id` ("person:maria-callas", "composer:giuseppe-verdi", "mix:…").
    func start(station id: String, name: String? = nil, api: any SethsterAPI) {
        guard let client = RadioClient(api: api) else {
            errorMessage = "Collection radio needs the Sethster server."
            return
        }
        AppServices.player.stop()
        resetPlayback()
        self.api = api
        self.client = client
        stationId = id
        stationName = name
        live = nil; liveNow = nil
        errorMessage = nil
        isLoading = true
        configureSessionAndCommands()
        let token = playToken
        Task {
            do {
                let r = try await client.next(station: id, count: 4, exclude: [])
                guard token == playToken else { return }
                stationName = r.station.name
                upcoming = r.items
                isLoading = false
                advance()
            } catch {
                guard token == playToken else { return }
                isLoading = false
                errorMessage = Self.describe(error)
            }
        }
    }

    func start(seed: RadioSeed, api: any SethsterAPI) {
        start(station: seed.stationId, name: "\(seed.name) Radio", api: api)
    }

    /// Skip the current excerpt (logged as a skip unless nearly finished).
    func skip() {
        guard stationId != nil else { return }
        finishCurrent(skipped: elapsed < max(duration * 0.85, duration - 15))
        advance()
    }

    /// Play a specific excerpt next (e.g. the same aria from another performance).
    func playNext(excerptId: String) {
        guard let client else { return }
        Task {
            guard let ex = try? await client.excerpt(id: excerptId) else { return }
            upcoming.insert(ex, at: 0)
            skip()
        }
    }

    private func advance() {
        guard stationId != nil else { return }
        if let c = current { played.insert(c, at: 0); if played.count > 50 { played.removeLast() } }
        guard !upcoming.isEmpty else {
            current = nil
            refill(thenPlay: true)
            return
        }
        let ex = upcoming.removeFirst()
        play(ex)
        refill(thenPlay: false)
    }

    private func refill(thenPlay: Bool) {
        guard let client, let sid = stationId, upcoming.count < 3 else { return }
        let exclude = ([current?.id] + upcoming.map(\.id) + played.prefix(5).map(\.id)).compactMap { $0 }
        let token = playToken
        Task {
            guard let r = try? await client.next(station: sid, count: 3 - upcoming.count, exclude: exclude),
                  token == playToken else { return }
            let have = Set(upcoming.map(\.id) + [current?.id].compactMap { $0 })
            upcoming += r.items.filter { !have.contains($0.id) }
            if thenPlay, current == nil { advance() }
        }
    }

    private func play(_ ex: RadioExcerpt) {
        current = ex
        errorMessage = nil
        elapsed = 0
        duration = ex.endS.map { $0 - ex.startS } ?? ex.durationS ?? 0
        artwork = nil; mpArtwork = nil
        guard let api, let url = api.mediaURL(ex.streamUrl) else { errorMessage = "Can't reach this recording."; return }
        let headers = api.authHeaders(for: url)
        let options: [String: Any] = headers.isEmpty ? [:] : ["AVURLAssetHTTPHeaderFieldsKey": headers]
        let item = AVPlayerItem(asset: AVURLAsset(url: url, options: options))
        item.preferredForwardBufferDuration = 20
        if let end = ex.endS { item.forwardPlaybackEndTime = CMTime(seconds: end, preferredTimescale: 600) }
        observeEnd(of: item)
        pendingStart = ex.startS
        player.replaceCurrentItem(with: item)
        loadArtwork(ex.performance?.artworkUrl)
        let token = playToken
        Task {
            let id = try? await client?.startPlay(excerpt: ex.id, station: stationId)
            if token == playToken, current?.id == ex.id { playId = id ?? nil }
        }
        tick()
    }

    private func finishCurrent(skipped: Bool) {
        guard let id = playId, let client else { return }
        playId = nil
        let listened = elapsed
        Task { try? await client.finishPlay(id, skipped: skipped, listened: listened) }
    }

    private func observeEnd(of item: AVPlayerItem) {
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification,
                                                             object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.stationId != nil else { return }
                self.failures = 0
                self.finishCurrent(skipped: false)
                self.advance()
            }
        }
    }

    // MARK: - Live

    func start(live station: LiveStation, api: any SethsterAPI) {
        guard let url = URL(string: station.streamUrl) else { errorMessage = "Bad stream URL."; return }
        AppServices.player.stop()
        resetPlayback()
        self.api = api
        client = RadioClient(api: api)
        live = station
        liveNow = nil
        errorMessage = nil
        configureSessionAndCommands()
        let item = AVPlayerItem(url: url)
        item.preferredForwardBufferDuration = 10
        let output = AVPlayerItemMetadataOutput(identifiers: nil)
        let delegate = RadioMetadataDelegate { [weak self] title in
            MainActor.assumeIsolated {
                guard let self, self.live?.id == station.id else { return }
                var now = self.liveNow ?? LiveNowPlaying()
                now.raw = title
                let parts = title.components(separatedBy: " - ")
                if parts.count >= 2 { now.artist = parts[0]; now.title = parts.dropFirst().joined(separator: " - ") }
                else { now.title = title }
                self.liveNow = now
                self.updateNowPlayingInfo(force: true)
            }
        }
        output.setDelegate(delegate, queue: .main)
        item.add(output)
        metaDelegate = delegate
        player.replaceCurrentItem(with: item)
        player.play()
        loadArtwork(station.logoUrl)
        pollNowPlaying(station)
        tick()
    }

    private func pollNowPlaying(_ station: LiveStation) {
        nowPlayingTask?.cancel()
        guard let client, station.hasNowPlaying == true else { return }
        nowPlayingTask = Task { [weak self] in
            while !Task.isCancelled {
                if let np = try? await client.nowPlaying(live: station.id), !np.isEmpty {
                    guard let self, self.live?.id == station.id else { return }
                    self.liveNow = np
                    self.updateNowPlayingInfo(force: true)
                }
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    // MARK: - Controls

    func togglePlayPause() { isPlaying ? pause() : resume() }

    func pause() { player.pause(); tick() }

    func resume() {
        guard isActive else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        if let live, player.currentItem == nil || player.currentItem?.status == .failed {
            start(live: live, api: api ?? AppServices.model.api); return
        }
        if live != nil {
            // a live stream resumed after a pause should be live again, not where it stopped
            player.currentItem?.seek(to: .positiveInfinity, completionHandler: nil)
        }
        player.play(); tick()
    }

    func seek(to seconds: Double) {
        guard let ex = current else { return }
        let t = ex.startS + min(max(0, seconds), max(duration - 1, 0))
        player.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func stop() {
        guard isActive || player.currentItem != nil else { return }
        if stationId != nil { finishCurrent(skipped: true) }
        resetPlayback()
        stationId = nil; stationName = nil; live = nil; liveNow = nil
        errorMessage = nil; showNowPlaying = false
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    private func resetPlayback() {
        playToken += 1
        nowPlayingTask?.cancel(); nowPlayingTask = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
        metaDelegate = nil
        current = nil; upcoming = []; played = []; playId = nil; pendingStart = nil
        isLoading = false; isPlaying = false; elapsed = 0; duration = 0; failures = 0
        artwork = nil; mpArtwork = nil; lastInfoKey = ""
    }

    // MARK: - Tick

    private func tick() {
        guard let item = player.currentItem else {
            isPlaying = false; isBuffering = isLoading
            return
        }
        if item.status == .failed {
            errorMessage = item.error?.localizedDescription ?? "Could not play this stream."
            // Station excerpts come from our server; AVPlayer hides a 401/403, so ask the account path.
            if stationId != nil { AppServices.account.verifySession() }
            if stationId != nil, failures < 3 {
                failures += 1
                finishCurrent(skipped: true)
                advance()
                return
            }
        }
        if let start = pendingStart, item.status == .readyToPlay {
            pendingStart = nil
            if start > 0.5 {
                player.seek(to: CMTime(seconds: start, preferredTimescale: 600), toleranceBefore: .zero,
                            toleranceAfter: .zero) { @Sendable [weak self] _ in
                    Task { @MainActor in self?.player.play() }
                }
            } else {
                player.play()
            }
        }
        if let ex = current {
            let t = player.currentTime().seconds
            elapsed = t.isFinite ? max(0, t - ex.startS) : 0
            if ex.endS == nil {
                let d = item.duration.seconds
                if d.isFinite, d > 0 { duration = d - ex.startS }
            }
        }
        isPlaying = player.timeControlStatus != .paused || pendingStart != nil
        isBuffering = player.timeControlStatus == .waitingToPlayAtSpecifiedRate || pendingStart != nil
        updateNowPlayingInfo()
    }

    // MARK: - System integration

    private func configureSessionAndCommands() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, policy: .longFormAudio)
        try? session.setActive(true)
        guard !commandsConfigured else { return }
        commandsConfigured = true
        // PlayerController registers its own targets; each player ignores commands while it is idle.
        let cc = MPRemoteCommandCenter.shared()
        cc.playCommand.addTarget { @Sendable [weak self] _ in
            Task { @MainActor in if self?.isActive == true { self?.resume() } }; return .success
        }
        cc.pauseCommand.addTarget { @Sendable [weak self] _ in
            Task { @MainActor in if self?.isActive == true { self?.pause() } }; return .success
        }
        cc.togglePlayPauseCommand.addTarget { @Sendable [weak self] _ in
            Task { @MainActor in if self?.isActive == true { self?.togglePlayPause() } }; return .success
        }
        cc.nextTrackCommand.addTarget { @Sendable [weak self] _ in
            Task { @MainActor in if self?.stationId != nil { self?.skip() } }; return .success
        }
    }

    private func updateNowPlayingInfo(force: Bool = false) {
        guard isActive else { return }
        let key = "\(current?.id ?? live?.id ?? "")|\(isPlaying)|\(Int(duration))|\(liveNow?.raw ?? liveNow?.title ?? "")"
        guard force || key != lastInfoKey else { return }
        lastInfoKey = key
        var info: [String: Any] = [
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
        ]
        if let live {
            info[MPMediaItemPropertyTitle] = liveNow?.lines.first ?? live.name
            info[MPMediaItemPropertyArtist] = liveNow?.lines.dropFirst().first ?? live.broadcaster ?? ""
            info[MPMediaItemPropertyAlbumTitle] = live.name
            info[MPNowPlayingInfoPropertyIsLiveStream] = true
        } else if let ex = current {
            info[MPMediaItemPropertyTitle] = ex.title
            info[MPMediaItemPropertyArtist] = ex.performerLine
            info[MPMediaItemPropertyAlbumTitle] = [stationName, ex.performance.map { "\($0.title) · \(Fmt.shortDate($0.date))" }]
                .compactMap { $0 }.joined(separator: " — ")
            info[MPMediaItemPropertyComposer] = ex.composer?.name ?? ""
            info[MPMediaItemPropertyPlaybackDuration] = duration
            info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = elapsed
        }
        if mpArtwork == nil, let img = artwork ?? ArtworkRenderer.placeholder(title: title, seed: stationId ?? live?.id ?? "radio") {
            mpArtwork = Self.makeArtwork(img)
        }
        if let mpArtwork { info[MPMediaItemPropertyArtwork] = mpArtwork }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        MPNowPlayingInfoCenter.default().playbackState = isPlaying ? .playing : .paused
    }

    nonisolated private static func makeArtwork(_ img: UIImage) -> MPMediaItemArtwork {
        MPMediaItemArtwork(boundsSize: img.size) { @Sendable _ in img }
    }

    private func loadArtwork(_ path: String?) {
        guard let api, let url = api.resolve(path) else { return }
        let headers = path?.hasPrefix("http") == true ? [:] : api.authHeaders
        let token = playToken
        let key = current?.id ?? live?.id
        Task {
            if let img = await ImageLoader.shared.image(for: url, headers: headers),
               token == playToken, key == (current?.id ?? live?.id) {
                artwork = img; mpArtwork = nil; updateNowPlayingInfo(force: true)
            }
        }
    }

    static func describe(_ error: Error) -> String {
        if case APIError.http(404, _) = error { return "Nothing to play on this station yet." }
        return error.localizedDescription
    }
}

/// ICY / timed metadata from a live stream ("StreamTitle").
final class RadioMetadataDelegate: NSObject, AVPlayerItemMetadataOutputPushDelegate, @unchecked Sendable {
    private let onTitle: @Sendable (String) -> Void
    init(onTitle: @escaping @Sendable (String) -> Void) { self.onTitle = onTitle }

    func metadataOutput(_ output: AVPlayerItemMetadataOutput, didOutputTimedMetadataGroups groups: [AVTimedMetadataGroup],
                        from track: AVPlayerItemTrack?) {
        for item in groups.flatMap(\.items) {
            let isTitle = item.identifier == .icyMetadataStreamTitle || item.commonKey == .commonKeyTitle
            if isTitle, let s = item.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
                onTitle(s)
                return
            }
        }
    }
}

import Foundation
import Observation

/// Shared phone/CarPlay state for the listener's ordered, offline-ready Rotation. Everything offline (metadata,
/// files, background-download descriptors) is namespaced by account, `<server identity>/<user id>`; with no
/// approved account the store is empty.
@Observable
@MainActor
final class RotationStore {
    static let listener = "default"

    private(set) var items: [RotationItem] = []
    private(set) var isLoading = false
    private(set) var errorMessage: String?
    private(set) var trackProgress: [String: Double] = [:]
    private(set) var failedTracks: Set<String> = []
    /// The approved account whose Rotation this is (AccountStore.rotationNamespace).
    private(set) var namespace: String?

    var allowsCellularDownloads: Bool {
        didSet {
            UserDefaults.standard.set(allowsCellularDownloads, forKey: "rotationAllowsCellular")
            guard oldValue != allowsCellularDownloads else { return }
            downloader.restartForCellularPolicyChange()
        }
    }

    @ObservationIgnored private var details: [String: PerformanceDetail] = [:]
    @ObservationIgnored private weak var model: AppModel?
    @ObservationIgnored private let files = FileManager.default
    @ObservationIgnored private let downloader = RotationDownloadCoordinator.shared
    @ObservationIgnored private var pendingOrder: [String]?
    @ObservationIgnored private var reorderTask: Task<Void, Never>?
    @ObservationIgnored private var reorderConfirmedItems: [RotationItem]?
    @ObservationIgnored private var mutationRevision = 0
    @ObservationIgnored private var mutationsInFlight = Set<String>()
    @ObservationIgnored private var refreshesInFlight = 0

    init(model: AppModel) {
        self.model = model
        allowsCellularDownloads = UserDefaults.standard.bool(forKey: "rotationAllowsCellular")
        downloader.owner = self
    }

    /// Switch to another account's offline store (nil: signed out / not approved). Cancels the previous
    /// account's downloads; its files stay on disk for the next sign-in unless the account is deleted.
    func setAccount(_ next: String?) {
        guard next != namespace else { return }
        if let old = namespace { downloader.cancelAll(account: old) }
        reorderTask?.cancel()
        reorderTask = nil
        pendingOrder = nil
        reorderConfirmedItems = nil
        mutationRevision += 1
        items = []
        details = [:]
        trackProgress = [:]
        failedTracks = []
        errorMessage = nil
        namespace = next
        downloader.setAccount(next)
        guard let next else { return }
        loadCache()
        downloader.restoreProgress(validItems: items, account: next)
    }

    /// Delete Account: remove that account's Rotation metadata, files and download tasks.
    static func purge(namespace: String) {
        RotationDownloadCoordinator.shared.cancelAll(account: namespace)
        if AppServices.rotation.namespace == namespace { AppServices.rotation.setAccount(nil) }
        try? FileManager.default.removeItem(at: accountDirectory(namespace))
    }

    var totalStorageBytes: Int64 {
        items.reduce(into: 0) { total, item in
            for track in item.download.tracks {
                guard let url = localURL(trackID: track.id),
                      let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize else { continue }
                total += Int64(size)
            }
        }
    }

    func contains(_ performanceID: String) -> Bool { items.contains { $0.id == performanceID } }

    func item(_ performanceID: String) -> RotationItem? { items.first { $0.id == performanceID } }

    func cachedPerformance(_ performanceID: String) -> PerformanceDetail? { details[performanceID] }

    func localURL(trackID: String) -> URL? {
        guard let namespace, let item = items.first(where: { item in item.download.tracks.contains { $0.id == trackID } }),
              let track = item.download.tracks.first(where: { $0.id == trackID }) else { return nil }
        let url = Self.localURL(for: track, performanceID: item.id, account: namespace)
        guard Self.isCompleteFile(at: url, expectedBytes: track.bytes) else {
            // A terminated copy, an HTTP error body, or an old file whose manifest size changed must
            // never make the item look downloaded or reach AVPlayer.
            try? files.removeItem(at: url)
            return nil
        }
        return url
    }

    func downloadState(for performanceID: String) -> RotationDownloadState {
        guard let item = item(performanceID), !item.download.tracks.isEmpty else { return .waiting }
        if item.download.tracks.allSatisfy({ localURL(trackID: $0.id) != nil }) { return .downloaded }
        if item.download.tracks.contains(where: { failedTracks.contains($0.id) }) { return .failed }
        let active = item.download.tracks.compactMap { trackProgress[$0.id] }
        if !active.isEmpty { return .downloading(active.reduce(0, +) / Double(item.download.tracks.count)) }
        return .waiting
    }

    func refresh() async {
        guard let model, let ns = namespace else { return }
        if model.isOfflineFallback {
            // Explicit demo mode is intentionally transient. Restore the last live snapshot when the user
            // leaves it without a connection rather than presenting demo Rotation as the live/offline queue.
            loadCache()
            retryMissingDownloads()
            return
        }
        let generation = model.generation
        let revision = mutationRevision
        let api = model.api
        refreshesInFlight += 1
        isLoading = true
        defer {
            refreshesInFlight -= 1
            isLoading = refreshesInFlight > 0
        }
        do {
            let response = try await api.rotation(listener: Self.listener)
            // A server/demo switch while the request was in flight owns the next refresh.
            guard generation == model.generation, ns == namespace, revision == mutationRevision,
                  mutationsInFlight.isEmpty, reorderTask == nil else { return }
            if !api.isDemo {
                let removed = Set(items.map(\.id)).subtracting(response.items.map(\.id))
                for id in removed {
                    downloader.cancel(performanceID: id, account: ns)
                    details[id] = nil
                    deleteFiles(performanceID: id)
                }
            }
            items = response.items
            errorMessage = nil
            if !api.isDemo { Self.adoptLegacyFiles(for: response.items, into: ns) }
            saveCache()
            await cacheDetailsAndDownload(using: api, generation: generation, account: ns)
        } catch {
            guard ns == namespace else { return }
            // Cached Rotation stays useful with no reception.
            errorMessage = items.isEmpty ? error.localizedDescription : nil
            retryMissingDownloads()
        }
    }

    func toggle(_ performance: PerformanceDetail) async {
        if contains(performance.id) { await remove(performance.id) } else { await add(performance) }
    }

    func add(_ performance: PerformanceDetail) async {
        guard let model, let ns = namespace else { return }
        guard !model.isOfflineFallback else {
            errorMessage = "Rotation changes require a connection to the Sethster server."
            return
        }
        guard mutationsInFlight.insert(performance.id).inserted else { return }
        mutationRevision += 1
        defer { mutationsInFlight.remove(performance.id) }
        if let reorderTask { await reorderTask.value }
        let generation = model.generation
        let api = model.api
        do {
            let added = try await api.addToRotation(performanceId: performance.id, listener: Self.listener)
            guard generation == model.generation, ns == namespace else { await refresh(); return }
            if !contains(added.id) { items.append(added) }
            details[performance.id] = performance
            errorMessage = nil
            saveCache()
            startDownloads(for: added, api: api, account: ns)
        } catch {
            guard generation == model.generation, ns == namespace else { await refresh(); return }
            errorMessage = error.localizedDescription
        }
    }

    func remove(_ performanceID: String) async {
        guard let model, let ns = namespace else { return }
        guard !model.isOfflineFallback else {
            errorMessage = "Rotation changes require a connection to the Sethster server."
            return
        }
        guard mutationsInFlight.insert(performanceID).inserted else { return }
        mutationRevision += 1
        defer { mutationsInFlight.remove(performanceID) }
        if let reorderTask { await reorderTask.value }
        guard let oldIndex = items.firstIndex(where: { $0.id == performanceID }) else { return }
        let removedItem = items.remove(at: oldIndex)
        let generation = model.generation
        let api = model.api
        do {
            try await api.removeFromRotation(performanceId: performanceID, listener: Self.listener)
            guard generation == model.generation, ns == namespace else { await refresh(); return }
            details[performanceID] = nil
            // Demo Rotation is independent of the listener's live offline library.
            if !api.isDemo {
                downloader.cancel(performanceID: performanceID, account: ns)
                deleteFiles(performanceID: performanceID)
            }
            errorMessage = nil
            saveCache()
        } catch {
            guard generation == model.generation, ns == namespace else { await refresh(); return }
            if !contains(performanceID) { items.insert(removedItem, at: min(oldIndex, items.count)) }
            errorMessage = error.localizedDescription
        }
    }

    func move(from offsets: IndexSet, to destination: Int) {
        guard namespace != nil, mutationsInFlight.isEmpty, model?.isOfflineFallback != true else {
            if model?.isOfflineFallback == true {
                errorMessage = "Rotation changes require a connection to the Sethster server."
            }
            return
        }
        mutationRevision += 1
        if reorderTask == nil { reorderConfirmedItems = items }
        items.move(fromOffsets: offsets, toOffset: destination)
        saveCache()
        pendingOrder = items.map(\.id)
        guard reorderTask == nil else { return }
        reorderTask = Task { await flushPendingOrder() }
    }

    /// Serialize complete-list PUTs. Sending two drag results concurrently can let the older response win on
    /// either the server or one UI, leaving phone and CarPlay with a different order.
    private func flushPendingOrder() async {
        while let desired = pendingOrder {
            pendingOrder = nil
            guard let model, let ns = namespace else { break }
            let generation = model.generation
            let api = model.api
            do {
                let response = try await api.reorderRotation(performanceIds: desired, listener: Self.listener)
                guard generation == model.generation, ns == namespace else { continue }
                reorderConfirmedItems = response.items
                // Keep a newer local drag visible; it will be sent on the next loop iteration.
                if pendingOrder == nil {
                    items = response.items
                    errorMessage = nil
                    saveCache()
                }
            } catch {
                guard generation == model.generation, ns == namespace else { continue }
                if pendingOrder == nil {
                    if let confirmed = reorderConfirmedItems { items = confirmed; saveCache() }
                    errorMessage = error.localizedDescription
                }
            }
        }
        guard !Task.isCancelled else { return }
        reorderTask = nil
        if pendingOrder != nil {
            reorderTask = Task { await flushPendingOrder() }
        } else {
            reorderConfirmedItems = nil
        }
    }

    func retry(_ performanceID: String) {
        guard let item = item(performanceID), let api = model?.api, let namespace else { return }
        failedTracks.subtract(item.download.tracks.map(\.id))
        startDownloads(for: item, api: api, account: namespace)
    }

    func clearError() { errorMessage = nil }

    /// `account` is the namespace `api` belongs to; after every await (success or failure) the store must still be
    /// on it, or A's credentials would start downloads in B's namespace.
    private func cacheDetailsAndDownload(using api: any SethsterAPI, generation: Int, account: String) async {
        for item in items {
            if details[item.id] == nil {
                let detail = try? await api.performance(id: item.id)
                guard model?.generation == generation, namespace == account,
                      items.contains(where: { $0.id == item.id }) else { return }
                if let detail {
                    details[item.id] = detail
                    saveCache()
                }
            }
            startDownloads(for: item, api: api, account: account)
        }
    }

    func retryMissingDownloads() {
        guard let api = model?.api, !api.isDemo, let namespace else { return }
        for item in items { startDownloads(for: item, api: api, account: namespace) }
    }

    private func startDownloads(for item: RotationItem, api: any SethsterAPI, account: String) {
        guard !api.isDemo, account == namespace else { return }
        for track in item.download.tracks where localURL(trackID: track.id) == nil {
            downloader.download(track, performanceID: item.id, account: account, api: api,
                                allowsCellular: allowsCellularDownloads)
        }
    }

    // Coordinator callbacks hop to the main actor; one from another account (a switch in between) is dropped.

    func downloadProgress(trackID: String, progress: Double, account: String) {
        guard account == namespace else { return }
        failedTracks.remove(trackID)
        trackProgress[trackID] = min(max(progress, 0), 1)
    }

    func downloadFinished(trackID: String, account: String) {
        guard account == namespace else { return }
        trackProgress[trackID] = nil
        failedTracks.remove(trackID)
        saveCache()
    }

    func downloadFailed(trackID: String, account: String) {
        guard account == namespace else { return }
        trackProgress[trackID] = nil
        failedTracks.insert(trackID)
    }

    private struct Cache: Codable {
        let items: [RotationItem]
        let details: [String: PerformanceDetail]
    }

    private func loadCache() {
        guard let namespace, let data = try? Data(contentsOf: Self.cacheURL(namespace)),
              let cache = try? LiveAPIClient.decoder.decode(Cache.self, from: data) else { return }
        items = cache.items
        details = cache.details
    }

    private func saveCache() {
        // Demo state belongs to MockRotationState and must not replace the live snapshot used offline.
        guard model?.api.isDemo == false, let namespace else { return }
        do {
            try Self.prepareDirectory(namespace)
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            try encoder.encode(Cache(items: items, details: details)).write(to: Self.cacheURL(namespace), options: .atomic)
        } catch { print("Could not save Rotation cache: \(error)") }
    }

    private func deleteFiles(performanceID: String) {
        guard let namespace else { return }
        let url = Self.mediaDirectory(namespace).appending(path: Self.fileComponent(performanceID), directoryHint: .isDirectory)
        try? files.removeItem(at: url)
    }

    nonisolated static let rootDirectory: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appending(path: "Rotation", directoryHint: .isDirectory)
    }()
    /// Rotation/Accounts/<namespace>/ holds that account's rotation.json and Media/.
    nonisolated static func accountDirectory(_ namespace: String) -> URL {
        rootDirectory.appending(path: "Accounts", directoryHint: .isDirectory)
            .appending(path: fileComponent(namespace), directoryHint: .isDirectory)
    }
    nonisolated static func mediaDirectory(_ namespace: String) -> URL {
        accountDirectory(namespace).appending(path: "Media", directoryHint: .isDirectory)
    }
    nonisolated static func cacheURL(_ namespace: String) -> URL { accountDirectory(namespace).appending(path: "rotation.json") }

    /// Before accounts, one un-namespaced store (listener `default`, Dad's) lived at Rotation/rotation.json +
    /// Rotation/Media. It is never shown as anyone's Rotation: the first successful live refresh of an account
    /// takes only the files of performances in that account's own Rotation (so they aren't fetched again), and
    /// the rest of the legacy store is deleted. An empty Rotation adopts nothing and keeps the legacy store: Dad
    /// may sign in before `move-rotation` has given his account the old Rotation. A failed move keeps it too.
    private static func adoptLegacyFiles(for items: [RotationItem], into namespace: String) {
        let fm = FileManager.default
        let legacyCache = rootDirectory.appending(path: "rotation.json")
        let legacyMedia = rootDirectory.appending(path: "Media", directoryHint: .isDirectory)
        guard !items.isEmpty, fm.fileExists(atPath: legacyCache.path) || fm.fileExists(atPath: legacyMedia.path),
              (try? prepareDirectory(namespace)) != nil else { return }
        var failed = false
        for item in items {
            let name = fileComponent(item.id)
            let src = legacyMedia.appending(path: name, directoryHint: .isDirectory)
            let dst = mediaDirectory(namespace).appending(path: name, directoryHint: .isDirectory)
            guard fm.fileExists(atPath: src.path), !fm.fileExists(atPath: dst.path) else { continue }
            if (try? fm.moveItem(at: src, to: dst)) == nil { failed = true }
        }
        guard !failed else { return }
        try? fm.removeItem(at: legacyCache)
        try? fm.removeItem(at: legacyMedia)
    }

    nonisolated static func localURL(for track: RotationTrackDownload, performanceID: String? = nil,
                                     account: String) -> URL {
        let performance = performanceID ?? String(track.id.split(separator: ":").first ?? "unknown")
        let ext = (track.format ?? "audio").lowercased().filter { $0.isLetter || $0.isNumber }
        return mediaDirectory(account)
            .appending(path: fileComponent(performance), directoryHint: .isDirectory)
            .appending(path: fileComponent(track.id) + "." + (ext.isEmpty ? "audio" : ext))
    }

    nonisolated static func prepareDirectory(_ namespace: String) throws {
        try FileManager.default.createDirectory(at: mediaDirectory(namespace), withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var root = rootDirectory
        try root.setResourceValues(values)
    }

    nonisolated static func isCompleteFile(at url: URL, expectedBytes: Int64?) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
              values.isRegularFile == true, let size = values.fileSize, size > 0 else { return false }
        return expectedBytes.map { Int64(size) == $0 } ?? true
    }

    nonisolated static func fileComponent(_ value: String) -> String {
        Data(value.utf8).base64EncodedString()
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "=", with: "")
    }
}

/// Owns the background URLSession. Task descriptions restore account + track identity after relaunch; tasks
/// are keyed by (account, track) and only the current account's completions are published.
final class RotationDownloadCoordinator: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    static let sessionIdentifier = "com.goatsandmonkeys.sethster.rotation"
    static let shared = RotationDownloadCoordinator()
    weak var owner: RotationStore?

    private let lock = NSLock()
    private var backgroundCompletionHandler: (() -> Void)?
    /// (account, track) key → the one task whose callbacks are allowed to publish/move a file. Tracking the task
    /// id prevents a cancelled attempt from racing a replacement attempt after a policy change or removal.
    private var activeTasks: [String: Int] = [:]
    private var pendingTracks = Set<String>()
    private var policyGeneration = 0
    /// (account, performance) keys.
    private var cancelledPerformances = Set<String>()
    private var currentAccount: String?
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.waitsForConnectivity = true
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    private override init() { super.init() }

    private static func key(_ account: String?, _ id: String) -> String { (account ?? "") + "\u{1F}" + id }

    func setAccount(_ account: String?) { lock.withLock { currentAccount = account } }

    /// Cancel every task belonging to `account` (sign-out, revocation, deletion).
    func cancelAll(account: String) {
        session.getAllTasks { [weak self] tasks in
            guard let self else { return }
            for task in tasks {
                guard let d = Self.descriptor(task), d.account == account else { continue }
                self.removeActiveTask(Self.key(account, d.trackID), taskID: task.taskIdentifier)
                task.cancel()
            }
        }
    }

    func handleBackgroundEvents(completionHandler: @escaping () -> Void) {
        lock.withLock { backgroundCompletionHandler = completionHandler }
    }

    func restoreProgress(validItems: [RotationItem], account: String) {
        let validTracks = Dictionary(uniqueKeysWithValues: validItems.flatMap { item in
            item.download.tracks.map { ($0.id, (item.id, $0.format, $0.bytes)) }
        })
        session.getAllTasks { [weak self] tasks in
            guard let self else { return }
            var kept: [URLSessionTask] = []
            for task in tasks {
                // Tasks from before accounts (no account) or for another account aren't this store's.
                guard let descriptor = Self.descriptor(task), descriptor.account == account,
                      let valid = validTracks[descriptor.trackID],
                      valid.0 == descriptor.performanceID, valid.1 == descriptor.format,
                      valid.2 == descriptor.expectedBytes,
                      task.state != .canceling, task.state != .completed else {
                    task.cancel()
                    continue
                }
                let trackKey = Self.key(account, descriptor.trackID)
                let keep = self.lock.withLock {
                    if let taskID = self.activeTasks[trackKey] { return taskID == task.taskIdentifier }
                    self.activeTasks[trackKey] = task.taskIdentifier
                    return true
                }
                if keep { kept.append(task) } else { task.cancel() }
            }
            for task in kept {
                guard let d = Self.descriptor(task) else { continue }
                let expected = task.countOfBytesExpectedToReceive
                let progress = expected > 0 ? Double(task.countOfBytesReceived) / Double(expected) : 0
                Task { @MainActor [weak self] in
                    self?.owner?.downloadProgress(trackID: d.trackID, progress: progress, account: account)
                }
            }
        }
    }

    func download(_ track: RotationTrackDownload, performanceID: String, account: String, api: any SethsterAPI,
                  allowsCellular: Bool) {
        guard let url = api.resolve(track.url) else { return }
        let trackKey = Self.key(account, track.id)
        let performanceKey = Self.key(account, performanceID)
        let generation = lock.withLock { () -> Int? in
            _ = cancelledPerformances.remove(performanceKey)
            guard currentAccount == account, activeTasks[trackKey] == nil,
                  pendingTracks.insert(trackKey).inserted else { return nil }
            return policyGeneration
        }
        guard let generation else { return }

        // `restoreProgress()` and a launch refresh can overlap. Consult the background session before making
        // a task so an already-restored transfer remains the single authoritative attempt for this track.
        session.getAllTasks { [weak self] tasks in
            guard let self else { return }
            if let existing = tasks.first(where: {
                let d = Self.descriptor($0)
                return d?.trackID == track.id && d?.account == account
                    && $0.state != .canceling && $0.state != .completed
            }) {
                self.lock.withLock {
                    self.pendingTracks.remove(trackKey)
                    if self.activeTasks[trackKey] == nil { self.activeTasks[trackKey] = existing.taskIdentifier }
                }
                return
            }
            let shouldCreate = self.lock.withLock {
                self.pendingTracks.remove(trackKey)
                return self.policyGeneration == generation
                    && self.currentAccount == account
                    && self.activeTasks[trackKey] == nil
                    && !self.cancelledPerformances.contains(performanceKey)
            }
            guard shouldCreate else { return }
            var request = URLRequest(url: url)
            request.allowsCellularAccess = allowsCellular
            api.authHeaders(for: url).forEach { request.setValue($1, forHTTPHeaderField: $0) }
            let task = self.session.downloadTask(with: request)
            task.taskDescription = Self.encode(Descriptor(performanceID: performanceID, trackID: track.id,
                                                          format: track.format, expectedBytes: track.bytes,
                                                          account: account))
            self.lock.withLock { self.activeTasks[trackKey] = task.taskIdentifier }
            task.resume()
            Task { @MainActor [weak self] in
                self?.owner?.downloadProgress(trackID: track.id, progress: 0, account: account)
            }
        }
    }

    func cancel(performanceID: String, account: String) {
        lock.withLock { _ = cancelledPerformances.insert(Self.key(account, performanceID)) }
        session.getAllTasks { tasks in
            for task in tasks {
                guard let d = Self.descriptor(task), d.performanceID == performanceID, d.account == account else { continue }
                self.removeActiveTask(Self.key(account, d.trackID), taskID: task.taskIdentifier)
                task.cancel()
            }
        }
    }

    /// `allowsCellularAccess` is fixed when a URLSessionTask is created. Recreate active tasks so changing
    /// the Settings toggle affects downloads that were already waiting/running, not only future additions.
    func restartForCellularPolicyChange() {
        lock.withLock {
            policyGeneration += 1
            pendingTracks.removeAll()
        }
        session.getAllTasks { [weak self] tasks in
            guard let self else { return }
            for task in tasks {
                if let d = Self.descriptor(task) {
                    self.removeActiveTask(Self.key(d.account, d.trackID), taskID: task.taskIdentifier)
                    task.cancel()
                }
            }
            Task { @MainActor [weak self] in self?.owner?.retryMissingDownloads() }
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        guard let d = Self.descriptor(downloadTask) else { return }
        guard isCurrent(downloadTask, descriptor: d) else { return }
        guard let account = d.account else { return }
        let progress = totalBytesExpectedToWrite > 0 ? Double(totalBytesWritten) / Double(totalBytesExpectedToWrite) : 0
        Task { @MainActor [weak self] in self?.owner?.downloadProgress(trackID: d.trackID, progress: progress, account: account) }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {
        guard let d = Self.descriptor(downloadTask), let account = d.account else { return }
        guard isCurrent(downloadTask, descriptor: d) else { return }
        let trackKey = Self.key(account, d.trackID)
        if let http = downloadTask.response as? HTTPURLResponse, [401, 403].contains(http.statusCode),
           let body = try? Data(contentsOf: location), body.count < 4096,
           let failure = AuthFailure.from(status: http.statusCode, body: body, retryAfter: nil) {
            // Revoked / signed out mid-download: the shared auth path decides; don't retry.
            AuthFailure.report(failure, token: AuthFailure.token(of: downloadTask.originalRequest))
            removeActiveTask(trackKey, taskID: downloadTask.taskIdentifier)
            Task { @MainActor [weak self] in self?.owner?.downloadFailed(trackID: d.trackID, account: account) }
            return
        }
        guard let response = downloadTask.response as? HTTPURLResponse,
              (200..<300).contains(response.statusCode),
              RotationStore.isCompleteFile(
                at: location,
                expectedBytes: d.expectedBytes ?? (response.statusCode == 200 && response.expectedContentLength > 0
                    ? response.expectedContentLength : nil)
              ) else {
            removeActiveTask(trackKey, taskID: downloadTask.taskIdentifier)
            Task { @MainActor [weak self] in self?.owner?.downloadFailed(trackID: d.trackID, account: account) }
            return
        }
        do {
            try RotationStore.prepareDirectory(account)
            let track = RotationTrackDownload(id: d.trackID, url: "", bytes: nil, format: d.format)
            let destination = RotationStore.localURL(for: track, performanceID: d.performanceID, account: account)
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: location, to: destination)
            // Removal/policy cancellation can arrive while the file operation is in progress.
            guard isCurrent(downloadTask, descriptor: d) else {
                try? FileManager.default.removeItem(at: destination)
                return
            }
            removeActiveTask(trackKey, taskID: downloadTask.taskIdentifier)
            Task { @MainActor [weak self] in self?.owner?.downloadFinished(trackID: d.trackID, account: account) }
        } catch {
            removeActiveTask(trackKey, taskID: downloadTask.taskIdentifier)
            Task { @MainActor [weak self] in self?.owner?.downloadFailed(trackID: d.trackID, account: account) }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        guard let error, let d = Self.descriptor(task), let account = d.account else { return }
        guard isCurrent(task, descriptor: d) else { return }
        let trackKey = Self.key(account, d.trackID)
        let ns = error as NSError
        if ns.code == NSURLErrorCancelled {
            removeActiveTask(trackKey, taskID: task.taskIdentifier)
            return
        }
        if let resumeData = ns.userInfo[NSURLSessionDownloadTaskResumeData] as? Data {
            let retry = self.session.downloadTask(withResumeData: resumeData)
            retry.taskDescription = task.taskDescription
            lock.withLock { activeTasks[trackKey] = retry.taskIdentifier }
            retry.resume()
            return
        }
        removeActiveTask(trackKey, taskID: task.taskIdentifier)
        Task { @MainActor [weak self] in
            guard let owner = self?.owner, owner.namespace == account else { return }
            owner.downloadFailed(trackID: d.trackID, account: account)
            try? await Task.sleep(for: .seconds(5))
            guard owner.namespace == account else { return }
            if let item = owner.items.first(where: { $0.download.tracks.contains { $0.id == d.trackID } }) {
                owner.retry(item.id)
            }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let completion = lock.withLock {
            let value = backgroundCompletionHandler
            backgroundCompletionHandler = nil
            return value
        }
        DispatchQueue.main.async { completion?() }
    }

    private struct Descriptor: Codable {
        let performanceID: String
        let trackID: String
        let format: String?
        let expectedBytes: Int64?
        /// nil for tasks created before accounts; those are cancelled on restore.
        var account: String? = nil
    }

    private static func encode(_ value: Descriptor) -> String? {
        try? String(decoding: JSONEncoder().encode(value), as: UTF8.self)
    }

    private static func descriptor(_ task: URLSessionTask) -> Descriptor? {
        guard let text = task.taskDescription else { return nil }
        return try? JSONDecoder().decode(Descriptor.self, from: Data(text.utf8))
    }

    private func isCurrent(_ task: URLSessionTask, descriptor: Descriptor) -> Bool {
        lock.withLock {
            descriptor.account != nil && descriptor.account == currentAccount
                && !cancelledPerformances.contains(Self.key(descriptor.account, descriptor.performanceID))
                && activeTasks[Self.key(descriptor.account, descriptor.trackID)] == task.taskIdentifier
        }
    }

    private func removeActiveTask(_ trackKey: String, taskID: Int) {
        lock.withLock {
            if activeTasks[trackKey] == taskID { activeTasks[trackKey] = nil }
        }
    }
}

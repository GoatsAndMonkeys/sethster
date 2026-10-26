import Foundation
import Observation
import UIKit
import UserNotifications

/// The signed-in Sethster account (plan: iOS). The session lives in the Keychain keyed by server identity, with
/// the last `me` cached beside it so an approved listener keeps their downloaded Rotation with no reception.
/// `me` is refreshed on launch, on foreground, every 30 s while pending, and on push. Network errors never
/// sign out; only the server's `signed_out` code does.
@Observable
@MainActor
final class AccountStore {
    enum Gate: Equatable {
        /// No session: Welcome (Sign In / Create Account / Browse Demo Data).
        case welcome
        /// Signed in but not approved: the status screen.
        case status(AccountStatus)
        /// Approved: today's tabs.
        case app
    }

    private(set) var me: AccountUser?
    private(set) var token: String?
    /// The server identity the session belongs to (`AppModel.serverIdentity`).
    private(set) var identity: String
    /// Bumped on every sign-out / account switch / revocation; stale callbacks compare before publishing.
    private(set) var epoch = 0
    /// Admins: sign-ups waiting for a decision (Approvals badge, Settings badge, app-icon badge).
    private(set) var pendingApprovals = 0
    /// A notification tap asked for Approvals.
    var showApprovals = false

    var gate: Gate {
        guard token != nil, let me else { return .welcome }
        return me.status == .approved ? .app : .status(me.status)
    }
    /// Content (live or demo) is on screen: approved, or "Browse Demo Data" without an approved account.
    var showsContent: Bool { gate == .app || model.browsingDemo }
    var isAdmin: Bool { gate == .app && me?.isAdmin == true }
    /// `<server identity>/<user id>`: the Rotation offline store's namespace. nil unless approved.
    var rotationNamespace: String? { gate == .app ? me.map { "\(identity)/\($0.userId)" } : nil }

    @ObservationIgnored private let model: AppModel
    @ObservationIgnored private let vault: any SessionVault
    @ObservationIgnored private let makeAPI: @Sendable (URL, String?) -> any AccountAPI
    @ObservationIgnored private let teardownContent: @MainActor () -> Void
    @ObservationIgnored private let namespaceChanged: @MainActor (String?) -> Void
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var refreshing = false
    @ObservationIgnored private var lastVerify = Date.distantPast
    @ObservationIgnored private var deviceToken: String?
    @ObservationIgnored private var uploadedPush: String?
    /// Bumped by each sign-in / sign-up attempt; only the latest one may adopt its session.
    @ObservationIgnored private var authAttempt = 0

    nonisolated static let pushEnv: String = {
        #if DEBUG
        "sandbox"
        #else
        "production"
        #endif
    }()

    init(model: AppModel, vault: any SessionVault = KeychainSessionVault(),
         makeAPI: @escaping @Sendable (URL, String?) -> any AccountAPI = { LiveAccountAPI(root: $0, token: $1) },
         teardownContent: @escaping @MainActor () -> Void = {},
         namespaceChanged: @escaping @MainActor (String?) -> Void = { _ in },
         startTasks: Bool = true) {
        self.model = model
        self.vault = vault
        self.makeAPI = makeAPI
        self.teardownContent = teardownContent
        self.namespaceChanged = namespaceChanged
        identity = AppModel.serverIdentity(model.serverURL)
        // Old installs used one shared access token; the server no longer accepts it.
        vault.removeLegacyToken()
        loadSession()
        model.onServerURLChange = { [weak self] in self?.serverChanged() }
        AuthFailure.sink = { [weak self] failure, token in self?.handle(failure, token: token) }
        if startTasks {
            startPolling()
            Task { await refresh() }
        }
    }

    // MARK: - Session

    private func loadSession() {
        let stored = vault.load(identity: identity)
        token = stored?.token
        me = stored?.token == nil ? nil : stored?.me
        publishSession()
    }

    private func save() {
        guard let token else { vault.delete(identity: identity); return }
        vault.save(StoredSession(token: token, me: me), identity: identity)
    }

    private func publishSession() {
        model.setSession(token: token, identity: token == nil ? nil : identity)
        if gate == .app { model.setBrowsingDemo(false) }
        namespaceChanged(rotationNamespace)
    }

    /// Settings changed the server. A different server identity means a different session (often none).
    private func serverChanged() {
        let next = AppModel.serverIdentity(model.serverURL)
        guard next != identity else { return }
        // Plan [R1-6]: changing the server signs out (a session never moves to another server).
        if token != nil { signOutLocally() } else { teardown() }
        identity = next
        pendingApprovals = 0
        loadSession()
        Task { await refresh() }
    }

    /// The https origin to send account traffic to: the active server, else the configured one, if it belongs
    /// to this identity. Sessions never travel over plain http or to another server.
    private var accountRoot: URL? {
        for s in [model.activeServer, model.serverURL] where AppModel.serverIdentity(s) == identity {
            if let c = LiveAPIClient(serverURL: s, token: nil), c.root.scheme == "https" { return c.root }
        }
        return nil
    }

    private func api(authenticated: Bool = true) throws -> any AccountAPI {
        guard let root = accountRoot else { throw AccountError.needsHTTPS }
        return makeAPI(root, authenticated ? token : nil)
    }

    // MARK: - Sign in / up / out

    func signUp(username: String, password: String, displayName: String) async throws {
        let attempt = beginAuthAttempt()
        let r = try await api(authenticated: false).signUp(username: username, password: password,
                                                           displayName: displayName)
        try adopt(r, attempt: attempt)
        await requestPushPermission()
    }

    func signIn(username: String, password: String) async throws {
        let attempt = beginAuthAttempt()
        let r = try await api(authenticated: false).signIn(username: username, password: password)
        try adopt(r, attempt: attempt)
        if r.user.isAdmin { await requestPushPermission() }
        await refreshPendingCount()
    }

    private func beginAuthAttempt() -> (id: Int, identity: String, epoch: Int) {
        authAttempt += 1
        return (authAttempt, identity, epoch)
    }

    /// A response for another server (Settings changed meanwhile), an older epoch or a superseded attempt is
    /// dropped: its token must never be saved under, or sent to, another identity.
    private func adopt(_ r: AuthResponse, attempt: (id: Int, identity: String, epoch: Int)) throws {
        guard attempt.id == authAttempt, attempt.identity == identity, attempt.epoch == epoch else {
            throw CancellationError()
        }
        if token != nil { teardown() }
        token = r.token
        me = r.user
        save()
        publishSession()
        uploadedPush = nil
        Task { await uploadPushToken() }
    }

    /// Sign Out: drop the push device and the session on the server (best effort), then locally.
    /// The old API is captured first and the local teardown runs at once, so playback stops even if the network
    /// is slow, and a later sign-in can't be cleared by this one finishing.
    func signOut() async {
        let old = token != nil ? try? api() : nil
        signOutLocally()
        if let old {
            try? await old.deletePushToken()
            try? await old.signOut()
        }
    }

    /// Clear the session and run the teardown path; Welcome follows from `gate`.
    func signOutLocally() {
        teardown()
        token = nil
        me = nil
        pendingApprovals = 0
        uploadedPush = nil
        vault.delete(identity: identity)
        publishSession()
        Self.setAppBadge(0)
    }

    /// DELETE /api/auth/me, then purge this account's Keychain session, Rotation metadata, files and download
    /// task records, and cached `me`.
    func deleteAccount(password: String) async throws {
        // Captured first: by the time the DELETE returns, `me` may be cleared (a request saw signed_out) or
        // belong to another account.
        let namespace = me.map { "\(identity)/\($0.userId)" }
        let deleting = (identity: identity, token: token, epoch: epoch)
        try await api().deleteAccount(password: password)
        if deleting.identity == identity, deleting.token == token, deleting.epoch == epoch { signOutLocally() }
        if let namespace { RotationStore.purge(namespace: namespace) }
    }

    func changePassword(old: String, new: String) async throws {
        if let replacement = try await api().changePassword(old: old, new: new) {
            token = replacement
            save()
            publishSession()
        }
    }

    /// Stop playback, clear the queue and Now Playing, cancel account-bound work and bump the epoch.
    private func teardown() {
        epoch += 1
        teardownContent()
    }

    // MARK: - Refresh

    /// Re-read `me` (launch, foreground, poll, push). Keeps the cached `me` on network errors.
    func refresh() async {
        guard token != nil, !refreshing, let api = try? api() else { return }
        refreshing = true
        defer { refreshing = false }
        let e = epoch, t = token
        guard let fresh = try? await api.me(), e == epoch, t == token else { return }
        apply(fresh)
        await refreshPendingCount()
        await uploadPushToken()
    }

    private func apply(_ fresh: AccountUser) {
        let wasApproved = gate == .app
        let wasAdmin = me?.isAdmin == true
        me = fresh
        save()
        if wasApproved && gate != .app { teardown() }
        if wasApproved != (gate == .app) {
            publishSession()
            if gate == .app { model.reload() }
        }
        if !wasAdmin && fresh.isAdmin && gate == .app { Task { await requestPushPermission() } }
    }

    /// A streamed item failed in AVPlayer, which hides the HTTP status: ask the server who we are (at most every
    /// 10 s), so a revoked session reaches the shared auth path.
    func verifySession() {
        guard token != nil, Date.now.timeIntervalSince(lastVerify) > 10 else { return }
        lastVerify = .now
        Task { await refresh() }
    }

    private func startPolling() {
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                guard let self else { return }
                if self.gate == .status(.pending) { await self.refresh() }
            }
        }
    }

    // MARK: - Auth failures

    /// The shared AuthFailure path. Ignores failures of requests made with another (older) session.
    func handle(_ failure: AuthFailure, token used: String?) {
        guard let used, used == token else { return }
        switch failure {
        case .signedOut:
            signOutLocally()
        case .pending, .rejected, .disabled:
            // Keep the session: account actions and deletion still work from the status screen.
            if let status = failure.status, var m = me, m.status != status {
                let wasApproved = gate == .app
                m.status = status
                me = m
                save()
                if wasApproved { teardown() }
                publishSession()
            }
            Task { await refresh() }
        case .notAdmin:
            Task { await refresh() }
        case .badCredentials, .usernameTaken, .rateLimited:
            break
        }
    }

    // MARK: - Admin

    func users(status: AccountStatus? = nil) async throws -> [AccountUser] {
        try await api().users(status: status)
    }

    func perform(_ action: AdminAction, on user: AccountUser) async throws {
        try await api().admin(action, userId: user.userId)
        await refreshPendingCount()
    }

    /// True when this account is the only approved admin (or that can't be checked), so deleting it warns that
    /// approvals stop until `create-admin` is run.
    func isLastAdmin() async -> Bool {
        guard me?.isAdmin == true else { return false }
        guard let all = try? await users() else { return true }
        return all.filter { $0.isAdmin && $0.status == .approved && $0.userId != me?.userId }.isEmpty
    }

    func refreshPendingCount() async {
        guard isAdmin, let api = try? api() else {
            if pendingApprovals != 0 { pendingApprovals = 0; Self.setAppBadge(0) }
            return
        }
        let e = epoch
        guard let pending = try? await api.users(status: .pending), e == epoch else { return }
        pendingApprovals = pending.count
        Self.setAppBadge(pending.count)
    }

    private static func setAppBadge(_ n: Int) {
        UNUserNotificationCenter.current().setBadgeCount(n) { _ in }
    }

    // MARK: - Push

    /// Asked after sign-up (to hear about approval) and for admins (to hear about sign-ups).
    func requestPushPermission() async {
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound])
        UIApplication.shared.registerForRemoteNotifications()
    }

    func didRegisterForPush(deviceToken data: Data) {
        deviceToken = data.map { String(format: "%02x", $0) }.joined()
        Task { await uploadPushToken() }
    }

    private func uploadPushToken() async {
        guard let deviceToken, let token, uploadedPush != deviceToken + token, let api = try? api() else { return }
        let key = deviceToken + token
        if (try? await api.uploadPushToken(deviceToken, env: Self.pushEnv)) != nil, token == self.token {
            uploadedPush = key
        }
    }

    /// A push arrived or was tapped: refresh `me` (approval/rejection) and the admin badge.
    func pushReceived(tapped: Bool) {
        Task {
            await refresh()
            if tapped, me?.isAdmin == true, gate == .app { showApprovals = true }
        }
    }
}

enum AccountError: LocalizedError {
    case needsHTTPS

    var errorDescription: String? {
        "Signing in needs the Sethster server’s secure (https://) address. Check the server in Settings."
    }
}

/// A plain-English message for account forms (auth codes, network errors).
@MainActor func accountMessage(_ error: any Error) -> String {
    if case APIError.auth(let f) = error { return f.message }
    if let e = error as? AccountError { return e.errorDescription ?? "" }
    return SettingsView.reason(error)
}

// MARK: - Keychain

struct StoredSession: Codable, Sendable {
    let token: String
    let me: AccountUser?
}

protocol SessionVault: Sendable {
    func load(identity: String) -> StoredSession?
    func save(_ session: StoredSession, identity: String)
    func delete(identity: String)
    func removeLegacyToken()
}

struct KeychainSessionVault: SessionVault {
    private static func key(_ identity: String) -> String { "session:" + identity }

    func load(identity: String) -> StoredSession? {
        Keychain.get(Self.key(identity)).flatMap { try? JSONDecoder().decode(StoredSession.self, from: Data($0.utf8)) }
    }

    func save(_ session: StoredSession, identity: String) {
        guard let data = try? JSONEncoder().encode(session) else { return }
        Keychain.set(String(decoding: data, as: UTF8.self), for: Self.key(identity))
    }

    func delete(identity: String) { Keychain.set("", for: Self.key(identity)) }

    func removeLegacyToken() {
        if Keychain.get("token") != nil { Keychain.set("", for: "token") }
    }
}

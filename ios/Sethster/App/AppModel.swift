import Foundation
import Network
import Observation
import Security

/// App-wide state: server settings and the active API client.
@Observable
@MainActor
final class AppModel {
    /// Server addresses come from Info.plist, filled from ios/Config/Local.xcconfig (gitignored) at build time.
    /// A string value, or a space-separated list.
    nonisolated private static func configList(_ key: String) -> [String] {
        let raw = Bundle.main.object(forInfoDictionaryKey: key) as? String ?? ""
        return raw.split(whereSeparator: \.isWhitespace).map(String.init)
    }
    /// The server's public HTTPS address; no Tailscale or token needed. Placeholder when not configured.
    static let defaultServer = configList("SethsterDefaultServer").first ?? "https://sethster.example.com"
    /// Tried when the configured server doesn't answer: the router can't loop the public address back to the
    /// NAS from inside the house, so the Tailscale HTTPS name. (The plain-http LAN address was dropped: account
    /// sessions only travel over HTTPS.)
    static let fallbackServers: [String] = configList("SethsterFallbackServers")
    /// The public name and the Tailscale name are the same server: one session works on either (plan: Transport).
    nonisolated static let trustedAliases: Set<String> = Set(configList("SethsterTrustedAliases"))
    /// Earlier built-in defaults (the Mac, the http LAN fallback). A saved server equal to one of these is
    /// migrated to `defaultServer`.
    static let legacyDefaults: Set<String> = Set(configList("SethsterLegacyDefaults"))

    var serverURL: String {
        didSet {
            guard serverURL != oldValue else { return }
            UserDefaults.standard.set(serverURL, forKey: "serverURL")
            onServerURLChange?()
            settingsChanged()
        }
    }
    /// The signed-in account's session and the server identity it belongs to (set by AccountStore). Attached
    /// only to HTTPS requests to that server.
    private(set) var sessionToken: String?
    @ObservationIgnored private var sessionIdentity: String?
    /// "Browse Demo Data" from Welcome / the status screen: bundled fixtures without an account. Not persisted.
    private(set) var browsingDemo = false
    /// AccountStore: a different server means a different session.
    @ObservationIgnored var onServerURLChange: (@MainActor () -> Void)?
    var useDemoData: Bool { didSet { UserDefaults.standard.set(useDemoData, forKey: "useDemoData"); settingsChanged() } }

    /// True while the configured server can't be reached and the app is automatically serving the bundled
    /// demo recordings instead (TestFlight / App Review can't see the private server). Distinct from the
    /// user's explicit `useDemoData` toggle.
    private(set) var isOfflineFallback = false
    /// A reachability check is in flight.
    private(set) var isCheckingServer = false
    /// The server actually in use: `serverURL`, or a reachable fallback candidate when that one isn't.
    private(set) var activeServer: String

    nonisolated static let healthTimeout: Duration = .seconds(4)

    private(set) var api: any SethsterAPI
    /// Bumped whenever the data source changes so every loaded screen refetches.
    private(set) var generation = 0

    init(demo: Bool? = nil) {
        let d = UserDefaults.standard
        var url = d.string(forKey: "serverURL") ?? Self.defaultServer
        if Self.legacyDefaults.contains(url.trimmingCharacters(in: .whitespacesAndNewlines)) {
            url = Self.defaultServer
            d.set(url, forKey: "serverURL")
        }
        serverURL = url
        activeServer = url
        useDemoData = demo ?? d.bool(forKey: "useDemoData")
        api = MockAPIClient.shared
        rebuild()
    }

    private func rebuild() {
        if useDemoData || isOfflineFallback || browsingDemo {
            api = MockAPIClient.shared
        } else {
            api = LiveAPIClient(serverURL: activeServer, token: credential(for: activeServer))
                ?? LiveAPIClient(serverURL: Self.defaultServer, token: nil)!
        }
        generation += 1
    }

    func setSession(token: String?, identity: String?) {
        guard token != sessionToken || identity != sessionIdentity else { return }
        sessionToken = token
        sessionIdentity = identity
        rebuild()
    }

    func setBrowsingDemo(_ on: Bool) {
        guard on != browsingDemo else { return }
        browsingDemo = on
        rebuild()
    }

    /// The session token, only for an https address of the server the session belongs to.
    private func credential(for server: String) -> String? {
        guard let sessionToken, let sessionIdentity, Self.serverIdentity(server) == sessionIdentity,
              LiveAPIClient(serverURL: server, token: nil)?.root.scheme == "https" else { return nil }
        return sessionToken
    }

    /// The server a session belongs to: the normalized origin, with the trusted aliases folded into one.
    nonisolated static func serverIdentity(_ server: String) -> String {
        guard let root = LiveAPIClient(serverURL: server, token: nil)?.root,
              let scheme = root.scheme?.lowercased(), let host = root.host()?.lowercased() else {
            return server.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        }
        let origin = "\(scheme)://\(host)" + (root.port.map { ":\($0)" } ?? "")
        return trustedAliases.contains(origin) ? "sethster-nas" : origin
    }

    func reload() { generation += 1 }

    private func settingsChanged() {
        isOfflineFallback = false
        activeServer = serverURL
        rebuild()
        Task { await checkServer() }
    }

    /// The configured server first, then the fallback candidates (deduplicated).
    private var candidates: [String] {
        var seen = Set<String>()
        return ([serverURL] + Self.fallbackServers).filter { seen.insert($0).inserted }
    }

    /// Health-checks the configured server and the fallback candidates in parallel (≈4 s timeout) and uses the
    /// first reachable one in preference order. None reachable → bundled demo data; reachable again → live
    /// data. No-op when the user chose demo data explicitly.
    /// Called on launch, on foreground, on network changes and from the banner's Retry button.
    func checkServer() async {
        guard !useDemoData, !isCheckingServer else { return }
        isCheckingServer = true
        defer { isCheckingServer = false }
        let urls = candidates
        // Health probes go without credentials.
        let clients = urls.map { LiveAPIClient(serverURL: $0, token: nil) }
        let reachable: [Bool] = await withTaskGroup(of: (Int, Bool).self) { group in
            for (i, c) in clients.enumerated() {
                group.addTask {
                    guard let c else { return (i, false) }
                    return (i, await Self.isReachable(c))
                }
            }
            var out = Array(repeating: false, count: clients.count)
            for await (i, ok) in group { out[i] = ok }
            return out
        }
        guard !useDemoData, urls == candidates else { return }
        let chosen = reachable.firstIndex(of: true).map { urls[$0] }
        let offline = chosen == nil
        if offline != isOfflineFallback || (chosen != nil && chosen != activeServer) {
            isOfflineFallback = offline
            if let chosen { activeServer = chosen }
            rebuild()
        }
    }

    nonisolated private static func isReachable(_ api: LiveAPIClient) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask { (try? await api.health())?.ok ?? false }
            group.addTask { try? await Task.sleep(for: healthTimeout); return false }
            let first = await group.next() ?? false
            group.cancelAll()
            return first
        }
    }

    // MARK: - Network changes

    @ObservationIgnored private var pathMonitor: NWPathMonitor?

    /// Re-check reachability whenever the network path changes (Wi-Fi joins/leaves, VPN, etc.).
    func startMonitoringNetwork() {
        guard pathMonitor == nil else { return }
        let monitor = NWPathMonitor()
        pathMonitor = monitor
        monitor.pathUpdateHandler = { [weak self] _ in
            Task { @MainActor in
                // Let the new path settle before probing.
                try? await Task.sleep(for: .milliseconds(800))
                await self?.checkServer()
            }
        }
        monitor.start(queue: DispatchQueue(label: "sethster.path"))
    }

    static let preview = AppModel(demo: true)
}

enum Keychain {
    private static let service = "com.sethster.app"

    static func set(_ value: String, for key: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrService as String: service, kSecAttrAccount as String: key]
        SecItemDelete(base as CFDictionary)
        guard !value.isEmpty else { return }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    static func get(_ key: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: key, kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

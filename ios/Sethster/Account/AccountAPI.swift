import Foundation

/// /api/auth and /api/admin (plan: Endpoints). A protocol so AccountStore can be driven by a mock.
protocol AccountAPI: Sendable {
    func signUp(username: String, password: String, displayName: String) async throws -> AuthResponse
    func signIn(username: String, password: String) async throws -> AuthResponse
    func me() async throws -> AccountUser
    func signOut() async throws
    func deleteAccount(password: String) async throws
    /// Returns a replacement session token if the server issues one.
    func changePassword(old: String, new: String) async throws -> String?
    func uploadPushToken(_ apnsToken: String, env: String) async throws
    func deletePushToken() async throws
    func users(status: AccountStatus?) async throws -> [AccountUser]
    func admin(_ action: AdminAction, userId: String) async throws
}

enum AdminAction: String, Sendable {
    case approve, reject, disable, enable
    case makeAdmin = "make-admin"
}

struct LiveAccountAPI: AccountAPI {
    let root: URL
    let token: String?

    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 15
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: c)
    }()

    private func send(_ method: String, _ path: String, query: [String: String] = [:],
                      body: [String: String]? = nil) async throws -> Data {
        guard var comps = URLComponents(string: root.absoluteString + "/api" + path) else { throw APIError.badURL }
        if !query.isEmpty { comps.queryItems = query.map { URLQueryItem(name: $0, value: $1) } }
        guard let url = comps.url else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await Self.session.data(for: req)
        try AuthFailure.check(data, resp, token: token)
        return data
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        do { return try LiveAPIClient.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(String(describing: error)) }
    }

    func signUp(username: String, password: String, displayName: String) async throws -> AuthResponse {
        try decode(AuthResponse.self, await send("POST", "/auth/signup", body: [
            "username": username, "password": password, "display_name": displayName,
        ]))
    }

    func signIn(username: String, password: String) async throws -> AuthResponse {
        try decode(AuthResponse.self, await send("POST", "/auth/login", body: ["username": username, "password": password]))
    }

    /// Tolerates `Me` bare or wrapped as `{user: Me}`.
    func me() async throws -> AccountUser {
        let data = try await send("GET", "/auth/me")
        struct Wrapped: Decodable { let user: AccountUser }
        if let w = try? LiveAPIClient.decoder.decode(Wrapped.self, from: data) { return w.user }
        return try decode(AccountUser.self, data)
    }

    func signOut() async throws { _ = try await send("POST", "/auth/logout") }

    func deleteAccount(password: String) async throws {
        _ = try await send("DELETE", "/auth/me", body: ["password": password])
    }

    func changePassword(old: String, new: String) async throws -> String? {
        let data = try await send("POST", "/auth/password", body: ["old": old, "new": new])
        struct Reply: Decodable { let token: String? }
        return (try? LiveAPIClient.decoder.decode(Reply.self, from: data))?.token
    }

    func uploadPushToken(_ apnsToken: String, env: String) async throws {
        _ = try await send("POST", "/auth/push-token", body: ["apns_token": apnsToken, "env": env])
    }

    func deletePushToken() async throws { _ = try await send("DELETE", "/auth/push-token") }

    /// Tolerates a bare array, `{items: [...]}` or `{users: [...]}`.
    func users(status: AccountStatus?) async throws -> [AccountUser] {
        let data = try await send("GET", "/admin/users", query: status.map { ["status": $0.rawValue] } ?? [:])
        if let list = try? LiveAPIClient.decoder.decode([AccountUser].self, from: data) { return list }
        struct Items: Decodable { let items: [AccountUser]?; let users: [AccountUser]? }
        let wrapped = try decode(Items.self, data)
        return wrapped.items ?? wrapped.users ?? []
    }

    func admin(_ action: AdminAction, userId: String) async throws {
        let id = userId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed
            .subtracting(CharacterSet(charactersIn: "/"))) ?? userId
        _ = try await send("POST", "/admin/users/\(id)/\(action.rawValue)")
    }
}

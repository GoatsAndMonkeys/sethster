import Foundation

/// An account as the server reports it: `Me` from /api/auth/me, and each row of /api/admin/users.
struct AccountUser: Codable, Hashable, Sendable, Identifiable {
    let userId: String
    let username: String
    let displayName: String?
    var status: AccountStatus
    let isAdmin: Bool
    let createdAt: String?

    var id: String { userId }
    var name: String { displayName.flatMap { $0.isEmpty ? nil : $0 } ?? username }

    init(userId: String, username: String, displayName: String?, status: AccountStatus, isAdmin: Bool,
         createdAt: String? = nil) {
        self.userId = userId
        self.username = username
        self.displayName = displayName
        self.status = status
        self.isAdmin = isAdmin
        self.createdAt = createdAt
    }

    private enum CodingKeys: String, CodingKey { case userId, id, username, displayName, status, isAdmin, createdAt }

    /// Tolerant: `user_id` (or `id`) may be a number or a string, `is_admin` a bool or 0/1.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let key: CodingKeys = c.contains(.userId) ? .userId : .id
        if let n = try? c.decode(Int.self, forKey: key) { userId = String(n) } else { userId = try c.decode(String.self, forKey: key) }
        username = try c.decode(String.self, forKey: .username)
        displayName = try c.decodeIfPresent(String.self, forKey: .displayName)
        status = (try? c.decode(AccountStatus.self, forKey: .status)) ?? .pending
        if let b = try? c.decode(Bool.self, forKey: .isAdmin) { isAdmin = b }
        else { isAdmin = ((try? c.decode(Int.self, forKey: .isAdmin)) ?? 0) != 0 }
        createdAt = try? c.decodeIfPresent(String.self, forKey: .createdAt)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(userId, forKey: .userId)
        try c.encode(username, forKey: .username)
        try c.encodeIfPresent(displayName, forKey: .displayName)
        try c.encode(status, forKey: .status)
        try c.encode(isAdmin, forKey: .isAdmin)
        try c.encodeIfPresent(createdAt, forKey: .createdAt)
    }
}

enum AccountStatus: String, Codable, Hashable, Sendable {
    case pending, approved, rejected, disabled
}

/// `{token, user: Me}` from sign-up and login.
struct AuthResponse: Codable, Sendable {
    let token: String
    let user: AccountUser
}

/// The server's stable auth error codes (plan: Dependencies / Auth behaviour), plus the two non-code account
/// errors (409 username taken, 429 throttled). One mapper shared by the API client, radio client, player and
/// download coordinator.
enum AuthFailure: Error, Equatable, Sendable {
    case signedOut, pending, rejected, disabled, notAdmin, badCredentials
    case usernameTaken
    case rateLimited(retryAfter: Int?)

    /// The status a 403 moves the account to, if any.
    var status: AccountStatus? {
        switch self {
        case .pending: .pending
        case .rejected: .rejected
        case .disabled: .disabled
        default: nil
        }
    }

    /// Maps an HTTP failure. The code is read from a top-level `code` or FastAPI's `detail.code`
    /// (or a bare `detail` string). 401/403 without a known code are left to the caller as plain HTTP errors.
    static func from(status: Int, body: Data, retryAfter: String?) -> AuthFailure? {
        switch status {
        case 409: return .usernameTaken
        case 429: return .rateLimited(retryAfter: retryAfter.flatMap { Int($0.trimmingCharacters(in: .whitespaces)) })
        case 401, 403:
            switch code(in: body) {
            case "signed_out": return .signedOut
            case "pending": return .pending
            case "rejected": return .rejected
            case "disabled": return .disabled
            case "not_admin": return .notAdmin
            case "bad_credentials": return .badCredentials
            default: return nil
            }
        default: return nil
        }
    }

    static func code(in body: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
        if let c = obj["code"] as? String { return c }
        if let d = obj["detail"] as? [String: Any], let c = d["code"] as? String { return c }
        if let d = obj["detail"] as? String { return d }
        return nil
    }

    /// Where mapped failures go (AccountStore). `token` is the credential the failed request carried, so a
    /// response to a request made with an older session can't sign out the current one.
    @MainActor static var sink: ((AuthFailure, String?) -> Void)?

    nonisolated static func report(_ failure: AuthFailure, token: String?) {
        Task { @MainActor in sink?(failure, token) }
    }

    /// Throws for a non-2xx response: `APIError.auth` (after reporting it) for a mapped failure, else `.http`.
    nonisolated static func check(_ data: Data, _ response: URLResponse, token: String?) throws {
        guard let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) else { return }
        if let f = from(status: http.statusCode, body: data, retryAfter: http.value(forHTTPHeaderField: "Retry-After")) {
            report(f, token: token)
            throw APIError.auth(f)
        }
        throw APIError.http(http.statusCode, String(decoding: data, as: UTF8.self))
    }

    /// The bearer token on a request, for media/download failures that only have the request.
    nonisolated static func token(of request: URLRequest?) -> String? {
        guard let h = request?.value(forHTTPHeaderField: "Authorization"), h.hasPrefix("Bearer ") else { return nil }
        return String(h.dropFirst(7))
    }

    var message: String {
        switch self {
        case .signedOut: "You’ve been signed out. Sign in again to keep listening."
        case .pending: "Your account is waiting for approval."
        case .rejected: "Your Sethster request wasn’t approved."
        case .disabled: "Your Sethster account has been turned off."
        case .notAdmin: "Only an admin can do that."
        case .badCredentials: "That username and password don’t match."
        case .usernameTaken: "That username isn’t available. Try another."
        case .rateLimited(let s):
            if let s, s > 90 { "Too many tries. Try again in \(Int((Double(s) / 60).rounded(.up))) minutes." }
            else if let s { "Too many tries. Try again in \(s) seconds." }
            else { "Too many tries. Try again in a few minutes." }
        }
    }
}

enum AccountLinks {
    static let privacy = URL(string: "https://goatsandmonkeys.github.io/sethster-privacy/")!
}

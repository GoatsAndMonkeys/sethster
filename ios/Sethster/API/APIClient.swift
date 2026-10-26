import Foundation

/// The contract in docs/API.md. `LiveAPIClient` talks to sethster-server; `MockAPIClient` serves
/// bundled fixtures (previews, offline/demo mode).
protocol SethsterAPI: Sendable {
    func health() async throws -> Health
    func home() async throws -> HomeResponse
    func performances(_ query: PerformanceQuery) async throws -> ItemsResponse<PerformanceSummary>
    func performance(id: String) async throws -> PerformanceDetail
    func composers() async throws -> [ComposerSummary]
    func composer(id: String) async throws -> ComposerDetail
    func work(id: String) async throws -> WorkDetail
    func people(q: String?) async throws -> [PersonSummary]
    func person(id: String) async throws -> PersonDetail
    func venue(id: String) async throws -> VenueDetail
    func search(q: String) async throws -> SearchResponse
    func questions() async throws -> [Question]
    func answer(performanceId: String, question: String, answer: String) async throws
    func rotation(listener: String) async throws -> RotationResponse
    func addToRotation(performanceId: String, listener: String) async throws -> RotationItem
    func removeFromRotation(performanceId: String, listener: String) async throws
    func reorderRotation(performanceIds: [String], listener: String) async throws -> RotationResponse

    // Phase 3 (liner notes). Clients without them throw 404, and the app hides those sections.
    func linerNotes(performanceId: String) async throws -> LinerNotes
    func transcript(performanceId: String) async throws -> Transcript
    func programNote(performanceId: String, n: Int) async throws -> ProgramNote
    func versions(workId: String) async throws -> WorkVersions

    /// Absolute URL for a server-relative path like `/api/people/x/image`. nil if unavailable.
    func resolve(_ path: String?) -> URL?
    /// Headers to attach to raw media requests (images, audio).
    var authHeaders: [String: String] { get }
    var isDemo: Bool { get }
    /// The session token the client sends, if any (for the shared AuthFailure path).
    var sessionToken: String? { get }
}

extension SethsterAPI {
    var sessionToken: String? { nil }

    /// `authHeaders`, but only for the server's own origin; third-party URLs (live streams, logos) get none.
    func authHeaders(for url: URL) -> [String: String] {
        guard let own = resolve("/"), own.scheme == url.scheme, own.host() == url.host(), own.port == url.port
        else { return [:] }
        return authHeaders
    }

    /// An AVPlayer media URL. On the server's own origin it also carries `?token=` (AVPlayer can't be relied on
    /// to send headers on every request); never on third-party URLs.
    func mediaURL(_ path: String?) -> URL? {
        guard let url = resolve(path) else { return nil }
        guard let token = sessionToken, !authHeaders(for: url).isEmpty,
              var comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return url }
        comps.queryItems = (comps.queryItems ?? []).filter { $0.name != "token" } + [URLQueryItem(name: "token", value: token)]
        return comps.url ?? url
    }

    func linerNotes(performanceId: String) async throws -> LinerNotes { throw APIError.http(404, "") }
    func transcript(performanceId: String) async throws -> Transcript { throw APIError.http(404, "") }
    func programNote(performanceId: String, n: Int) async throws -> ProgramNote { throw APIError.http(404, "") }
    func versions(workId: String) async throws -> WorkVersions { throw APIError.http(404, "") }

    func rotation(listener: String = "default") async throws -> RotationResponse {
        throw APIError.http(404, "Rotation is not available.")
    }
    func addToRotation(performanceId: String, listener: String = "default") async throws -> RotationItem {
        throw APIError.http(404, "Rotation is not available.")
    }
    func removeFromRotation(performanceId: String, listener: String = "default") async throws {
        throw APIError.http(404, "Rotation is not available.")
    }
    func reorderRotation(performanceIds: [String], listener: String = "default") async throws -> RotationResponse {
        throw APIError.http(404, "Rotation is not available.")
    }

    /// Venues have no list endpoint in v0; derive them from the performance list.
    func venues() async throws -> [VenueSummary] {
        let all = try await performances(PerformanceQuery(sort: "title", limit: 1000)).items
        var counts: [String: (VenueRef, Int)] = [:]
        for p in all { if let v = p.venue { counts[v.id] = (v, (counts[v.id]?.1 ?? 0) + 1) } }
        return counts.values
            .map { VenueSummary(id: $0.0.id, name: $0.0.name, city: $0.0.city, count: $0.1) }
            .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }
}

enum APIError: LocalizedError {
    case badURL
    case http(Int, String)
    case decoding(String)
    /// 401/403/409/429 with a known meaning (AuthFailure); already reported to AccountStore.
    case auth(AuthFailure)

    var errorDescription: String? {
        switch self {
        case .badURL: "The server URL is not valid. Check Settings."
        case .auth(let f): f.message
        case .http(let code, let body): "Server returned \(code). \(body.prefix(200))"
        case .decoding(let msg): "Unexpected data from server: \(msg)"
        }
    }
}

struct LiveAPIClient: SethsterAPI {
    let root: URL       // e.g. http://localhost:8642
    let token: String?
    var isDemo: Bool { false }
    var sessionToken: String? { token }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15
        config.requestCachePolicy = .reloadRevalidatingCacheData
        return URLSession(configuration: config)
    }()

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// Accepts "host:port", "http://host:port", or ".../api" and normalises to the origin.
    init?(serverURL: String, token: String?) {
        var s = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        if s.hasSuffix("/api") { s.removeLast(4) }
        guard let url = URL(string: s), url.host() != nil else { return nil }
        root = url
        self.token = (token?.isEmpty ?? true) ? nil : token
    }

    var authHeaders: [String: String] {
        token.map { ["Authorization": "Bearer \($0)"] } ?? [:]
    }

    func resolve(_ path: String?) -> URL? {
        guard let path, !path.isEmpty else { return nil }
        if path.hasPrefix("http://") || path.hasPrefix("https://") { return URL(string: path) }
        let p = path.hasPrefix("/") ? path : "/api/" + path
        return URL(string: root.absoluteString + p)
    }

    private func url(_ path: String, _ query: [String: String?] = [:]) throws -> URL {
        guard var comps = URLComponents(string: root.absoluteString + "/api" + path) else { throw APIError.badURL }
        let items = query.compactMap { k, v in v.flatMap { $0.isEmpty ? nil : URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { comps.queryItems = items.sorted { $0.name < $1.name } }
        guard let u = comps.url else { throw APIError.badURL }
        return u
    }

    private func get<T: Decodable & Sendable>(_ path: String, _ query: [String: String?] = [:]) async throws -> T {
        var req = URLRequest(url: try url(path, query))
        authHeaders.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        return try await send(req)
    }

    private func send<T: Decodable & Sendable>(_ req: URLRequest) async throws -> T {
        let (data, resp) = try await Self.session.data(for: req)
        try AuthFailure.check(data, resp, token: token)
        do { return try Self.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(String(describing: error)) }
    }

    private func request<T: Decodable & Sendable, Body: Encodable & Sendable>(
        _ method: String, path: String, body: Body? = nil, query: [String: String?] = [:]
    ) async throws -> T {
        var req = URLRequest(url: try url(path, query))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        authHeaders.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            req.httpBody = try encoder.encode(body)
        }
        return try await send(req)
    }

    private static func pathID(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
    }

    func health() async throws -> Health { try await get("/health") }
    /// Sends the phone's local date so the daily Spotlight changes at the user's midnight, not the server's.
    func home() async throws -> HomeResponse { try await get("/home", ["date": HistoryClient.iso(.now)]) }

    func performances(_ q: PerformanceQuery) async throws -> ItemsResponse<PerformanceSummary> {
        try await get("/performances", [
            "collection": q.collection, "composer": q.composer, "person": q.person, "venue": q.venue,
            "city": q.city, "year": q.year, "q": q.q, "sort": q.sort, "limit": String(q.limit),
            "offset": String(q.offset),
        ])
    }

    func performance(id: String) async throws -> PerformanceDetail { try await get("/performances/\(Self.pathID(id))") }

    func composers() async throws -> [ComposerSummary] {
        let r: ItemsResponse<ComposerSummary> = try await get("/composers", ["limit": "500"])
        return r.items
    }

    func composer(id: String) async throws -> ComposerDetail { try await get("/composers/\(Self.pathID(id))") }
    func work(id: String) async throws -> WorkDetail { try await get("/works/\(Self.pathID(id))") }

    func people(q: String?) async throws -> [PersonSummary] {
        let r: ItemsResponse<PersonSummary> = try await get("/people", ["q": q, "limit": "1000"])
        return r.items
    }

    func person(id: String) async throws -> PersonDetail { try await get("/people/\(Self.pathID(id))") }
    func venue(id: String) async throws -> VenueDetail { try await get("/venues/\(Self.pathID(id))") }
    func search(q: String) async throws -> SearchResponse { try await get("/search", ["q": q]) }

    func questions() async throws -> [Question] {
        let r: ItemsResponse<Question> = try await get("/questions", ["limit": "500"])
        return r.items
    }

    func answer(performanceId: String, question: String, answer: String) async throws {
        var req = URLRequest(url: try url("/questions/answer"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authHeaders.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        let enc = JSONEncoder(); enc.keyEncodingStrategy = .convertToSnakeCase
        req.httpBody = try enc.encode(AnswerBody(performanceId: performanceId, question: question, answer: answer))
        let _: OKResponse = try await send(req)
    }

    func linerNotes(performanceId: String) async throws -> LinerNotes {
        try await get("/performances/\(Self.pathID(performanceId))/liner-notes")
    }
    func transcript(performanceId: String) async throws -> Transcript { try await get("/transcripts/\(Self.pathID(performanceId))") }
    func programNote(performanceId: String, n: Int) async throws -> ProgramNote {
        try await get("/programs/\(Self.pathID(performanceId))/\(n)")
    }
    func versions(workId: String) async throws -> WorkVersions { try await get("/works/\(Self.pathID(workId))/versions") }

    func rotation(listener: String = "default") async throws -> RotationResponse {
        try await get("/rotation", ["listener": listener])
    }

    func addToRotation(performanceId: String, listener: String = "default") async throws -> RotationItem {
        try await request("POST", path: "/rotation", body: RotationAddBody(performanceId: performanceId,
                                                                            listener: listener))
    }

    func removeFromRotation(performanceId: String, listener: String = "default") async throws {
        let _: OKResponse = try await request("DELETE", path: "/rotation/\(Self.pathID(performanceId))",
                                              body: Optional<RotationAddBody>.none,
                                              query: ["listener": listener])
    }

    func reorderRotation(performanceIds: [String], listener: String = "default") async throws -> RotationResponse {
        try await request("PUT", path: "/rotation",
                          body: RotationOrderBody(performanceIds: performanceIds, listener: listener))
    }
}

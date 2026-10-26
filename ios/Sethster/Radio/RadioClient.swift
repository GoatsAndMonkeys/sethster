import Foundation

/// /api/radio calls, built on the active API client's `resolve` + `authHeaders` (so it follows the same
/// server / fallback / token as everything else). Unavailable (nil) in demo mode.
struct RadioClient: Sendable {
    let base: URL
    let headers: [String: String]
    let token: String?

    init?(api: any SethsterAPI) {
        guard !api.isDemo, let u = api.resolve("/api/radio") else { return nil }
        base = u
        headers = api.authHeaders
        token = api.sessionToken
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 15
        return URLSession(configuration: c)
    }()

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder(); e.keyEncodingStrategy = .convertToSnakeCase; return e
    }()

    private func url(_ path: String, _ query: [String: String?] = [:]) throws -> URL {
        guard var c = URLComponents(string: base.absoluteString + path) else { throw APIError.badURL }
        let items = query.compactMap { k, v in v.flatMap { $0.isEmpty ? nil : URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { c.queryItems = items.sorted { $0.name < $1.name } }
        guard let u = c.url else { throw APIError.badURL }
        return u
    }

    private func send<T: Decodable & Sendable>(_ method: String, _ path: String, query: [String: String?] = [:],
                                               body: (any Encodable & Sendable)? = nil) async throws -> T {
        var req = URLRequest(url: try url(path, query))
        req.httpMethod = method
        headers.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try Self.encoder.encode(body)
        }
        let (data, resp) = try await Self.session.data(for: req)
        try AuthFailure.check(data, resp, token: token)
        do { return try LiveAPIClient.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(String(describing: error)) }
    }

    private static func pathID(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
    }

    struct Items<T: Codable & Sendable>: Codable, Sendable { @DefaultEmpty var items: [T] = [] }

    func seeds(q: String?, type: RadioSeedType?, limit: Int = 60) async throws -> [RadioSeed] {
        let r: Items<RadioSeed> = try await send("GET", "/seeds", query: ["q": q, "type": type?.rawValue, "limit": String(limit)])
        return r.items
    }

    func stations() async throws -> [RadioStation] {
        let r: Items<RadioStation> = try await send("GET", "/stations")
        return r.items
    }

    struct StationBody: Encodable, Sendable { let seeds: [RadioSeedRef] }

    func createStation(seeds: [RadioSeedRef]) async throws -> RadioStation {
        try await send("POST", "/stations", body: StationBody(seeds: seeds))
    }

    func next(station: String, count: Int, exclude: [String]) async throws -> RadioNext {
        try await send("GET", "/stations/\(Self.pathID(station))/next",
                       query: ["count": String(count), "exclude": exclude.joined(separator: ",")])
    }

    func excerpt(id: String) async throws -> RadioExcerpt {
        try await send("GET", "/excerpts/\(Self.pathID(id))")
    }

    struct PlayBody: Encodable, Sendable { let excerptId: String; let stationId: String?; let client: String }
    struct PlayUpdate: Encodable, Sendable { let outcome: String; let listenedS: Double? }

    func startPlay(excerpt: String, station: String?) async throws -> Int? {
        let r: RadioPlayStarted = try await send("POST", "/plays", body: PlayBody(excerptId: excerpt, stationId: station, client: "ios"))
        return r.playId
    }

    func finishPlay(_ id: Int, skipped: Bool, listened: Double) async throws {
        let _: OKResponse = try await send("POST", "/plays/\(id)",
                                           body: PlayUpdate(outcome: skipped ? "skipped" : "played", listenedS: listened))
    }

    func deleteStation(_ id: String) async throws {
        let _: OKResponse = try await send("DELETE", "/stations/\(Self.pathID(id))")
    }

    func live() async throws -> [LiveStation] {
        let r: Items<LiveStation> = try await send("GET", "/live")
        return r.items
    }

    func nowPlaying(live id: String) async throws -> LiveNowPlaying {
        try await send("GET", "/live/\(Self.pathID(id))/now-playing")
    }
}

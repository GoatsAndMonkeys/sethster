import Foundation

/// Sections + Compare endpoints (server compare_api.py, docs/sections.md). Only the live server has them;
/// `Compare.client(for:)` returns nil in demo mode and the views hide themselves.
struct CompareClient: Sendable {
    let live: LiveAPIClient

    private func get<T: Decodable & Sendable>(_ path: String, _ query: [String: String] = [:]) async throws -> T {
        guard var comps = URLComponents(string: live.root.absoluteString + "/api" + path) else { throw APIError.badURL }
        if !query.isEmpty { comps.queryItems = query.map { URLQueryItem(name: $0, value: $1) }.sorted { $0.name < $1.name } }
        guard let url = comps.url else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.timeoutInterval = 20
        live.authHeaders.forEach { req.setValue($1, forHTTPHeaderField: $0) }
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, resp) = try await URLSession.shared.data(for: req)
        try AuthFailure.check(data, resp, token: live.token)
        do { return try LiveAPIClient.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(String(describing: error)) }
    }

    private static func pathID(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
    }

    func sections(performanceId: String) async throws -> SectionsResponse {
        try await get("/performances/\(Self.pathID(performanceId))/sections")
    }

    /// `workId` may be a DB work id or a canonical work id.
    func compare(workId: String) async throws -> CompareResponse {
        try await get("/works/\(Self.pathID(workId))/compare")
    }

    /// The moment in `to` that corresponds to `t` seconds into `trackId`. `mode`: auto | section | moment.
    func jump(from: String, to: String, trackId: String, t: Double, mode: String = "auto") async throws -> JumpTarget {
        try await get("/compare/jump", ["from": from, "to": to, "track_id": trackId,
                                         "t": String(format: "%.2f", max(0, t)), "mode": mode])
    }

    func canonicalWorks(minVersions: Int = 2) async throws -> CanonicalWorksResponse {
        try await get("/canonical-works", ["min_versions": String(minVersions)])
    }
}

enum Compare {
    static func client(for api: any SethsterAPI) -> CompareClient? {
        (api as? LiveAPIClient).map(CompareClient.init(live:))
    }
}

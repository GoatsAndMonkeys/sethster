import Foundation

/// On This Day, calendar shelves and historical context (docs/CALENDAR.md). Only the live server has these;
/// `History.client(for:)` returns nil in demo mode and the views hide themselves.
struct HistoryClient: Sendable {
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

    /// `date` = YYYY-MM-DD in the device's calendar (the server's "today" may be another time zone).
    func onThisDay(date: Date = .now) async throws -> OnThisDayResponse {
        try await get("/on-this-day", ["date": Self.iso(date)])
    }

    func calendar(date: Date = .now) async throws -> CalendarResponse {
        try await get("/calendar", ["date": Self.iso(date)])
    }

    func context(performanceId: String) async throws -> HistoricalContext {
        try await get("/performances/\(Self.pathID(performanceId))/context")
    }

    static func iso(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year ?? 2000, c.month ?? 1, c.day ?? 1)
    }
}

enum History {
    static func client(for api: any SethsterAPI) -> HistoryClient? {
        (api as? LiveAPIClient).map(HistoryClient.init(live:))
    }
}

import Foundation

// Shapes from /api/radio (docs/RADIO.md). Decoded with `.convertFromSnakeCase`; tolerant like Models.swift.

enum RadioSeedType: String, Codable, Sendable, CaseIterable, Identifiable {
    case composer, person, ensemble
    var id: String { rawValue }
    var label: String {
        switch self {
        case .composer: "Composers"
        case .person: "Performers"
        case .ensemble: "Ensembles"
        }
    }
    var icon: String {
        switch self {
        case .composer: "music.quarternote.3"
        case .person: "person.wave.2"
        case .ensemble: "person.3"
        }
    }
}

struct RadioSeed: Codable, Hashable, Sendable, Identifiable {
    let type: RadioSeedType
    let id: String
    let name: String
    let imageUrl: String?
    let subtitle: String?
    let excerptCount: Int?
    let famousCount: Int?
    var stationId: String { "\(type.rawValue):\(id)" }
}

struct RadioSeedRef: Codable, Hashable, Sendable {
    let type: RadioSeedType
    let id: String
    var weight: Double? = nil
}

struct RadioStation: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    @DefaultEmpty var seeds: [RadioSeedRef] = []
    let lastPlayedAt: String?
    let playCount: Int?
    let excerptCount: Int?
}

struct RadioPerformer: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let type: String?
    let role: String?
    let certainty: String?
    let imageUrl: String?
}

struct RadioHighlight: Codable, Hashable, Sendable {
    let id: String
    let title: String
    let type: String?
    let tier: Int?
}

struct RadioWorkRef: Codable, Hashable, Sendable { let id: String; let title: String? }

struct RadioSameNumber: Codable, Hashable, Sendable {
    let excerptId: String
    let performanceId: String
    let title: String?
}

struct RadioExcerpt: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let trackTitle: String?
    let kind: String?
    let startS: Double
    let endS: Double?
    let durationS: Double?
    let durationEstimated: Bool?
    let streamUrl: String
    let work: RadioWorkRef?
    let composer: Ref?
    let performance: PerformanceSummary?
    @DefaultEmpty var performers: [RadioPerformer] = []
    @DefaultEmpty var characters: [String] = []
    let highlight: RadioHighlight?
    let linerNotesUrl: String?
    let versionsUrl: String?
    let versionCount: Int?
    @DefaultEmpty var sameNumber: [RadioSameNumber] = []
    @DefaultEmpty var reasons: [String] = []

    /// "Maria Callas, Giulietta Simionato · Antonino Votto"
    var performerLine: String {
        let sure = performers.filter { $0.certainty != "maybe" }
        let singers = sure.filter { $0.type == "person" && $0.certainty == "featured" }.prefix(3).map(\.name)
        let rest = sure.filter { $0.type == "person" && $0.certainty == "present" && ($0.role ?? "").lowercased().contains("conduct") }
            .prefix(1).map(\.name)
        let line = (singers + rest).joined(separator: ", ")
        return line.isEmpty ? (performance?.subtitle ?? "") : line
    }

    var kindLabel: String? {
        guard let k = kind, !["excerpt", "number"].contains(k) else { return nil }
        return k.capitalized
    }
}

struct RadioNext: Codable, Sendable {
    let station: RadioStationRef
    @DefaultEmpty var items: [RadioExcerpt] = []
    struct RadioStationRef: Codable, Sendable { let id: String; let name: String }
}

struct RadioPlayStarted: Codable, Sendable { let ok: Bool?; let playId: Int? }

struct LiveStation: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let broadcaster: String?
    let country: String?
    let city: String?
    let homepage: String?
    let streamUrl: String
    let format: String?
    let bitrateKbps: Int?
    let logoUrl: String?
    let description: String?
    let airsOpera: Bool?
    @DefaultEmpty var tags: [String] = []
    let hasNowPlaying: Bool?
}

struct LiveNowPlaying: Codable, Hashable, Sendable {
    var title: String? = nil
    var artist: String? = nil
    var composer: String? = nil
    var programme: String? = nil
    var raw: String? = nil

    var lines: [String] {
        var out: [String] = []
        let main = [composer, title].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: ": ")
        if !main.isEmpty { out.append(main) }
        if let a = artist, !a.isEmpty { out.append(a) }
        if out.isEmpty, let r = raw, !r.isEmpty { out.append(r) }
        if let p = programme, !p.isEmpty, !out.contains(p) { out.append(p) }
        return out
    }
    var isEmpty: Bool { lines.isEmpty }
}

/// The bundled copy of data/radio/live_stations.json (fallback when the server is unreachable / demo mode).
private struct BundledLiveFile: Decodable {
    struct Station: Decodable {
        let id: String; let name: String; let broadcaster: String?; let country: String?; let city: String?
        let homepage: String?; let streamUrl: String?; let format: String?; let bitrateKbps: Int?
        let logoUrl: String?; let description: String?; let airsOpera: Bool?; let tags: [String]?
        let metadata: Meta?
        struct Meta: Decodable { let type: String? }
    }
    let stations: [Station]
}

enum BundledLiveStations {
    static let all: [LiveStation] = {
        guard let url = Bundle.main.url(forResource: "live_stations", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let file = try? LiveAPIClient.decoder.decode(BundledLiveFile.self, from: data) else { return [] }
        return file.stations.compactMap { s in
            guard let stream = s.streamUrl else { return nil }
            return LiveStation(id: s.id, name: s.name, broadcaster: s.broadcaster, country: s.country, city: s.city,
                               homepage: s.homepage, streamUrl: stream, format: s.format, bitrateKbps: s.bitrateKbps,
                               logoUrl: s.logoUrl, description: s.description, airsOpera: s.airsOpera,
                               tags: s.tags ?? [], hasNowPlaying: (s.metadata?.type ?? "none") != "none")
        }
    }()
}

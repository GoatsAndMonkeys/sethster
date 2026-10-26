import Foundation

// Shapes from docs/API.md. Decoded with `.convertFromSnakeCase`; nearly everything is optional or
// defaults to empty so a server that omits a field never breaks a whole page.

// MARK: - Decoding helpers

/// Decodes a missing or null array as `[]`.
@propertyWrapper
struct DefaultEmpty<T: Codable & Sendable>: Codable, Sendable {
    var wrappedValue: [T]
    init(wrappedValue: [T] = []) { self.wrappedValue = wrappedValue }
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        wrappedValue = c.decodeNil() ? [] : try c.decode([T].self)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wrappedValue)
    }
}

extension DefaultEmpty: Equatable where T: Equatable {}
extension DefaultEmpty: Hashable where T: Hashable {}

extension KeyedDecodingContainer {
    func decode<T>(_ type: DefaultEmpty<T>.Type, forKey key: Key) throws -> DefaultEmpty<T> {
        try decodeIfPresent(type, forKey: key) ?? DefaultEmpty()
    }
}

/// A string-ish value that tolerates numbers (e.g. `years` could arrive as 1813 or "1813–1901").
struct LooseString: Codable, Hashable, Sendable, CustomStringConvertible {
    let value: String
    var description: String { value }
    init(_ value: String) { self.value = value }
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { value = s }
        else if let i = try? c.decode(Int.self) { value = String(i) }
        else if let d = try? c.decode(Double.self) { value = String(d) }
        else { value = "" }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer(); try c.encode(value)
    }
}

// MARK: - Enums

enum EvidenceStatus: String, Codable, Sendable, CaseIterable {
    case confirmed, productionOnly = "production_only", inferred, conflict, unknown

    init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = EvidenceStatus(rawValue: raw) ?? .unknown
    }
}

enum Segment: String, Codable, Sendable {
    case music, announcer, intermission, applause, interview, other

    init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = Segment(rawValue: raw) ?? .other
    }

    var isMusic: Bool { self == .music }
}

// MARK: - Refs

struct Ref: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
}

struct VenueRef: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let city: String?
}

struct PersonRef: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let imageUrl: String?
}

// MARK: - Performances

struct PerformanceSummary: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let subtitle: String?
    @DefaultEmpty var composers: [Ref] = []
    let date: String?
    let dateStatus: EvidenceStatus?
    let venue: VenueRef?
    let kind: String?
    let collection: String?
    let durationS: Double?
    let artworkUrl: String?
    let confirmedRatio: Double?
}

struct ProgramItem: Codable, Hashable, Sendable {
    let workId: String?
    let title: String
    let composer: Ref?
    let catalogue: String?
    let notes: String?
}

struct Credit: Codable, Hashable, Sendable {
    let person: PersonRef
    let role: String?
    let workId: String?
    let status: EvidenceStatus
    let notes: String?
    @DefaultEmpty var sourceIds: [String] = []
    /// Server-computed plain-English rating (build 8+ servers). Older servers omit it; see `shownRating`.
    var rating: CreditRating? = nil

    /// The server's rating, or a rough estimate from the evidence status when the server has none.
    var shownRating: CreditRating {
        if let rating { return rating }
        let n = Set(sourceIds).count
        switch status {
        case .confirmed:
            return n >= 2 ? CreditRating(stars: 4, confidence: 86, label: "Well backed up", basis: "Confirmed by \(n) sources.")
                          : CreditRating(stars: 3, confidence: 75, label: "Backed up", basis: "Confirmed by a source.")
        case .productionOnly:
            return CreditRating(stars: 2, confidence: 50, label: "Likely",
                                basis: "In this production's cast, but no source names them for this exact date.")
        case .inferred:
            return CreditRating(stars: 2, confidence: 40, label: "Likely",
                                basis: "A careful guess (for example from that season's cast); nothing confirms this exact date.")
        case .conflict:
            return CreditRating(stars: 1, confidence: 25, label: "Unverified", basis: "Sources disagree about this one.")
        case .unknown:
            return CreditRating(stars: 1, confidence: 15, label: "Unverified", basis: "Not checked yet.")
        }
    }
}

/// How well a credit is backed up: 1–5 stars (3 is still good), a percent confidence, and one sentence why.
struct CreditRating: Codable, Hashable, Sendable {
    let stars: Int
    let confidence: Int
    var label: String? = nil
    var basis: String? = nil
}

struct Track: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let index: Int
    let title: String
    let workId: String?
    let segment: Segment
    let durationS: Double?
    let streamUrl: String
    let format: String?
    /// Full research note for the track (server-provided; `title` is the cleaned-up label).
    let description: String?
}

struct Source: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let url: String?
    let waybackUrl: String?
    let title: String?
    let type: String?
    let dateSpecific: Bool?
    let excerpt: String?
}

struct Resource: Codable, Hashable, Sendable, Identifiable {
    let url: String
    let waybackUrl: String?
    let title: String?
    let kind: String?
    let description: String?
    var id: String { url }
}

struct PerformanceDetail: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let subtitle: String?
    @DefaultEmpty var composers: [Ref] = []
    let date: String?
    let dateStatus: EvidenceStatus?
    let venue: VenueRef?
    let kind: String?
    let collection: String?
    let durationS: Double?
    let artworkUrl: String?
    let confirmedRatio: Double?

    let summary: String?
    let dateEvidence: String?
    @DefaultEmpty var candidateDates: [String] = []
    let broadcastDate: String?
    let broadcaster: String?
    let eventName: String?
    @DefaultEmpty var program: [ProgramItem] = []
    @DefaultEmpty var credits: [Credit] = []
    @DefaultEmpty var tracks: [Track] = []
    @DefaultEmpty var sources: [Source] = []
    @DefaultEmpty var resources: [Resource] = []
    @DefaultEmpty var openQuestions: [String] = []
    let folder: String?

    var asSummary: PerformanceSummary {
        PerformanceSummary(id: id, title: title, subtitle: subtitle, composers: composers, date: date,
                           dateStatus: dateStatus, venue: venue, kind: kind, collection: collection,
                           durationS: durationS ?? tracks.compactMap(\.durationS).reduce(0, +),
                           artworkUrl: artworkUrl, confirmedRatio: confirmedRatio)
    }

    func source(_ id: String) -> Source? { sources.first { $0.id == id } }
    func work(_ id: String?) -> ProgramItem? { program.first { $0.workId == id && id != nil } }
}

// MARK: - Composers, works, people, venues

struct ComposerSummary: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let years: LooseString?
    let imageUrl: String?
    let performanceCount: Int?
}

struct WorkSummary: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let composer: Ref?
    let performanceCount: Int?
}

struct ComposerDetail: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let years: LooseString?
    let wikidata: String?
    let imageUrl: String?
    let bio: String?
    var bioLong: String? = nil
    var description: String? = nil
    var imageCredit: String? = nil
    let wikipediaUrl: String?
    @DefaultEmpty var works: [WorkSummary] = []
}

struct WorkDetail: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let composer: Ref?
    let wikidata: String?
    let description: String?
    let wikipediaUrl: String?
    @DefaultEmpty var performances: [PerformanceSummary] = []
}

struct PersonSummary: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let kind: String?
    let imageUrl: String?
    let creditCount: Int?
}

struct PersonCredit: Codable, Hashable, Sendable {
    let performance: PerformanceSummary
    let role: String?
    let status: EvidenceStatus?
}

struct PersonDetail: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let kind: String?
    let years: LooseString?
    let wikidata: String?
    let imageUrl: String?
    let bio: String?
    var bioLong: String? = nil
    var description: String? = nil
    var imageCredit: String? = nil
    let wikipediaUrl: String?
    @DefaultEmpty var credits: [PersonCredit] = []
}

struct VenueSummary: Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let city: String?
    let count: Int
}

struct VenueDetail: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let name: String
    let city: String?
    let wikidata: String?
    @DefaultEmpty var performances: [PerformanceSummary] = []
}

// MARK: - Responses

struct Health: Codable, Sendable {
    let ok: Bool
    let performances: Int?
}

struct CollectionInfo: Codable, Hashable, Sendable, Identifiable {
    let name: String
    let count: Int?
    var id: String { name }
}

struct HomeResponse: Codable, Sendable {
    @DefaultEmpty var recent: [PerformanceSummary] = []
    @DefaultEmpty var onThisDay: [PerformanceSummary] = []
    @DefaultEmpty var collections: [CollectionInfo] = []
    @DefaultEmpty var composers: [ComposerSummary] = []
    /// Today's Spotlight (build 8+ servers): one composer, performer, venue or city, or "Remembering <name>".
    var spotlight: HomeSpotlight? = nil
}

struct HomeSpotlight: Codable, Hashable, Sendable {
    /// "spotlight" or "remembering"
    let mode: String?
    /// "composer", "performer", "venue" or "city"
    let kind: String
    /// Entity id (the city name for cities).
    let id: String
    let name: String
    let eyebrow: String?
    let title: String?
    let subtitle: String?
    let blurb: String?
    let imageUrl: String?
    let count: Int?
    @DefaultEmpty var items: [PerformanceSummary] = []

    var isRemembering: Bool { mode == "remembering" }

    var route: Route {
        switch kind {
        case "composer": .composer(id)
        case "venue": .venue(id)
        case "city": .city(id)
        default: .person(id)
        }
    }
}

struct ItemsResponse<T: Codable & Sendable>: Codable, Sendable {
    @DefaultEmpty var items: [T] = []
    let total: Int?
}

struct SearchResponse: Codable, Sendable {
    @DefaultEmpty var performances: [PerformanceSummary] = []
    @DefaultEmpty var people: [PersonSummary] = []
    @DefaultEmpty var works: [WorkSummary] = []
    @DefaultEmpty var composers: [ComposerSummary] = []

    var isEmpty: Bool { performances.isEmpty && people.isEmpty && works.isEmpty && composers.isEmpty }
}

struct Question: Codable, Hashable, Sendable, Identifiable {
    let performanceId: String
    let performanceTitle: String?
    let question: String
    let answer: String?
    let answeredAt: String?
    var id: String { performanceId + "|" + question }
    var isAnswered: Bool { !(answer ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
}

struct AnswerBody: Codable, Sendable {
    let performanceId: String
    let question: String
    let answer: String
}

struct OKResponse: Codable, Sendable { let ok: Bool? }

struct PerformanceQuery: Hashable, Sendable {
    var collection: String?
    var composer: String?
    var person: String?
    var venue: String?
    var year: String?
    var q: String?
    var sort: String? = "-date"
    var limit: Int = 200
    var offset: Int = 0
    var city: String? = nil
}

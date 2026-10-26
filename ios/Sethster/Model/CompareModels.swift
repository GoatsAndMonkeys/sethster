import Foundation

// Sections + Compare (server: compare_api.py; schema docs/sections.md). Keys arrive snake_case and are
// converted by LiveAPIClient.decoder.

struct SectionPoint: Codable, Hashable, Sendable {
    let trackId: String
    let trackIndex: Int?
    let t: Double?
}

/// One logical section of a performance: a number, movement or act, or a non-music stretch.
struct PerformanceSection: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let kind: String?
    let music: Bool
    let title: String?
    let act: String?
    let group: String?
    let key: String?
    let workId: String?
    let canonicalWorkId: String?
    let start: SectionPoint
    let end: SectionPoint
    let startAbs: Double?
    let durationS: Double
    let confidence: Double?
    let needsReview: Bool
    let labelSource: String?
    let tempoRatio: Double?
    let referenceTitle: String?
    @DefaultEmpty var characters: [String] = []

    var displayTitle: String { title ?? kind?.capitalized ?? "Section" }
}

struct SectionsResponse: Codable, Sendable {
    let performanceId: String
    let available: Bool
    let pipeline: String?
    @DefaultEmpty var sections: [PerformanceSection] = []

    /// The music section playing at `t` seconds into `trackId`.
    func section(at trackId: String, t: Double, tracks: [Track]) -> PerformanceSection? {
        let order = Dictionary(uniqueKeysWithValues: tracks.map { ($0.id, $0.index) })
        guard let idx = order[trackId] else { return nil }
        return sections.first { s in
            guard s.music, let a = order[s.start.trackId], let b = order[s.end.trackId] else { return false }
            let afterStart = idx > a || (idx == a && t >= (s.start.t ?? 0))
            let beforeEnd = idx < b || (idx == b && t < (s.end.t ?? .infinity))
            return afterStart && beforeEnd
        }
    }
}

struct CanonicalWorkRef: Codable, Hashable, Sendable {
    let id: String
    let title: String
    let composer: OptionalRef?
    let catalogue: String?
    @DefaultEmpty var workIds: [String] = []
    let excerptOf: String?
}

struct OptionalRef: Codable, Hashable, Sendable {
    let id: String?
    let name: String?
}

struct CompareVersion: Codable, Hashable, Sendable, Identifiable {
    struct Lead: Codable, Hashable, Sendable { var person: PersonRef; var role: String? = nil }
    struct RefSummary: Codable, Hashable, Sendable {
        let recordingId: String?
        let name: String?
        let kind: String?
        let alignmentQuality: Double?
    }
    var id: String { performanceId }
    let performanceId: String
    let title: String?
    let date: String?
    let venue: VenueRef?
    let conductor: PersonRef?
    @DefaultEmpty var leadSingers: [Lead] = []
    let durationS: Double?
    let confidence: Double?
    let kind: String?
    let collection: String?
    let artworkUrl: String?
    let workId: String
    let year: Int?
    let sectionsAvailable: Bool
    let method: String?
    let musicS: Double?
    let sectionCount: Int?
    let needsReview: Int?
    let firstTrackId: String?
    let reference: RefSummary?
}

struct RoleDifference: Codable, Hashable, Sendable, Identifiable {
    struct Name: Codable, Hashable, Sendable { let id: String; let name: String; let status: String? }
    var id: String { role }
    let role: String
    let byVersion: [String: [Name]]
    let differs: Bool
    let presentIn: Int
}

struct CompareDifferences: Codable, Hashable, Sendable {
    struct Years: Codable, Hashable, Sendable { let min: Int; let max: Int; let span: Int }
    let conductorDiffers: Bool
    let venueDiffers: Bool
    let years: Years?
    @DefaultEmpty var venues: [String] = []
    @DefaultEmpty var roles: [RoleDifference] = []
}

struct SectionCell: Codable, Hashable, Sendable {
    let sectionId: String
    let durationS: Double
    let start: SectionPoint
    let confidence: Double?
    let needsReview: Bool?
    let tempoRatio: Double?
    let relative: Double?
}

struct SectionRow: Codable, Hashable, Sendable, Identifiable {
    var id: String { key }
    let key: String
    let label: String?
    let group: String?
    let cells: [String: SectionCell]
    let medianS: Double?
    let versions: Int
}

struct SectionTable: Codable, Hashable, Sendable {
    @DefaultEmpty var rows: [SectionRow] = []
    let sharedRows: Int
    let comparable: Bool
}

struct CompareResponse: Codable, Hashable, Sendable {
    let canonical: CanonicalWorkRef
    @DefaultEmpty var versions: [CompareVersion] = []
    let differences: CompareDifferences
    let sections: SectionTable
}

/// Where to go in the other version (GET /api/compare/jump).
struct JumpTarget: Codable, Hashable, Sendable {
    let performanceId: String
    let workId: String?
    let trackId: String
    let trackIndex: Int?
    let t: Double
    let method: String          // alignment | section | proportional | start
    let confidence: Double?
    let section: PerformanceSection?
}

struct CanonicalWorkItem: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let title: String
    let composer: OptionalRef?
    let catalogue: String?
    let versionCount: Int
}

struct CanonicalWorksResponse: Codable, Sendable {
    struct Multi: Codable, Sendable { let pieces: Int; let completeWorks: Int; let excerpts: Int }
    @DefaultEmpty var items: [CanonicalWorkItem] = []
    let total: Int
    let multiVersion: Multi?
}

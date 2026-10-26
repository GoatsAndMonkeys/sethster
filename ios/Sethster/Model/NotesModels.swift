import Foundation

// Phase 3 shapes from docs/API.md (v0.2): transcripts, liner notes, program notes, versions. Decoding is tolerant
// (everything optional / defaulting to empty) and the app hides any section whose endpoint 404s or is empty.

/// A citation: where an external fact came from, with live and archived links.
struct Citation: Codable, Hashable, Sendable {
    var sourceId: String? = nil
    var title: String? = nil
    var url: String? = nil
    var waybackUrl: String? = nil
    var captured: String? = nil
    var license: String? = nil

    var isEmpty: Bool { url == nil && waybackUrl == nil && title == nil }
}

// MARK: - Transcript

struct TranscriptLine: Codable, Hashable, Sendable, Identifiable {
    var trackId: String? = nil
    var trackIndex: Int? = nil
    /// Seconds within the track.
    var start: Double
    var end: Double? = nil
    var text: String
    var language: String? = nil
    var confidence: Double? = nil

    var id: String { "\(trackId ?? String(trackIndex ?? 0))@\(start)" }

    func belongs(to track: Track) -> Bool {
        if let trackId { return trackId == track.id }
        if let trackIndex { return trackIndex == track.index }
        return false
    }
}

struct Transcript: Codable, Sendable {
    var performanceId: String? = nil
    var model: String? = nil
    var language: String? = nil
    @DefaultEmpty var segments: [TranscriptLine] = []
}

// MARK: - Program notes

/// Parsed from the program note's mandatory "Source:" footer.
struct ProgramSource: Codable, Hashable, Sendable {
    var title: String? = nil
    var url: String? = nil
    var waybackUrl: String? = nil
    var captured: String? = nil

    var citation: Citation { Citation(title: title, url: url, waybackUrl: waybackUrl, captured: captured) }
}

struct ProgramNoteRef: Codable, Hashable, Sendable {
    var n: Int? = nil
    var title: String? = nil
    var url: String? = nil
    var source: ProgramSource? = nil
    @DefaultEmpty var cites: [String] = []
}

struct ProgramNote: Codable, Hashable, Sendable {
    var performanceId: String? = nil
    var n: Int? = nil
    var title: String? = nil
    /// The full Markdown file, Source footer included.
    var markdown: String? = nil
    var source: ProgramSource? = nil
}

// MARK: - Liner notes

struct CitedSource: Codable, Hashable, Sendable, Identifiable {
    var id: String
    var kind: String? = nil      // research | wikipedia | wikidata | program | broadcast | record
    var title: String? = nil
    var url: String? = nil
    var waybackUrl: String? = nil
    var type: String? = nil
    var dateSpecific: Bool? = nil
    var excerpt: String? = nil

    var citation: Citation {
        Citation(sourceId: id, title: title, url: url, waybackUrl: waybackUrl,
                 license: kind == "wikipedia" ? "CC BY-SA 4.0" : nil)
    }
}

struct LinerFact: Codable, Hashable, Sendable {
    var label: String? = nil
    var text: String
    @DefaultEmpty var cites: [String] = []
}

struct LinerNotes: Codable, Sendable {
    struct PerformanceSection: Codable, Sendable {
        @DefaultEmpty var facts: [LinerFact] = []
        @DefaultEmpty var castChanges: [LinerFact] = []
    }
    struct Premiere: Codable, Hashable, Sendable { var date: String? = nil; var place: String? = nil }
    struct TimelineItem: Codable, Hashable, Sendable {
        var trackId: String? = nil
        var index: Int? = nil
        var title: String? = nil
        var durationS: Double? = nil
    }
    struct WorkItem: Codable, Hashable, Sendable {
        var workId: String? = nil
        var title: String? = nil
        var composer: Ref? = nil
        var catalogue: String? = nil
        var description: String? = nil
        var synopsis: String? = nil
        var synopsisHeading: String? = nil
        var premiere: Premiere? = nil
        @DefaultEmpty var librettists: [String] = []
        var wikipediaUrl: String? = nil
        @DefaultEmpty var timeline: [TimelineItem] = []
        @DefaultEmpty var cites: [String] = []
    }
    struct Quote: Codable, Hashable, Sendable {
        var trackId: String? = nil
        var trackIndex: Int? = nil
        var start: Double? = nil
        var end: Double? = nil
        var text: String
        var reason: String? = nil
        @DefaultEmpty var cites: [String] = []
    }
    struct Feature: Codable, Hashable, Sendable {
        var trackId: String? = nil
        var trackIndex: Int? = nil
        var title: String? = nil
        var durationS: Double? = nil
        var firstLine: String? = nil
        @DefaultEmpty var cites: [String] = []
    }
    struct Broadcast: Codable, Sendable {
        var hasTranscript: Bool? = nil
        @DefaultEmpty var quotes: [Quote] = []
        @DefaultEmpty var intermissionFeatures: [Feature] = []
    }
    struct Items<T: Codable & Sendable>: Codable, Sendable { @DefaultEmpty var items: [T] = [] }

    var performance: PerformanceSection? = nil
    var programNotes: Items<ProgramNoteRef>? = nil
    var work: Items<WorkItem>? = nil
    var broadcast: Broadcast? = nil
    var sources: Items<CitedSource>? = nil

    func source(_ id: String) -> CitedSource? { sources?.items.first { $0.id == id } }
}

// MARK: - Versions

struct VersionRow: Codable, Hashable, Sendable {
    struct Lead: Codable, Hashable, Sendable { var person: PersonRef; var role: String? = nil }
    var performanceId: String
    var title: String? = nil
    var date: String? = nil
    var dateStatus: EvidenceStatus? = nil
    var venue: VenueRef? = nil
    var conductor: PersonRef? = nil
    @DefaultEmpty var leadSingers: [Lead] = []
    var durationS: Double? = nil
    var confidence: Double? = nil
    var kind: String? = nil
    var collection: String? = nil
    var artworkUrl: String? = nil
    var isCurrent: Bool? = nil
}

struct WorkVersions: Codable, Sendable {
    @DefaultEmpty var items: [VersionRow] = []
    let total: Int?
}

/// One row of the Versions table, from the versions endpoint or (fallback) a PerformanceSummary.
struct WorkVersion: Hashable, Sendable, Identifiable {
    let id: String
    let date: String?
    let venue: String?
    let conductor: String?
    let cast: String
    let durationS: Double?
    let confidence: Double?

    init(_ r: VersionRow) {
        id = r.performanceId; date = r.date; venue = r.venue?.name; conductor = r.conductor?.name
        cast = r.leadSingers.map(\.person.name).joined(separator: ", ")
        durationS = r.durationS; confidence = r.confidence
    }

    init(_ p: PerformanceSummary) {
        id = p.id; date = p.date; venue = p.venue?.name; conductor = nil
        cast = p.subtitle ?? ""; durationS = p.durationS; confidence = p.confirmedRatio
    }
}

extension APIError {
    var isNotFound: Bool { if case .http(404, _) = self { true } else { false } }
}

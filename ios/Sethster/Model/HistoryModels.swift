import Foundation

// Shapes from docs/CALENDAR.md (v0.3): On This Day, calendar shelves, historical context.
// Tolerant decoding: everything optional or defaulting to empty.

struct LinkSource: Codable, Hashable, Sendable {
    var id: String? = nil
    var title: String? = nil
    var url: String? = nil
    var waybackUrl: String? = nil
    var citation: Citation { Citation(sourceId: id, title: title, url: url, waybackUrl: waybackUrl) }
}

struct LooseRef: Codable, Hashable, Sendable {
    var id: String? = nil
    var name: String? = nil
}

// MARK: - On This Day

struct ContextNote: Codable, Hashable, Sendable {
    var topic: String? = nil
    var text: String
    @DefaultEmpty var cites: [String] = []
    @DefaultEmpty var sources: [LinkSource] = []
    var check: String? = nil
    var contextUrl: String? = nil
}

struct CastLine: Codable, Hashable, Sendable {
    var person: PersonRef
    var role: String? = nil
}

struct PlayRef: Codable, Hashable, Sendable {
    var id: String
    var index: Int? = nil
}

struct OnThisDayCard: Codable, Hashable, Sendable, Identifiable {
    var performance: PerformanceSummary
    var date: String
    var year: Int
    var yearsAgo: Int? = nil
    var offsetDays: Int? = nil
    var dateStatus: String? = nil
    var headline: String? = nil
    @DefaultEmpty var cast: [CastLine] = []
    var conductor: PersonRef? = nil
    var venue: VenueRef? = nil
    var play: PlayRef? = nil
    var context: ContextNote? = nil
    var id: String { performance.id }
}

struct PremiereEvent: Codable, Hashable, Sendable, Identifiable {
    var workId: String
    var title: String? = nil
    var composer: LooseRef? = nil
    var date: String
    var yearsAgo: Int? = nil
    var place: String? = nil
    var performanceCount: Int? = nil
    var source: LinkSource? = nil
    var id: String { workId }
}

struct LifeEvent: Codable, Hashable, Sendable, Identifiable {
    var kind: String            // composer | performer
    var id: String
    var name: String
    var imageUrl: String? = nil
    var date: String
    var years: Int? = nil
    var alive: Bool? = nil
    var performanceCount: Int? = nil
    var source: LinkSource? = nil
}

struct OnThisDayResponse: Codable, Sendable {
    var date: String
    var label: String? = nil
    var mode: String            // day | week | none
    @DefaultEmpty var items: [OnThisDayCard] = []
    @DefaultEmpty var premieres: [PremiereEvent] = []
    @DefaultEmpty var birthdays: [LifeEvent] = []
    @DefaultEmpty var deathdays: [LifeEvent] = []
}

// MARK: - Calendar shelves

struct ShelfEntity: Codable, Hashable, Sendable {
    var kind: String? = nil
    var id: String? = nil
    var name: String? = nil
    var imageUrl: String? = nil
}

struct CalendarShelf: Codable, Hashable, Sendable, Identifiable {
    var id: String
    var kind: String            // in_memoriam | composer_month | ring_season | anniversary
    var title: String
    var subtitle: String? = nil
    var status: String? = nil
    var stationId: String? = nil
    var entity: ShelfEntity? = nil
    @DefaultEmpty var sources: [LinkSource] = []
    @DefaultEmpty var items: [PerformanceSummary] = []
}

struct CalendarResponse: Codable, Sendable {
    var date: String
    @DefaultEmpty var shelves: [CalendarShelf] = []
}

// MARK: - Historical context (GET /performances/{id}/context)

struct CtxItems<T: Codable & Sendable>: Codable, Sendable {
    var title: String? = nil
    @DefaultEmpty var items: [T] = []
}

struct CtxFact: Codable, Hashable, Sendable {
    var label: String? = nil
    var text: String
    @DefaultEmpty var cites: [String] = []
    var status: String? = nil
    var personId: String? = nil
}

struct CtxParagraph: Codable, Hashable, Sendable {
    var topic: String? = nil
    var title: String? = nil
    var text: String
    @DefaultEmpty var cites: [String] = []
    var status: String? = nil
    var check: String? = nil
    var backend: String? = nil
}

struct CtxPiece: Codable, Hashable, Sendable {
    var workId: String
    var title: String? = nil
    var composer: LooseRef? = nil
    @DefaultEmpty var facts: [CtxFact] = []
    @DefaultEmpty var paragraphs: [CtxParagraph] = []
}

struct CtxSameSeason: Codable, Hashable, Sendable {
    var performanceId: String
    var title: String? = nil
    var date: String? = nil
}

struct CtxRecording: Codable, Sendable {
    var title: String? = nil
    var season: String? = nil
    @DefaultEmpty var facts: [CtxFact] = []
    @DefaultEmpty var paragraphs: [CtxParagraph] = []
    @DefaultEmpty var sameSeason: [CtxSameSeason] = []
}

struct CtxPerformer: Codable, Hashable, Sendable {
    var person: PersonRef
    var role: String? = nil
    var stage: String? = nil    // early | peak | late
    var age: Int? = nil
    var careerStart: Int? = nil
    var careerEnd: Int? = nil
    var estimated: Bool? = nil
    var text: String? = nil
    @DefaultEmpty var cites: [String] = []
}

struct CtxEvent: Codable, Hashable, Sendable {
    var kind: String            // composed | premiere | version | recording
    var date: String
    var label: String? = nil
    var performanceId: String? = nil
    var isCurrent: Bool? = nil
    @DefaultEmpty var cites: [String] = []
}

struct CtxTimeline: Codable, Hashable, Sendable {
    var workId: String
    var title: String? = nil
    @DefaultEmpty var events: [CtxEvent] = []
}

struct HistoricalContext: Codable, Sendable {
    var performanceId: String
    var title: String? = nil
    var piece: CtxItems<CtxPiece>? = nil
    var recording: CtxRecording? = nil
    var performers: CtxItems<CtxPerformer>? = nil
    var timeline: CtxItems<CtxTimeline>? = nil
    var sources: CtxItems<CitedSource>? = nil

    var isEmpty: Bool {
        (piece?.items.isEmpty ?? true) && (recording?.facts.isEmpty ?? true) && (performers?.items.isEmpty ?? true)
    }
}

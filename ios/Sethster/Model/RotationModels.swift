import Foundation

/// A performance in the listener's ordered Rotation, enriched with the original-file download manifest.
struct RotationItem: Codable, Hashable, Sendable, Identifiable {
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
    let addedAt: String
    let download: RotationDownload

    var asSummary: PerformanceSummary {
        PerformanceSummary(id: id, title: title, subtitle: subtitle, composers: composers, date: date,
                           dateStatus: dateStatus, venue: venue, kind: kind, collection: collection,
                           durationS: durationS, artworkUrl: artworkUrl, confirmedRatio: confirmedRatio)
    }
}

struct RotationDownload: Codable, Hashable, Sendable {
    @DefaultEmpty var tracks: [RotationTrackDownload] = []
}

struct RotationTrackDownload: Codable, Hashable, Sendable, Identifiable {
    let id: String
    let url: String
    let bytes: Int64?
    let format: String?
}

struct RotationResponse: Codable, Sendable {
    let listener: String
    @DefaultEmpty var items: [RotationItem] = []
}

struct RotationAddBody: Codable, Sendable {
    let performanceId: String
    let listener: String
}

struct RotationOrderBody: Codable, Sendable {
    let performanceIds: [String]
    let listener: String
}

enum RotationDownloadState: Equatable, Sendable {
    case waiting
    case downloading(Double)
    case downloaded
    case failed

    var progress: Double? {
        if case .downloading(let value) = self { value } else { nil }
    }
}

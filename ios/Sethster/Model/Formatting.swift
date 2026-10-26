import Foundation

enum Fmt {
    private static let monthNames: [String] = DateFormatter().monthSymbols ?? []

    /// "2016-04-09" → "9 April 2016"; "2013-01" → "January 2013"; "1976" → "1976".
    static func date(_ iso: String?) -> String {
        guard let iso, !iso.isEmpty else { return "Date unknown" }
        let parts = iso.prefix(10).split(separator: "-").map(String.init)
        guard let year = parts.first else { return iso }
        if parts.count >= 2, let m = Int(parts[1]), (1...12).contains(m) {
            let month = monthNames.indices.contains(m - 1) ? monthNames[m - 1] : parts[1]
            if parts.count >= 3, let d = Int(parts[2]) { return "\(d) \(month) \(year)" }
            return "\(month) \(year)"
        }
        return year
    }

    /// Short form for cards: "9 Apr 2016".
    static func shortDate(_ iso: String?) -> String {
        let long = date(iso)
        let parts = long.split(separator: " ")
        if parts.count == 3 { return "\(parts[0]) \(parts[1].prefix(3)) \(parts[2])" }
        return long
    }

    static func year(_ iso: String?) -> String? {
        guard let iso, iso.count >= 4 else { return nil }
        return String(iso.prefix(4))
    }

    /// 12174 → "3 h 23 min"; 838 → "14 min".
    static func longDuration(_ seconds: Double?) -> String? {
        guard let s = seconds, s > 0 else { return nil }
        let mins = Int((s / 60).rounded())
        if mins >= 60 { return "\(mins / 60) h \(mins % 60) min" }
        return "\(max(mins, 1)) min"
    }

    /// 838 → "13:58"; 4000 → "1:06:40".
    static func clock(_ seconds: Double?) -> String {
        guard let s = seconds, s.isFinite, s >= 0 else { return "–:––" }
        let t = Int(s.rounded(.down))
        let h = t / 3600, m = (t % 3600) / 60, sec = t % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }

    static func kind(_ kind: String?) -> String? {
        switch kind {
        case "broadcast": "Broadcast"
        case "commercial_release": "Commercial release"
        case "live_unofficial_release": "Unofficial live release"
        case "private_recording": "Private recording"
        case "compilation": "Compilation"
        default: nil
        }
    }

    static func initials(_ name: String) -> String {
        let words = name.split(separator: " ").filter { $0.first?.isLetter == true }
        let chars = [words.first, words.count > 1 ? words.last : nil].compactMap { $0?.first }
        return String(chars).uppercased()
    }
}

extension Track {
    /// Research note with timing parentheticals like "(18:46; inferred)" removed.
    private var cleaned: String {
        (description ?? title)
            .replacingOccurrences(of: #"\s*\(\d{1,2}:\d{2}(:\d{2})?[^)]*\)"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// First clause of the note: "Act I, scene 1". Falls back to "Track N".
    var shortTitle: String {
        let c = cleaned
        let head = sentenceBreak(c).map { String(c[..<$0.lowerBound]) } ?? c
        let t = head.trimmingCharacters(in: CharacterSet(charactersIn: " .;,"))
        return t.isEmpty ? "Track \(index)" : t
    }

    /// First ". " that ends a real sentence, skipping movement numerals ("I. Allegro") and abbreviations ("No. 6").
    private func sentenceBreak(_ c: String) -> Range<String.Index>? {
        var from = c.startIndex
        while let r = c.range(of: ". ", range: from..<c.endIndex) {
            let word = c[..<r.lowerBound].split(separator: " ").last.map(String.init) ?? ""
            let isNumeral = word.allSatisfy { "IVXLCivxlc".contains($0) } || word.allSatisfy(\.isNumber)
            let isAbbrev = ["No", "Op", "op", "St", "Mr", "Mrs", "Dr", "ca", "c", "vs", "arr", "BWV", "K"].contains(word)
            if !word.isEmpty && !isNumeral && !isAbbrev { return r }
            from = r.upperBound
        }
        return nil
    }

    /// Whatever the research note says beyond the short title, if anything.
    var detailNote: String? {
        let c = cleaned
        guard let r = sentenceBreak(c) else { return nil }
        let rest = c[r.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        return rest.count > 3 ? rest : nil
    }

    var segmentIcon: String {
        switch segment {
        case .music: "music.note"
        case .announcer: "mic"
        case .intermission: "cup.and.saucer"
        case .applause: "hands.clap"
        case .interview: "bubble.left.and.bubble.right"
        case .other: "waveform"
        }
    }
}

extension EvidenceStatus {
    var label: String {
        switch self {
        case .confirmed: "Confirmed"
        case .productionOnly: "Production cast"
        case .inferred: "Inferred"
        case .conflict: "Conflict"
        case .unknown: "Unknown"
        }
    }

    var symbol: String {
        switch self {
        case .confirmed: "checkmark.seal.fill"
        case .productionOnly: "theatermasks"
        case .inferred: "questionmark.circle"
        case .conflict: "exclamationmark.triangle.fill"
        case .unknown: "circle.dashed"
        }
    }

    var explanation: String {
        switch self {
        case .confirmed: "A date-specific source names this person in this role for this performance."
        case .productionOnly: "Credited for the production or the season, but no source read confirms this exact date."
        case .inferred: "Deduced from indirect evidence (timings, season listings, the recording itself)."
        case .conflict: "Sources disagree. See the notes and sources below."
        case .unknown: "Evidence status not recorded."
        }
    }
}

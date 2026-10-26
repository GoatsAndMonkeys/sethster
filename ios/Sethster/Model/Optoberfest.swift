import Foundation
import Observation

/// Optoberfest: an October recommendation shelf built on the device (the server has no themes API yet,
/// see docs/plans/themed-programming.md). Works come from the bundled `optoberfest.json`
/// (data/themes/supernatural_tags.jsonl). This is NOT the full Phase 0 design: no radio scheduling,
/// spacing, Verdi layering or server assignments. It only picks, per day, one work-recording plus a shelf.
struct ThemeTag: Codable, Sendable {
    let workId: String
    let value: String      // yes | partial
    let hint: String
    /// One sentence on why the work belongs in Optoberfest.
    let reason: String?
    /// Performance id → a note on that production (e.g. a staging that makes the work spookier).
    let performanceNotes: [String: String]?

    // Explicit keys rather than .convertFromSnakeCase, which would also rewrite the performance-id keys.
    private enum CodingKeys: String, CodingKey {
        case workId = "work_id", value, hint, reason, performanceNotes = "performance_notes"
    }
}

struct ThemePick: Identifiable, Hashable, Sendable {
    let workId: String
    let workTitle: String
    let composer: String?
    let performance: PerformanceSummary
    let hint: String?
    let reason: String?
    /// The tag's note on this particular recording, if it has one.
    let productionNote: String?
    var id: String { performance.id + "|" + workId }

    /// "from <performance title>" for the Home card, or nil when the title would mislead: it is the work itself,
    /// or it names another composer (a recital titled "Tosti: Four Songs" holding the picked aria).
    var fromLabel: String? {
        let label = performance.title
        guard label != workTitle, !label.isEmpty else { return nil }
        let surname = composer?.split(separator: " ").last.map(String.init)
        if let colon = label.firstIndex(of: ":") {
            let lead = label[..<colon].trimmingCharacters(in: .whitespaces)
            guard let composer, composer.localizedCaseInsensitiveContains(lead)
                    || (surname.map { lead.localizedCaseInsensitiveContains($0) } ?? false) else { return nil }
        } else if performance.composers.count > 1,
                  !(surname.map { label.localizedCaseInsensitiveContains($0) } ?? false) {
            return nil
        }
        return "from \(label)"
    }
}

@MainActor @Observable
final class OptoberfestStore {
    static let shared = OptoberfestStore()

    private(set) var today: ThemePick?
    private(set) var more: [ThemePick] = []
    private(set) var loading = false
    private var loadedKey: String?
    private var loadingKey: String?
    private var loadID: UUID?
    private var loadTask: Task<[ThemePick], Never>?

    /// Recordings the themed-programming plan quarantines (§3.4).
    nonisolated static let quarantined: Set<String> = [
        "cso-0603-haitink", "cso-0606-beethoven-9", "bbcconcert-121117-missa", "phila-040115-sawallisch",
        "vienna-401105-missa", "bso-221029-uchida", "bso-230211-bso", "bbcconcert-050419-sq", "phila-021215-messiah",
    ]

    static let tags: [ThemeTag] = {
        guard let url = Bundle.main.url(forResource: "optoberfest", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([ThemeTag].self, from: data)) ?? []
    }()

    static var calendar: Calendar {
        var c = Calendar(identifier: .gregorian); c.timeZone = .current; return c
    }

    /// Today, or the `-themeDate YYYY-MM-DD` launch argument (testing / screenshots).
    static var themeDate: Date {
        if let s = UserDefaults.standard.string(forKey: "themeDate") {
            let p = s.split(separator: "-").compactMap { Int($0) }
            if p.count == 3, let d = calendar.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12)) { return d }
        }
        return .now
    }

    static func isActive(_ date: Date = themeDate) -> Bool { calendar.component(.month, from: date) == 10 }
    static func isHalloween(_ date: Date = themeDate) -> Bool {
        let c = calendar.dateComponents([.month, .day], from: date); return c.month == 10 && c.day == 31
    }
    static var headline: String { isHalloween() ? "Happy Halloween" : "Optoberfest" }

    static func dayKey(_ date: Date) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// Deterministic weighted order for a day (Efraimidis–Spirakis keys with a stable FNV-1a hash).
    /// Weights follow plan §3.5: partial fades, yes builds toward Halloween; Oct 31 is yes-only.
    static func ranked(for date: Date) -> [ThemeTag] {
        let day = Double(calendar.component(.day, from: date))
        let halloween = isHalloween(date)
        let key = dayKey(date)
        let pool = tags.filter { !halloween || $0.value == "yes" }
        func weight(_ t: ThemeTag) -> Double {
            t.value == "yes" ? 0.6 + day / 31 * 0.8 : max(0.05, 1.0 - day / 31 * 0.7)
        }
        func unit(_ s: String) -> Double {
            var h: UInt64 = 0xcbf29ce484222325
            for b in s.utf8 { h ^= UInt64(b); h = h &* 0x100000001b3 }
            return (Double(h >> 11) + 0.5) / Double(1 << 53)
        }
        return pool
            .map { (t: $0, k: -log(unit(key + "|" + $0.workId)) / weight($0)) }
            .sorted { $0.k != $1.k ? $0.k < $1.k : $0.t.workId < $1.t.workId }
            .map(\.t)
    }

    /// Loads today's pick and shelf once per server and day (cheap to call on every appearance).
    func load(api: any SethsterAPI, force: Bool = false) async {
        let date = Self.themeDate
        let key = (api.resolve("/")?.absoluteString ?? "demo") + "|" + Self.dayKey(date)
        guard Self.isActive(date), !api.isDemo else {
            loadTask?.cancel()
            loadTask = nil
            loadID = nil
            loadingKey = nil
            loading = false
            today = nil; more = []; loadedKey = key; return
        }
        if !force, key == loadedKey { return }

        let task: Task<[ThemePick], Never>
        let id: UUID
        if !force, key == loadingKey, let currentTask = loadTask, let currentID = loadID {
            // Home and CarPlay commonly arrive together. Both must wait for the shared result.
            task = currentTask
            id = currentID
        } else {
            loadTask?.cancel()
            id = UUID()
            let tagsByWork = Dictionary(Self.tags.map { ($0.workId, $0) }, uniquingKeysWith: { a, _ in a })
            let candidates = Array(Self.ranked(for: date).prefix(24))
            task = Task { await Self.resolve(candidates, tags: tagsByWork, api: api) }
            loadTask = task
            loadID = id
            loadingKey = key
            loading = true
        }

        let resolved = await task.value
        // A server/day/forced request that started later owns the state, even if this request finishes last.
        guard loadID == id, loadingKey == key else { return }
        var seen = Set<String>()
        let picks = resolved.filter { seen.insert($0.performance.id).inserted }
        today = picks.first
        more = Array(picks.dropFirst().prefix(10))
        loadedKey = picks.isEmpty ? nil : key   // empty (e.g. outage): retry on next appearance
        loadTask = nil
        loadID = nil
        loadingKey = nil
        loading = false
    }

    nonisolated private static func resolve(_ candidates: [ThemeTag], tags: [String: ThemeTag],
                                            api: any SethsterAPI) async -> [ThemePick] {
        await withTaskGroup(of: (Int, ThemePick?).self) { group in
            for (i, tag) in candidates.enumerated() {
                group.addTask { (i, await resolve(tags[tag.workId] ?? tag, api: api)) }
            }
            var out = [(Int, ThemePick?)]()
            for await result in group { out.append(result) }
            return out.sorted { $0.0 < $1.0 }.compactMap(\.1)
        }
    }

    /// The best eligible recording of a work: confirmed_ratio >= 0.5, not quarantined, with tracks mapped to it.
    /// Recordings the tag has a production note for are tried first, then the rest by confirmed ratio.
    nonisolated private static func resolve(_ tag: ThemeTag, api: any SethsterAPI) async -> ThemePick? {
        guard let work = try? await api.work(id: tag.workId) else { return nil }
        let notes = (tag.performanceNotes ?? [:]).filter { !$0.value.isEmpty }
        let eligible = work.performances
            .filter { ($0.confirmedRatio ?? 0) >= 0.5 && !quarantined.contains($0.id) }
            .sorted { ($0.confirmedRatio ?? 0) != ($1.confirmedRatio ?? 0) ? ($0.confirmedRatio ?? 0) > ($1.confirmedRatio ?? 0) : $0.id < $1.id }
        let noted = eligible.filter { notes[$0.id] != nil }
        let perfs = noted + eligible.filter { notes[$0.id] == nil }.prefix(3)
        for p in perfs {
            guard let d = try? await api.performance(id: p.id),
                  d.tracks.contains(where: { $0.workId == tag.workId }) else { continue }
            return ThemePick(workId: tag.workId, workTitle: work.title, composer: work.composer?.name,
                             performance: p, hint: tag.hint.isEmpty ? nil : tag.hint,
                             reason: tag.reason?.isEmpty == false ? tag.reason : nil,
                             productionNote: notes[p.id])
        }
        return nil
    }

    /// Plays only the tracks of the picked work (falls back to the whole performance if none are mapped).
    /// Nothing plays if the account changed (sign-out, server switch) while the performance was fetched.
    static func play(_ pick: ThemePick, api: any SethsterAPI, player: PlayerController) async throws {
        let epoch = AppServices.account.epoch
        var detail = try await api.performance(id: pick.performance.id)
        guard epoch == AppServices.account.epoch else { return }
        let subset = detail.tracks.filter { $0.workId == pick.workId }
        if !subset.isEmpty { detail.tracks = subset }
        player.play(detail, api: api)
    }
}

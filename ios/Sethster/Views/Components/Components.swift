import SafariServices
import SwiftUI

// MARK: - Navigation

enum Route: Hashable {
    case performance(String)
    case composer(String)
    case work(String)
    case person(String)
    case venue(String)
    case collection(String)
    /// Every performance in a city (from the Home Spotlight).
    case city(String)
    case linerNotes(String)
    case programNote(String, Int)
    case composers, people, venues, collections, allPerformances
}

struct RouteDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content.navigationDestination(for: Route.self) { route in
            switch route {
            case .performance(let id): PerformanceView(id: id)
            case .composer(let id): ComposerView(id: id)
            case .work(let id): WorkView(id: id)
            case .person(let id): PersonView(id: id)
            case .venue(let id): VenueView(id: id)
            case .collection(let name): CollectionView(name: name)
            case .city(let name):
                FilteredPerformances(title: name, query: PerformanceQuery(sort: "-date", limit: 500, city: name))
            case .linerNotes(let id): LinerNotesView(id: id)
            case .programNote(let id, let n): ProgramNoteView(performanceId: id, n: n)
            case .composers: ComposersListView()
            case .people: PeopleListView()
            case .venues: VenuesListView()
            case .collections: CollectionsListView()
            case .allPerformances: AllPerformancesView()
            }
        }
    }
}

extension View {
    func withRoutes() -> some View { modifier(RouteDestinations()) }
}

// MARK: - Loading

enum LoadState<T> {
    case loading
    case loaded(T)
    case failed(String)
}

/// Loads `T` when it appears (and whenever the data source changes), showing progress / errors.
struct Loader<T: Sendable, Content: View>: View {
    @Environment(AppModel.self) private var model
    let load: (any SethsterAPI) async throws -> T
    @ViewBuilder let content: (T) -> Content
    @State private var state: LoadState<T> = .loading

    var body: some View {
        Group {
            switch state {
            case .loading:
                ProgressView().controlSize(.large).frame(maxWidth: .infinity, maxHeight: .infinity)
            case .failed(let msg):
                ContentUnavailableView {
                    Label("Couldn't load", systemImage: "wifi.exclamationmark")
                } description: {
                    Text(msg)
                } actions: {
                    Button("Try Again") { Task { await run() } }.buttonStyle(.bordered)
                    if !model.api.isDemo {
                        Button("Browse Demo Data") { model.useDemoData = true }.font(.uiFootnote)
                    }
                }
            case .loaded(let value):
                content(value)
            }
        }
        .task(id: model.generation) { await run() }
    }

    private func run() async {
        if case .loaded = state {} else { state = .loading }
        do { state = .loaded(try await load(model.api)) }
        catch is CancellationError {}
        catch let e as URLError where e.code == .cancelled {}
        catch { state = .failed(error.localizedDescription) }
    }
}

// MARK: - Rows & cards

struct PerformanceRow: View {
    let p: PerformanceSummary
    var showComposer = true

    var body: some View {
        HStack(spacing: 12) {
            ArtworkView(path: p.artworkUrl, title: p.title, seed: p.collection ?? p.id, cornerRadius: 6)
                .frame(width: 56, height: 56)
            VStack(alignment: .leading, spacing: 2) {
                Text(p.title).font(.display(17, weight: .medium)).lineLimit(2)
                if let sub = p.subtitle, !sub.isEmpty {
                    Text(sub).font(.uiSubheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Text(meta).font(.uiCaption).foregroundStyle(.tertiary).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
    }

    var meta: String {
        var parts: [String] = []
        if showComposer, let c = p.composers.first { parts.append(c.name.split(separator: " ").last.map(String.init) ?? c.name) }
        parts.append(Fmt.shortDate(p.date))
        if let v = p.venue?.name { parts.append(v) }
        return parts.joined(separator: " · ")
    }
}

struct PerformanceCard: View {
    let p: PerformanceSummary
    var width: CGFloat = 164

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ArtworkView(path: p.artworkUrl, title: p.title, seed: p.collection ?? p.id)
                .frame(width: width, height: width)
                .shadow(color: .black.opacity(0.12), radius: 8, y: 4)
            Text(p.title).font(.display(16, weight: .medium)).lineLimit(1)
            Text([p.subtitle ?? p.composers.first?.name, Fmt.year(p.date)].compactMap { $0 }.joined(separator: " · "))
                .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(width: width, alignment: .leading)
    }
}

// MARK: - In-app browser

struct WebLink: Identifiable {
    let url: URL
    var id: URL { url }
}

struct SafariView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> SFSafariViewController {
        let vc = SFSafariViewController(url: url)
        vc.preferredControlTintColor = UIColor(named: "AccentColor")
        return vc
    }
    func updateUIViewController(_ vc: SFSafariViewController, context: Context) {}
}

extension View {
    func inAppBrowser(_ link: Binding<WebLink?>) -> some View {
        sheet(item: link) { SafariView(url: $0.url).ignoresSafeArea() }
    }
}

/// Expandable paragraph for long research prose.
struct ExpandableText: View {
    let text: String
    var lineLimit = 4
    var font: Font = .body
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text).font(font).lineLimit(expanded ? nil : lineLimit)
                .fixedSize(horizontal: false, vertical: true)
            if text.count > lineLimit * 45 {
                Button(expanded ? "Less" : "More") { withAnimation(.snappy) { expanded.toggle() } }
                    .font(.uiFootnote.weight(.semibold))
                    .buttonStyle(.borderless)
            }
        }
    }
}

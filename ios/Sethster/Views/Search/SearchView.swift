import SwiftUI

struct SearchView: View {
    @Environment(AppModel.self) private var model
    @State private var query = ""
    @State private var results: SearchResponse?
    @State private var error: String?
    @State private var searching = false

    var body: some View {
        List {
            if let r = results {
                if !r.performances.isEmpty {
                    Section("Performances") {
                        ForEach(r.performances) { p in NavigationLink(value: Route.performance(p.id)) { PerformanceRow(p: p) } }
                    }
                }
                if !r.composers.isEmpty {
                    Section("Composers") {
                        ForEach(r.composers) { c in
                            NavigationLink(value: Route.composer(c.id)) {
                                HStack(spacing: 12) {
                                    Portrait(path: c.imageUrl, name: c.name, size: 40)
                                    Text(c.name).font(.display(17, weight: .medium))
                                }
                            }
                        }
                    }
                }
                if !r.works.isEmpty {
                    Section("Works") {
                        ForEach(r.works) { w in
                            NavigationLink(value: Route.work(w.id)) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(w.title).font(.display(17, weight: .medium))
                                    if let c = w.composer { Text(c.name).font(.uiCaption).foregroundStyle(.secondary) }
                                }
                            }
                        }
                    }
                }
                if !r.people.isEmpty {
                    Section("Performers") {
                        ForEach(r.people) { p in
                            NavigationLink(value: Route.person(p.id)) {
                                HStack(spacing: 12) {
                                    Portrait(path: p.imageUrl, name: p.name, size: 40)
                                    Text(p.name)
                                }
                            }
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .overlay {
            if let error {
                ContentUnavailableView("Search failed", systemImage: "exclamationmark.magnifyingglass", description: Text(error))
            } else if query.isEmpty {
                ContentUnavailableView("Search the collection", systemImage: "magnifyingglass",
                                       description: Text("Operas, symphonies, singers, conductors, orchestras, venues."))
            } else if let r = results, r.isEmpty, !searching {
                ContentUnavailableView.search(text: query)
            }
        }
        .searchable(text: $query, prompt: "Works, performers, composers")
        .task(id: "\(query)|\(model.generation)") {
            let q = query.trimmingCharacters(in: .whitespaces)
            guard q.count >= 2 else { results = nil; error = nil; return }
            searching = true
            defer { searching = false }
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            do { results = try await model.api.search(q: q); error = nil }
            catch is CancellationError {}
            catch let e as URLError where e.code == .cancelled {}
            catch { self.error = error.localizedDescription }
        }
        .navigationTitle("Search")
    }
}

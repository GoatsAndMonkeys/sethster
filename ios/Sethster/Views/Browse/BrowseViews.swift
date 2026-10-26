import SwiftUI

struct BrowseView: View {
    @Environment(AppModel.self) private var model
    @Environment(AccountStore.self) private var account
    @State private var showSettings = false

    var body: some View {
        List {
            Section {
                row("Composers", "music.quarternote.3", .composers)
                row("Performers", "person.2", .people)
                row("Venues", "building.columns", .venues)
                row("All Performances", "list.bullet.rectangle", .allPerformances)
            }
            // Server address without hunting for the Home gear.
            Section {
                Button { showSettings = true } label: {
                    Label {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Server & Settings").font(.display(19)).foregroundStyle(.primary)
                            Text(model.api.isDemo ? "Demo data" : (model.api.resolve("/")?.host() ?? ""))
                                .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    } icon: { Image(systemName: "gearshape").foregroundStyle(.tint) }
                    .padding(.vertical, 4)
                }
                .badge(account.pendingApprovals)
            }
        }
        .navigationTitle("Browse")
        .sheet(isPresented: $showSettings) { SettingsView() }
    }

    private func row(_ title: String, _ icon: String, _ route: Route) -> some View {
        NavigationLink(value: route) {
            Label { Text(title).font(.display(19)) } icon: { Image(systemName: icon).foregroundStyle(.tint) }
                .padding(.vertical, 6)
        }
    }
}

struct ComposersListView: View {
    @State private var filter = ""
    var body: some View {
        Loader(load: { try await $0.composers() }) { items in
            let shown = filter.isEmpty ? items : items.filter { $0.name.localizedCaseInsensitiveContains(filter) }
            List(shown.sorted { lastName($0.name) < lastName($1.name) }) { c in
                NavigationLink(value: Route.composer(c.id)) {
                    HStack(spacing: 12) {
                        Portrait(path: c.imageUrl, name: c.name, size: 48)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(c.name).font(.display(18, weight: .medium))
                            Text([c.years?.value, c.performanceCount.map { "\($0) performances" }].compactMap { $0 }
                                .joined(separator: " · ")).font(.uiCaption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .listStyle(.plain)
            .searchable(text: $filter, prompt: "Filter composers")
        }
        .navigationTitle("Composers")
    }

    private func lastName(_ n: String) -> String { n.split(separator: " ").last.map(String.init) ?? n }
}

struct PeopleListView: View {
    @State private var filter = ""
    @State private var kind = "all"

    var body: some View {
        Loader(load: { try await $0.people(q: nil) }) { items in
            let shown = items.filter {
                (filter.isEmpty || $0.name.localizedCaseInsensitiveContains(filter))
                    && (kind == "all" || ($0.kind ?? "person") == kind)
            }
            List {
                Picker("Kind", selection: $kind) {
                    Text("All").tag("all"); Text("People").tag("person"); Text("Ensembles").tag("ensemble")
                }
                .pickerStyle(.segmented)
                .listRowSeparator(.hidden)
                ForEach(shown) { p in
                    NavigationLink(value: Route.person(p.id)) {
                        HStack(spacing: 12) {
                            Portrait(path: p.imageUrl, name: p.name, size: 44)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.name).font(.uiBody.weight(.medium))
                                if let n = p.creditCount {
                                    Text("\(n) \(n == 1 ? "credit" : "credits")").font(.uiCaption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
            .listStyle(.plain)
            .searchable(text: $filter, prompt: "Filter performers")
        }
        .navigationTitle("Performers")
    }
}

struct VenuesListView: View {
    var body: some View {
        Loader(load: { try await $0.venues() }) { items in
            List(items) { v in
                NavigationLink(value: Route.venue(v.id)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(v.name).font(.display(17, weight: .medium))
                        Text([v.city, "\(v.count) \(v.count == 1 ? "performance" : "performances")"].compactMap { $0 }
                            .joined(separator: " · ")).font(.uiCaption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)
                }
            }
            .listStyle(.plain)
        }
        .navigationTitle("Venues")
    }
}

struct CollectionsListView: View {
    var body: some View {
        Loader(load: { try await $0.home().collections }) { items in
            ScrollView {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                    ForEach(items) { c in
                        NavigationLink(value: Route.collection(c.name)) { CollectionTile(c: c) }.buttonStyle(.plain)
                    }
                }
                .padding()
            }
        }
        .navigationTitle("Collections")
    }
}

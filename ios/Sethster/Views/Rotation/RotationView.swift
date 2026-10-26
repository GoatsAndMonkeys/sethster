import SwiftUI

struct RotationView: View {
    @Environment(AppModel.self) private var model
    @Environment(PlayerController.self) private var player
    @Environment(RotationStore.self) private var rotation

    var body: some View {
        List {
            if !rotation.items.isEmpty {
                Section {
                    ForEach(rotation.items) { item in row(item) }
                        .onMove(perform: rotation.move)
                        .onDelete { offsets in
                            let ids = offsets.map { rotation.items[$0].id }
                            for id in ids { Task { await rotation.remove(id) } }
                        }
                } footer: {
                    Text("\(rotation.items.count) in rotation · \(ByteCountFormatter.string(fromByteCount: rotation.totalStorageBytes, countStyle: .file)) downloaded")
                }
            }
        }
        .overlay {
            if rotation.items.isEmpty && !rotation.isLoading {
                ContentUnavailableView("Nothing in Rotation", systemImage: "arrow.trianglehead.2.clockwise.rotate.90",
                                       description: Text("Add a performance to keep its original tracks ready offline."))
            } else if rotation.isLoading && rotation.items.isEmpty {
                ProgressView().controlSize(.large)
            }
        }
        .navigationTitle("In Rotation")
        .toolbar { EditButton() }
        .refreshable { await rotation.refresh() }
        .task(id: model.generation) { await rotation.refresh() }
        .alert("Rotation", isPresented: Binding(get: { rotation.errorMessage != nil },
                                                 set: { if !$0 { rotation.clearError() } })) {
            Button("OK") { rotation.clearError() }
        } message: {
            Text(rotation.errorMessage ?? "")
        }
    }

    private func row(_ item: RotationItem) -> some View {
        HStack(spacing: 12) {
            NavigationLink(value: Route.performance(item.id)) {
                ArtworkView(path: item.artworkUrl, title: item.title, seed: item.collection ?? item.id,
                            cornerRadius: 6)
                    .frame(width: 56, height: 56)
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title).font(.display(17, weight: .medium)).lineLimit(2)
                    Text([item.subtitle ?? item.composers.first?.name, Fmt.shortDate(item.date)]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.uiCaption).foregroundStyle(.secondary).lineLimit(1)
                    downloadLabel(item)
                }
            }
            .buttonStyle(.plain)
            Button { play(item) } label: {
                Image(systemName: player.performance?.id == item.id && player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 30)).foregroundStyle(Color.ember)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Play \(item.title)")
        }
        .padding(.vertical, 3)
    }

    @ViewBuilder private func downloadLabel(_ item: RotationItem) -> some View {
        switch rotation.downloadState(for: item.id) {
        case .downloaded:
            Label("Downloaded", systemImage: "checkmark.circle.fill")
                .font(.uiCaption2).foregroundStyle(.green)
        case .downloading(let progress):
            HStack(spacing: 6) {
                ProgressView(value: progress).frame(width: 70)
                Text("\(Int(progress * 100))%")
            }
            .font(.uiCaption2).foregroundStyle(.secondary)
        case .failed:
            Button("Retry download") { rotation.retry(item.id) }
                .font(.uiCaption2).buttonStyle(.borderless)
        case .waiting:
            Text(model.api.isDemo ? "Demo audio unavailable" : "Waiting to download")
                .font(.uiCaption2).foregroundStyle(.secondary)
        }
    }

    private func play(_ item: RotationItem) {
        if player.performance?.id == item.id { player.togglePlayPause(); return }
        let api = model.api, epoch = AppServices.account.epoch
        Task {
            if let cached = rotation.cachedPerformance(item.id) {
                player.play(cached, api: api)
            } else if let detail = try? await api.performance(id: item.id), epoch == AppServices.account.epoch {
                player.play(detail, api: api)
            }
        }
    }
}

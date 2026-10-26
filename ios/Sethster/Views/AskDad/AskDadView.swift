import SwiftUI

/// Open research questions only a person who was there (Dad) can answer.
struct AskDadView: View {
    @Environment(AppModel.self) private var model
    @State private var questions: [Question] = []
    @State private var loaded = false
    @State private var error: String?
    @State private var showAnswered = false
    @State private var editing: Question?

    var body: some View {
        let open = questions.filter { !$0.isAnswered }
        let answered = questions.filter(\.isAnswered)
        List {
            Section {
                Text("Things the research couldn't settle. Anything you remember helps: who sang, which night, what was on the tape.")
                    .font(.display(15)).foregroundStyle(.secondary)
                    .listRowBackground(Color.clear)
            }
            ForEach(grouped(open), id: \.0) { pid, title, items in
                Section {
                    ForEach(items) { q in
                        Button { editing = q } label: { QuestionRow(q: q) }.buttonStyle(.plain)
                    }
                } header: {
                    NavigationLink(value: Route.performance(pid)) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(title).font(.display(15, weight: .semibold)).textCase(nil)
                            Text(pid).font(.uiCaption2.monospaced()).textCase(nil).foregroundStyle(.tertiary).lineLimit(1)
                            Image(systemName: "chevron.right").font(.uiCaption2)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
            if !answered.isEmpty {
                Section {
                    DisclosureGroup("Answered (\(answered.count))", isExpanded: $showAnswered) {
                        ForEach(answered) { q in
                            Button { editing = q } label: { QuestionRow(q: q) }.buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .overlay {
            if !loaded && error == nil { ProgressView() }
            else if let error { ContentUnavailableView("Couldn't load questions", systemImage: "wifi.exclamationmark", description: Text(error)) }
            else if loaded && questions.isEmpty { ContentUnavailableView("No open questions", systemImage: "checkmark.circle") }
        }
        .refreshable { await load() }
        .task(id: model.generation) { await load() }
        .sheet(item: $editing) { q in
            AnswerSheet(question: q) { answer in
                try await model.api.answer(performanceId: q.performanceId, question: q.question, answer: answer)
                await load()
            }
        }
        .navigationTitle("Ask Dad")
    }

    private func grouped(_ qs: [Question]) -> [(String, String, [Question])] {
        var order: [String] = []
        var map: [String: [Question]] = [:]
        for q in qs {
            if map[q.performanceId] == nil { order.append(q.performanceId) }
            map[q.performanceId, default: []].append(q)
        }
        return order.map { ($0, map[$0]?.first?.performanceTitle ?? $0, map[$0] ?? []) }
    }

    private func load() async {
        do { questions = try await model.api.questions(); error = nil }
        catch is CancellationError { return }
        catch { self.error = error.localizedDescription }
        loaded = true
    }
}

struct QuestionRow: View {
    let q: Question
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(q.question).font(.uiSubheadline).lineLimit(5).fixedSize(horizontal: false, vertical: true)
            if let a = q.answer, q.isAnswered {
                Label { Text(a).font(.uiSubheadline.weight(.medium)) } icon: { Image(systemName: "checkmark.bubble.fill").foregroundStyle(Color.ink) }
                if let at = q.answeredAt { Text("Answered \(Fmt.date(at))").font(.uiCaption2).foregroundStyle(.tertiary) }
            } else {
                Text("Tap to answer").font(.uiCaption.weight(.medium)).foregroundStyle(.tint)
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }
}

struct AnswerSheet: View {
    let question: Question
    let submit: (String) async throws -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if let t = question.performanceTitle { Eyebrow(t) }
                    Text(question.question).font(.display(16))
                }
                Section("Your answer") {
                    TextEditor(text: $text).frame(minHeight: 140)
                }
                if let error { Section { Text(error).foregroundStyle(Color.ember).font(.uiFootnote) } }
            }
            .navigationTitle("Answer")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if sending { ProgressView() } else {
                        Button("Send") {
                            sending = true
                            Task {
                                do { try await submit(text); dismiss() }
                                catch { self.error = error.localizedDescription }
                                sending = false
                            }
                        }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }
            .onAppear { text = question.answer ?? "" }
        }
    }
}

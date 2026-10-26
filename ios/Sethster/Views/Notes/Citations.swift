import SwiftUI

// MARK: - Citation rows

extension Citation {
    /// The live page, or nil.
    var liveURL: URL? {
        guard let url = url.flatMap(URL.init(string:)), url.scheme == "http" || url.scheme == "https" else { return nil }
        return url
    }

    /// The archived copy: the recorded snapshot, else the Wayback Machine's latest capture of the live URL.
    var archiveURL: URL? {
        if let archived = waybackUrl.flatMap(URL.init(string:)),
           archived.scheme == "http" || archived.scheme == "https" { return archived }
        guard let liveURL else { return nil }
        return URL(string: "https://web.archive.org/web/" + liveURL.absoluteString)
    }

    init(source: Source) {
        self.init(sourceId: source.id, title: source.title, url: source.url, waybackUrl: source.waybackUrl)
    }

    init(resource: Resource) {
        self.init(title: resource.title, url: resource.url, waybackUrl: resource.waybackUrl)
    }

    static func wikipedia(_ url: String?) -> Citation? {
        guard let url else { return nil }
        return Citation(title: "Wikipedia", url: url, license: "CC BY-SA 4.0")
    }
}

/// A compact source credit. The title opens the live page when available, otherwise its archived copy.
struct CitationRow: View {
    let citation: Citation
    let open: (URL) -> Void
    var prefix = "Source"

    var body: some View {
        let live = citation.liveURL
        let bestURL = live ?? citation.archiveURL
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            if let bestURL {
                Button { open(bestURL) } label: {
                    Text(credit(bestURL))
                        .font(.uiCaption.weight(.medium))
                        .underline()
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
            } else {
                Text(citation.title ?? (prefix == "Source" ? "Unknown source" : prefix)).font(.uiCaption.weight(.medium))
            }
            let details = details(live ?? bestURL)
            if !details.isEmpty {
                Text("· " + details).font(.uiCaption).foregroundStyle(Color.graphite)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .contain)
    }

    private func credit(_ url: URL) -> String {
        citation.title ?? (prefix == "Source" ? url.host() ?? "Source" : prefix)
    }

    private func details(_ url: URL?) -> String {
        var parts: [String] = []
        if let host = url?.host(), citation.title != nil { parts.append(host.replacingOccurrences(of: "www.", with: "")) }
        if let l = citation.license { parts.append(l) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Markdown

/// Renders the Markdown subset the program-notes pipeline emits: headings, paragraphs, bullet and numbered lists,
/// block quotes, rules, and inline emphasis/links (via AttributedString). Links open in the in-app browser.
/// A trailing "Source: …" paragraph is shown as a distinct footer so the citation is always visible.
struct MarkdownView: View {
    let markdown: String
    var font: Font = .uiReading

    enum Block: Hashable {
        case heading(Int, String), paragraph(String), bullet(String), numbered(String, String), quote(String), rule
        case footer(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(Self.parse(markdown).enumerated()), id: \.offset) { _, block in
                view(block)
            }
        }
    }

    @ViewBuilder private func view(_ block: Block) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Self.inline(text))
                .font(level <= 1 ? .uiTitle2 : level == 2 ? .uiTitle3 : .uiHeadline)
                .padding(.top, level <= 2 ? 8 : 2)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let text):
            Text(Self.inline(text)).font(font).lineSpacing(5).fixedSize(horizontal: false, vertical: true)
        case .bullet(let text):
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("•").font(font).foregroundStyle(Color.ember)
                Text(Self.inline(text)).font(font).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
            }
        case .numbered(let n, let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(n).font(font.monospacedDigit()).foregroundStyle(Color.graphite)
                Text(Self.inline(text)).font(font).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
            }
        case .quote(let text):
            Text(Self.inline(text)).font(font).foregroundStyle(Color.graphite)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Color.ember).frame(width: 2) }
                .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Rectangle().fill(Color.rule).frame(height: 1).padding(.vertical, 4)
        case .footer(let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "text.book.closed").foregroundStyle(Color.ember)
                Text(Self.inline(text)).font(.uiFootnote).foregroundStyle(Color.graphite)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .accessibilityElement(children: .combine)
        }
    }

    static func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(s)
    }

    static func parse(_ md: String) -> [Block] {
        var blocks: [Block] = []
        var para: [String] = []
        func flush() {
            guard !para.isEmpty else { return }
            let text = para.joined(separator: " ")
            let plain = text.trimmingCharacters(in: CharacterSet(charactersIn: "*_ "))
            blocks.append(plain.hasPrefix("Source:") || plain.hasPrefix("Sources:") ? .footer(text) : .paragraph(text))
            para = []
        }
        for raw in md.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { flush(); continue }
            if line.hasPrefix("#") {
                flush()
                let level = line.prefix { $0 == "#" }.count
                blocks.append(.heading(level, line.dropFirst(level).trimmingCharacters(in: .whitespaces)))
            } else if line == "---" || line == "***" || line == "___" {
                flush(); blocks.append(.rule)
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("+ ") {
                flush(); blocks.append(.bullet(String(line.dropFirst(2))))
            } else if let r = line.range(of: #"^\d{1,3}[.)] "#, options: .regularExpression) {
                flush(); blocks.append(.numbered(String(line[r]).trimmingCharacters(in: .whitespaces), String(line[r.upperBound...])))
            } else if line.hasPrefix(">") {
                flush(); blocks.append(.quote(line.dropFirst().trimmingCharacters(in: .whitespaces)))
            } else {
                para.append(line)
            }
        }
        flush()
        return blocks
    }
}

extension View {
    /// Route Markdown/AttributedString links into the in-app browser.
    func linksInAppBrowser(_ web: Binding<WebLink?>) -> some View {
        environment(\.openURL, OpenURLAction { url in
            guard url.scheme == "http" || url.scheme == "https" else { return .systemAction }
            web.wrappedValue = WebLink(url: url)
            return .handled
        })
    }
}

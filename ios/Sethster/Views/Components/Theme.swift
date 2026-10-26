import SwiftUI
import UIKit

// Sethster's visual identity: the pencil-sketch logo's ink on paper, one warm accent ("ember", the asset
// catalog's AccentColor), and Lexend (SIL OFL, bundled in Resources/Fonts) for every word on screen.

// MARK: - Color

extension Color {
    /// Page background: warm paper (light) / soft black (dark).
    static let paper = Color("Paper")
    /// Raised surfaces (cards, tiles) on paper.
    static let card = Color("Card")
    /// Primary text and the sketch's pencil black.
    static let ink = Color("Ink")
    /// Secondary text.
    static let graphite = Color("Graphite")
    /// The one warm accent. Interactive elements, the current track, conflicts.
    static let ember = Color("AccentColor")
    /// Hairline rules.
    static let rule = Color("Ink").opacity(0.12)
}

// MARK: - Typography

/// Lexend type scale. Every style scales with Dynamic Type relative to the matching system text style.
extension Font {
    static let lexendFamily = "Lexend"

    static func lexend(_ size: CGFloat, weight: Font.Weight = .regular, relativeTo style: Font.TextStyle) -> Font {
        .custom(lexendFamily, size: size, relativeTo: style).weight(weight)
    }

    /// Display / headline text at an explicit size (kept for call sites that need a specific size). The Dynamic
    /// Type style it scales with is picked from the size.
    static func display(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        let style: Font.TextStyle = switch size {
        case 30...: .largeTitle
        case 25..<30: .title
        case 21..<25: .title2
        case 18..<21: .title3
        case 16.5..<18: .body
        case 15.5..<16.5: .callout
        case 14..<15.5: .subheadline
        case 12.5..<14: .footnote
        default: .caption
        }
        return lexend(size, weight: weight, relativeTo: style)
    }

    // Lexend sets wide and tall, so sizes sit about a point under the system defaults.
    static let uiLargeTitle = lexend(32, weight: .semibold, relativeTo: .largeTitle)
    static let uiTitle = lexend(26, weight: .semibold, relativeTo: .title)
    static let uiTitle2 = lexend(21, weight: .semibold, relativeTo: .title2)
    static let uiTitle3 = lexend(18, weight: .medium, relativeTo: .title3)
    static let uiHeadline = lexend(16, weight: .semibold, relativeTo: .headline)
    static let uiBody = lexend(16, relativeTo: .body)
    static let uiCallout = lexend(15, relativeTo: .callout)
    static let uiSubheadline = lexend(14, relativeTo: .subheadline)
    static let uiFootnote = lexend(13, relativeTo: .footnote)
    static let uiCaption = lexend(12, relativeTo: .caption)
    static let uiCaption2 = lexend(11, relativeTo: .caption2)
    /// Long-form reading (liner notes, program notes): light weight, generous leading set by the view.
    static let uiReading = lexend(17, weight: .light, relativeTo: .body)
}

enum Theme {
    /// UIKit chrome (navigation bars, segmented controls) in Lexend too. Call once at launch.
    @MainActor static func applyUIKitAppearance() {
        func font(_ name: String, _ size: CGFloat, _ style: UIFont.TextStyle) -> UIFont? {
            UIFont(name: name, size: size).map { UIFontMetrics(forTextStyle: style).scaledFont(for: $0) }
        }
        let nav = UINavigationBar.appearance()
        if let f = font("Lexend-SemiBold", 17, .headline) { nav.titleTextAttributes = [.font: f] }
        if let f = font("Lexend-SemiBold", 32, .largeTitle) { nav.largeTitleTextAttributes = [.font: f] }
        let seg = UISegmentedControl.appearance()
        if let f = font("Lexend-Regular", 13, .footnote) { seg.setTitleTextAttributes([.font: f], for: .normal) }
        if let f = font("Lexend-Medium", 13, .footnote) { seg.setTitleTextAttributes([.font: f], for: .selected) }
    }
}

// MARK: - Headings

struct Eyebrow: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(.lexend(11, weight: .semibold, relativeTo: .caption2))
            .tracking(1.3)
            .foregroundStyle(Color.graphite)
    }
}

struct SectionTitle: View {
    let title: String
    var trailing: String?
    init(_ title: String, trailing: String? = nil) { self.title = title; self.trailing = trailing }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Rectangle().fill(Color.ink).frame(width: 28, height: 2)
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.uiTitle2)
                Spacer()
                if let trailing { Text(trailing).font(.uiFootnote).foregroundStyle(Color.graphite) }
            }
        }
        .padding(.horizontal)
        .padding(.top, 30)
        .padding(.bottom, 10)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

// MARK: - Evidence badges

/// Archival "stamp" badges. Each status differs in shape and fill as well as wording, so it reads without color:
/// confirmed = solid ink, production only = outlined, inferred = dashed outline, conflict = solid ember.
struct EvidenceBadge: View {
    let status: EvidenceStatus
    /// Icon only (the label is still spoken by VoiceOver).
    var compact = false

    private var fill: Color? {
        switch status {
        case .confirmed: .ink
        case .conflict: .ember
        default: nil
        }
    }

    private var stroke: StrokeStyle? {
        switch status {
        case .productionOnly: StrokeStyle(lineWidth: 1)
        case .inferred, .unknown: StrokeStyle(lineWidth: 1, dash: [3, 2])
        default: nil
        }
    }

    var body: some View {
        Group {
            if compact {
                Image(systemName: status.symbol)
                    .font(.uiFootnote)
                    .foregroundStyle(status == .conflict ? Color.ember : Color.ink)
            } else {
                HStack(spacing: 4) {
                    Image(systemName: status.symbol).imageScale(.small)
                    Text(status.label)
                }
                .font(.lexend(11, weight: .medium, relativeTo: .caption2))
                .foregroundStyle(fill != nil ? Color.paper : Color.ink)
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background {
                    if let fill { Capsule().fill(fill) }
                }
                .overlay {
                    if let stroke { Capsule().strokeBorder(Color.ink.opacity(0.7), style: stroke) }
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Evidence: \(status.label)")
        .accessibilityHint(status.explanation)
    }
}

/// Share of credits a date-specific source confirms.
struct ConfidenceMeter: View {
    let ratio: Double
    var short = false
    var body: some View {
        HStack(spacing: 8) {
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.rule)
                    Capsule().fill(Color.ink).frame(width: g.size.width * min(max(ratio, 0), 1))
                }
            }
            .frame(width: 60, height: 4)
            Text("\(Int((ratio * 100).rounded()))%" + (short ? " confirmed" : " of credits confirmed"))
                .font(.uiCaption).foregroundStyle(Color.graphite)
        }
        .accessibilityElement(children: .combine)
    }
}

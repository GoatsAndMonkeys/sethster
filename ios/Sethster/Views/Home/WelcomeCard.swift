import SwiftUI

/// Shown once per account on this device, after the first sign-in: confirms the Rotation came across
/// (adoptLegacyFiles moved the old on-device downloads into the account) and lists what's new.
enum WelcomeCardMemory {
    private static func key(_ account: String) -> String { "welcomeCardSeen:" + account }
    static func seen(account: String) -> Bool { UserDefaults.standard.bool(forKey: key(account)) }
    static func markSeen(account: String) { UserDefaults.standard.set(true, forKey: key(account)) }
}

struct WelcomeCard: View {
    @Environment(RotationStore.self) private var rotation
    let name: String?
    let onDone: () -> Void

    private var rotationLine: String {
        let n = rotation.items.count
        if n > 0 { return "Your Rotation came across: \(n) \(n == 1 ? "recording" : "recordings"), right where you left them." }
        if rotation.isLoading { return "Your Rotation is coming across now. It will be in the Rotation tab in a moment." }
        return "Your Rotation is now kept with your account."
    }

    private let news: [(symbol: String, title: String, detail: String)] = [
        ("books.vertical", "Browse", "Look through composers, performers, venues, or every performance."),
        ("dot.radiowaves.left.and.right", "Radio", "Ready-made stations, or make your own."),
        ("car", "CarPlay", "The same Browse and Radio on your car’s screen."),
        ("moon.stars", "Optoberfest", "All October, a spooky pick each day on Home."),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Eyebrow("Welcome")
                Text(name.map { "Hello, \($0)" } ?? "You’re signed in")
                    .font(.uiLargeTitle).padding(.top, 6)

                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "checkmark.circle.fill").font(.uiTitle2).foregroundStyle(Color.ember)
                    Text(rotationLine).font(.uiBody).fixedSize(horizontal: false, vertical: true)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.rule, lineWidth: 0.75))
                .padding(.top, 18)
                .accessibilityElement(children: .combine)

                Rectangle().fill(Color.ink).frame(width: 28, height: 2).padding(.top, 30)
                Text("What’s new").font(.uiTitle2).padding(.top, 8).padding(.bottom, 6)
                    .accessibilityAddTraits(.isHeader)

                ForEach(news, id: \.title) { item in
                    HStack(alignment: .top, spacing: 14) {
                        Image(systemName: item.symbol).font(.uiTitle3).foregroundStyle(Color.ember)
                            .frame(width: 30)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title).font(.uiHeadline)
                            Text(item.detail).font(.uiSubheadline).foregroundStyle(Color.graphite)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.vertical, 9)
                    .accessibilityElement(children: .combine)
                }

                Button(action: onDone) {
                    Text("Got it").font(.uiHeadline).frame(maxWidth: .infinity).padding(.vertical, 6)
                }
                .buttonStyle(.borderedProminent).tint(Color.ember)
                .padding(.top, 28)
            }
            .padding(24)
        }
        .background(Color.paper)
        .interactiveDismissDisabled()
    }
}

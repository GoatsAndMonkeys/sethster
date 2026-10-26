import SwiftUI
import UserNotifications

/// One AppModel (settings + API client) and one PlayerController for the whole process, shared by the
/// SwiftUI phone scene and the CarPlay scene (which can connect without the phone UI ever appearing).
@MainActor
enum AppServices {
    /// Starts network monitoring, whose first callback runs the launch reachability check (→ demo fallback
    /// if the server is unreachable). Either scene may be the first to touch this.
    static let model: AppModel = {
        let m = AppModel()
        m.startMonitoringNetwork()
        return m
    }()
    static let rotation = RotationStore(model: model)
    static let player = PlayerController()
    static let radio = RadioPlayer()
    /// The signed-in account. Its teardown is the sign-out / revocation path: stop playback, clear Now Playing,
    /// cancel account-bound downloads (the Rotation namespace changes with the account).
    static let account = AccountStore(
        model: model,
        teardownContent: {
            player.stop()
            radio.stop()
        },
        namespaceChanged: { rotation.setAccount($0) })
}

@main
struct SethsterApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppServices.model
    @State private var rotation = AppServices.rotation
    @State private var player = AppServices.player
    @State private var radio = AppServices.radio
    @State private var account = AppServices.account
    @Environment(\.scenePhase) private var scenePhase

    init() { Theme.applyUIKitAppearance() }

    var body: some Scene {
        WindowGroup {
            RootView()
                .font(.uiBody)
                .tint(Color.ember)
                .environment(model)
                .environment(rotation)
                .environment(player)
                .environment(radio)
                .environment(account)
                .task(id: model.generation) { await rotation.refresh() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await model.checkServer() }
                Task { await account.refresh() }
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // Register on every launch; the token is uploaded once a session exists (plan: Push).
        application.registerForRemoteNotifications()
        _ = AppServices.account
        #if DEBUG
        // Screenshot hook: `-demoBrowse YES` opens the demo data without the Welcome screen's button.
        if UserDefaults.standard.bool(forKey: "demoBrowse") { AppServices.model.setBrowsingDemo(true) }
        #endif
        return true
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        AppServices.account.didRegisterForPush(deviceToken: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {}

    /// Approval / sign-up pushes: show them in the foreground too, and refresh `me` and the badge.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        await MainActor.run { AppServices.account.pushReceived(tapped: false) }
        return [.banner, .list, .sound, .badge]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        await MainActor.run { AppServices.account.pushReceived(tapped: true) }
    }

    func application(_ application: UIApplication,
                     handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == RotationDownloadCoordinator.sessionIdentifier else {
            completionHandler()
            return
        }
        let coordinator = RotationDownloadCoordinator.shared
        coordinator.handleBackgroundEvents(completionHandler: completionHandler)
        // Recreate the store as well as the session on a background relaunch, so delegate callbacks have
        // an owner that can validate files and publish completion before iOS snapshots the process again. The
        // account comes first: it sets the Rotation namespace, without which completions are dropped.
        _ = AppServices.account
        _ = AppServices.rotation
    }
}

/// Off for friends & family; the research questions ("Ask Dad", open questions) come back later for Dad.
enum FeatureFlags {
    static let askDad = false
}

enum AppTab: Hashable { case home, rotation, browse, radio, search, askDad }

struct RootView: View {
    @Environment(PlayerController.self) private var player
    @Environment(AppModel.self) private var model
    @Environment(RadioPlayer.self) private var radio
    @Environment(AccountStore.self) private var account
    @State private var tab: AppTab = .home
    @State private var homePath: [Route] = []
    @State private var browsePath: [Route] = []
    @State private var welcomeFor: WelcomeTarget?

    var body: some View {
        if account.showsContent {
            content
        } else if case .status(let status) = account.gate {
            AccountStatusView(status: status)
        } else {
            WelcomeView()
        }
    }

    private var content: some View {
        @Bindable var player = player
        @Bindable var account = account
        return tabs
            .safeAreaInset(edge: .top, spacing: 0) {
                if account.gate != .app { DemoAccountBanner() }
            }
            .sheet(isPresented: $account.showApprovals) {
                NavigationStack { ApprovalsView().closeButton() }
            }
            .sheet(item: $welcomeFor) { shown in
                WelcomeCard(name: account.me?.name) {
                    WelcomeCardMemory.markSeen(account: shown.id)
                    welcomeFor = nil
                }
            }
            .task(id: account.rotationNamespace) {
                #if DEBUG
                // Screenshot hook (like `-route`): `-previewWelcomeCard YES` shows the card without an account.
                if UserDefaults.standard.bool(forKey: "previewWelcomeCard") {
                    welcomeFor = WelcomeTarget(id: "debug-preview")
                    return
                }
                #endif
                // Once per account on this device, after the first sign-in (approved accounts only, not demo).
                guard let ns = account.rotationNamespace, !WelcomeCardMemory.seen(account: ns) else { welcomeFor = nil; return }
                welcomeFor = WelcomeTarget(id: ns)
            }
            .sheet(isPresented: $player.showNowPlaying) {
                NowPlayingView()
                    .presentationDragIndicator(.visible)
            }
            .sheet(isPresented: Binding(get: { radio.showNowPlaying }, set: { radio.showNowPlaying = $0 })) {
                RadioNowPlayingView().presentationDragIndicator(.visible)
            }
            .onOpenURL { handle($0) }
            .task {
                // Launch argument `-route sethster://...` (screenshots / UI automation without the open-URL prompt).
                if let r = UserDefaults.standard.string(forKey: "route"), let u = URL(string: r) { handle(u) }
            }
    }

    /// Deep links: sethster://performance/<id>, composer/<id>, work/<id>, person/<id>, venue/<id>,
    /// collection/<name>, play/<performance-id>[?track=N] (starts playback and opens Now Playing),
    /// tab/<home|browse|search|askdad>.
    private func handle(_ url: URL) {
        guard url.scheme == "sethster", let host = url.host() else { return }
        let arg = url.pathComponents.dropFirst().joined(separator: "/").removingPercentEncoding ?? ""
        let route: Route? = switch host {
        case "performance": .performance(arg)
        case "composer": .composer(arg)
        case "work": .work(arg)
        case "person": .person(arg)
        case "venue": .venue(arg)
        case "collection": .collection(arg)
        case "liner": .linerNotes(arg)
        case "program":
            arg.split(separator: "/").count == 2
                ? .programNote(String(arg.split(separator: "/")[0]), Int(arg.split(separator: "/")[1]) ?? 1) : nil
        default: nil
        }
        if let route { tab = .home; homePath = [route]; return }
        switch host {
        case "tab":
            tab = ["rotation": .rotation, "browse": .browse, "radio": .radio,
                   "search": .search, "askdad": .askDad][arg] ?? .home
        case "play":
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let track = items.first { $0.name == "track" }?.value.flatMap(Int.init) ?? 1
            let showSheet = items.first { $0.name == "sheet" }?.value != "0"
            let api = model.api, epoch = AppServices.account.epoch
            Task {
                guard let perf = try? await api.performance(id: arg), epoch == AppServices.account.epoch else { return }
                player.play(perf, from: track - 1, api: api)
                player.showNowPlaying = showSheet
            }
        default: break
        }
    }

    @ViewBuilder private var tabs: some View {
        let view = TabView(selection: $tab) {
            Tab("Home", systemImage: "music.note.house", value: AppTab.home) {
                NavigationStack(path: $homePath) { HomeView().withRoutes() }.legacyMiniPlayerInset()
            }
            Tab("Rotation", systemImage: "arrow.trianglehead.2.clockwise.rotate.90", value: AppTab.rotation) {
                NavigationStack { RotationView().withRoutes() }.legacyMiniPlayerInset()
            }
            Tab("Browse", systemImage: "books.vertical", value: AppTab.browse) {
                NavigationStack(path: $browsePath) { BrowseView().withRoutes() }.legacyMiniPlayerInset()
            }
            Tab("Radio", systemImage: "dot.radiowaves.left.and.right", value: AppTab.radio) {
                NavigationStack { RadioView().withRoutes() }.legacyMiniPlayerInset()
            }
            if FeatureFlags.askDad {
                Tab("Ask Dad", systemImage: "questionmark.bubble", value: AppTab.askDad) {
                    NavigationStack { AskDadView().withRoutes() }.legacyMiniPlayerInset()
                }
            }
            Tab(value: AppTab.search, role: .search) {
                NavigationStack { SearchView().withRoutes() }.legacyMiniPlayerInset()
            }
        }
        if #available(iOS 26.1, *) {
            view
                .tabViewBottomAccessory(isEnabled: player.hasQueue || radio.isActive) { AnyMiniPlayer() }
                .tabBarMinimizeBehavior(.onScrollDown)
        } else {
            view
        }
    }
}

/// The account (Rotation namespace) a Welcome card is showing for.
struct WelcomeTarget: Identifiable { let id: String }

private struct LegacyMiniPlayerInset: ViewModifier {
    @Environment(PlayerController.self) private var player
    @Environment(RadioPlayer.self) private var radio
    func body(content: Content) -> some View {
        if #available(iOS 26.1, *) {
            content
        } else {
            content.safeAreaInset(edge: .bottom, spacing: 0) {
                if player.hasQueue || radio.isActive {
                    AnyMiniPlayer()
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .shadow(color: .black.opacity(0.12), radius: 10, y: 3)
                        .padding(.horizontal, 10).padding(.bottom, 6)
                }
            }
        }
    }
}

extension View {
    fileprivate func legacyMiniPlayerInset() -> some View { modifier(LegacyMiniPlayerInset()) }
}

import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(RotationStore.self) private var rotation
    @Environment(AccountStore.self) private var account
    @Environment(\.dismiss) private var dismiss
    @State private var status: TestResult?
    /// Edited here, committed on submit / leaving Settings: changing the server signs out, so not per keystroke.
    @State private var serverDraft = ""
    @State private var confirmServerChange = false

    struct TestResult: Equatable {
        let ok: Bool
        let message: String
    }
    @State private var testing = false

    var body: some View {
        @Bindable var model = model
        @Bindable var rotation = rotation
        NavigationStack {
            Form {
                AccountSettingsSections()

                Section {
                    TextField("Server URL", text: $serverDraft, prompt: Text(AppModel.defaultServer))
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { commitServer() }
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("Test Connection")
                            Spacer()
                            if testing { ProgressView() }
                        }
                    }
                    .disabled(model.useDemoData)
                    if let status {
                        Label {
                            Text(status.message).font(.uiFootnote)
                                .foregroundStyle(status.ok ? Color.primary : Color.red)
                                .fixedSize(horizontal: false, vertical: true)
                        } icon: {
                            Image(systemName: status.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                .foregroundStyle(status.ok ? Color.green : Color.red)
                        }
                    }
                    if !model.isOfflineFallback, !model.useDemoData, model.activeServer != model.serverURL {
                        Text("This server isn’t answering, so the app is using \(model.activeServer).")
                            .font(.uiFootnote).foregroundStyle(.secondary)
                    }
                    if model.isOfflineFallback && status == nil {
                        Text("Can’t reach this server right now, so the app is showing demo recordings. It switches back automatically when the server is reachable.")
                            .font(.uiFootnote).foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Sethster Server")
                } footer: {
                    Text("The family server. The default, \(AppModel.defaultServer), works anywhere with an internet connection. At home it falls back to its secure Tailscale address. If neither can be reached, the app shows demo recordings until the server is back. Changing to a different server signs you out.")
                }

                Section {
                    Toggle("Use demo data", isOn: $model.useDemoData)
                } footer: {
                    Text("Browse a few bundled performances without a server. Audio needs the server.")
                }

                Section {
                    Toggle("Download over cellular", isOn: $rotation.allowsCellularDownloads)
                } header: {
                    Text("In Rotation")
                } footer: {
                    Text("Rotation downloads use Wi-Fi by default. Existing downloads always remain playable offline.")
                }

                Section("About") {
                    LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "–")
                    HStack {
                        Image("Logo").resizable().scaledToFit().frame(width: 36, height: 36)
                            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        Text("Sethster: the family's off-air archive.").font(.display(15))
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { if commitServer() { dismiss() } } } }
            .onAppear { serverDraft = model.serverURL }
            .onDisappear { _ = commitServer(interactive: false) }
            .alert("Change server?", isPresented: $confirmServerChange) {
                Button("Change and Sign Out", role: .destructive) { applyServer() }
                Button("Cancel", role: .cancel) { serverDraft = model.serverURL }
            } message: {
                Text("Your account belongs to this server, so changing it signs you out.")
            }
        }
    }

    private var trimmedDraft: String { serverDraft.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// Commit the draft; false while asking first. Moving a signed-in account to a different server asks (it signs
    /// out); swiping Settings away mid-edit (`interactive: false`) keeps the current server in that case.
    @discardableResult
    private func commitServer(interactive: Bool = true) -> Bool {
        let next = trimmedDraft
        guard !next.isEmpty, next != model.serverURL else { return true }
        if account.token != nil, AppModel.serverIdentity(next) != AppModel.serverIdentity(model.serverURL) {
            if interactive { confirmServerChange = true }
            return false
        }
        applyServer()
        return true
    }

    private func applyServer() {
        let next = trimmedDraft
        guard !next.isEmpty else { return }
        model.serverURL = next
        status = nil
    }

    private func test() async {
        testing = true
        defer { testing = false }
        status = nil
        guard commitServer() else { return }
        guard let live = LiveAPIClient(serverURL: model.serverURL, token: model.sessionToken) else {
            status = TestResult(ok: false, message: "That server address isn’t valid. Check it and try again.")
            return
        }
        do {
            let h = try await live.health()
            guard h.ok else {
                status = TestResult(ok: false, message: "The server answered but says it isn’t healthy. Try again in a minute.")
                return
            }
            // health is open; content needs an approved account
            if account.gate == .app { _ = try await live.performances(PerformanceQuery(limit: 1)) }
            status = TestResult(ok: true, message: "Connected" + (h.performances.map { " · \($0) performances" } ?? ""))
            if model.isOfflineFallback { await model.checkServer() } else { model.reload() }
        } catch {
            status = TestResult(ok: false, message: Self.reason(error))
        }
    }

    /// A plain-English reason a connection test failed.
    static func reason(_ error: Error) -> String {
        if let e = error as? APIError {
            switch e {
            case .badURL: return "That server address isn’t valid. Check it and try again."
            case .decoding: return "The server answered with something the app doesn’t understand. It may need updating."
            case .auth(let failure): return failure.message
            case .http(let code, _):
                switch code {
                case 401, 403: return "The server needs you to sign in again."
                case 404: return "Something answered at that address, but it isn’t the Sethster server."
                case 502, 503, 504: return "The server on the NAS isn’t answering (it may be restarting). Try again in a minute."
                case 500...599: return "The server on the NAS had a problem (error \(code)). Try again in a minute."
                default: return "The server answered with error \(code)."
                }
            }
        }
        if let e = error as? URLError {
            switch e.code {
            case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed, .internationalRoamingOff:
                return "No connection to your Tailscale network. Check that Tailscale is on and you’re online."
            case .cannotFindHost, .dnsLookupFailed:
                return "Can’t find the server. Make sure Tailscale is connected on this phone."
            case .cannotConnectToHost:
                return "Couldn’t connect to the server. Check that Tailscale is on; the server on the NAS may be restarting."
            case .timedOut:
                return "The server took too long to answer. The NAS may be busy or asleep; try again in a minute."
            case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
                 .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot, .clientCertificateRejected:
                return "The secure connection to the server failed. Check the address starts with https:// and is the NAS."
            case .appTransportSecurityRequiresSecureConnection:
                return "This address needs https://."
            default:
                return "Couldn’t reach the server (\(e.localizedDescription))."
            }
        }
        return "Couldn’t reach the server (\(error.localizedDescription))."
    }
}

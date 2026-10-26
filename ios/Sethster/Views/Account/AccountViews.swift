import SwiftUI

// Plan: iOS → Root gate. Welcome (signed out), the status screen (pending / rejected / disabled), the demo banner,
// Settings → Account, and the admin Approvals / Everyone lists.

// MARK: - Welcome

struct WelcomeView: View {
    @Environment(AppModel.self) private var model
    @State private var form: AccountFormView.Mode?
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 28) {
                    VStack(spacing: 12) {
                        Image("Logo").resizable().scaledToFit().frame(width: 88, height: 88)
                            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                        Text("Sethster").font(.display(40, weight: .bold))
                        Text("The family's off-air archive of classical broadcasts.")
                            .font(.uiCallout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding(.top, 48)

                    VStack(spacing: 12) {
                        Button { form = .signIn } label: {
                            Text("Sign In").font(.uiHeadline).frame(maxWidth: .infinity).padding(.vertical, 6)
                        }
                        .buttonStyle(.borderedProminent)
                        Button { form = .signUp } label: {
                            Text("Create Account").font(.uiHeadline).frame(maxWidth: .infinity).padding(.vertical, 6)
                        }
                        .buttonStyle(.bordered)
                        Text("New accounts are approved by the family before they can listen.")
                            .font(.uiFootnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }

                    Button("Browse Demo Data") { model.setBrowsingDemo(true) }
                        .font(.uiBody)

                    PrivacyLink()
                }
                .padding(.horizontal, 24)
                .frame(maxWidth: 480)
                .frame(maxWidth: .infinity)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Server Settings")
                }
            }
            .sheet(item: $form) { AccountFormView(mode: $0) }
            .sheet(isPresented: $showSettings) { SettingsView() }
        }
    }
}

struct PrivacyLink: View {
    var body: some View {
        Link(destination: AccountLinks.privacy) {
            Label("Privacy Policy", systemImage: "hand.raised")
        }
        .font(.uiFootnote)
    }
}

// MARK: - Sign In / Create Account

struct AccountFormView: View {
    enum Mode: String, Identifiable {
        case signIn, signUp
        var id: String { rawValue }
    }

    let mode: Mode
    @Environment(AccountStore.self) private var account
    @Environment(\.dismiss) private var dismiss
    @State private var username = ""
    @State private var password = ""
    @State private var displayName = ""
    @State private var working = false
    @State private var error: String?
    @FocusState private var focus: Field?

    private enum Field { case username, displayName, password }

    private static let usernameChars = CharacterSet(charactersIn:
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

    private var trimmedUsername: String { username.trimmingCharacters(in: .whitespaces) }
    private var trimmedName: String { displayName.trimmingCharacters(in: .whitespaces) }

    /// The server's rules (plan: Endpoints), checked here so most mistakes never leave the phone.
    private var problem: String? {
        let u = trimmedUsername
        if mode == .signIn { return u.isEmpty || password.isEmpty ? "" : nil }
        if u.count < 3 || u.count > 32 { return "Usernames are 3 to 32 characters." }
        if !u.unicodeScalars.allSatisfy(Self.usernameChars.contains) {
            return "Usernames use letters, numbers, dots, dashes and underscores."
        }
        if trimmedName.count > 64 { return "Display names are at most 64 characters." }
        if password.count < 8 { return "Passwords are at least 8 characters." }
        if password.count > 128 { return "Passwords are at most 128 characters." }
        return nil
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Username", text: $username)
                        .textContentType(.username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .focused($focus, equals: .username).submitLabel(.next)
                        .onSubmit { focus = mode == .signUp ? .displayName : .password }
                    if mode == .signUp {
                        TextField("Display name (optional)", text: $displayName)
                            .textContentType(.name)
                            .focused($focus, equals: .displayName).submitLabel(.next)
                            .onSubmit { focus = .password }
                    }
                    SecureField("Password", text: $password)
                        .textContentType(mode == .signUp ? .newPassword : .password)
                        .focused($focus, equals: .password).submitLabel(.go)
                        .onSubmit { Task { await submit() } }
                } footer: {
                    if mode == .signUp {
                        Text("3–32 letters, numbers, dots, dashes or underscores. Passwords are at least 8 characters. The family approves new accounts; you'll get a notification when yours is ready.")
                    }
                }

                if let message = error ?? (mode == .signUp && !username.isEmpty && !password.isEmpty ? problem : nil),
                   !message.isEmpty {
                    Section {
                        Label(message, systemImage: "exclamationmark.triangle")
                            .font(.uiFootnote).foregroundStyle(.red)
                    }
                }

                Section {
                    Button {
                        Task { await submit() }
                    } label: {
                        HStack {
                            Text(mode == .signIn ? "Sign In" : "Create Account").font(.uiHeadline)
                            Spacer()
                            if working { ProgressView() }
                        }
                    }
                    .disabled(working || problem != nil)
                } footer: {
                    PrivacyLink().padding(.top, 8)
                }
            }
            .navigationTitle(mode == .signIn ? "Sign In" : "Create Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .onAppear { focus = .username }
            .onChange(of: username) { error = nil }
            .onChange(of: password) { error = nil }
        }
    }

    private func submit() async {
        guard problem == nil, !working else { return }
        working = true
        defer { working = false }
        error = nil
        do {
            switch mode {
            case .signIn:
                try await account.signIn(username: trimmedUsername, password: password)
            case .signUp:
                try await account.signUp(username: trimmedUsername, password: password,
                                         displayName: trimmedName.isEmpty ? trimmedUsername : trimmedName)
            }
            dismiss()
        } catch {
            self.error = accountMessage(error)
        }
    }
}

// MARK: - Pending / rejected / disabled

struct AccountStatusView: View {
    let status: AccountStatus
    @Environment(AccountStore.self) private var account
    @Environment(AppModel.self) private var model
    @State private var checking = false
    @State private var showDelete = false

    private var icon: String {
        switch status {
        case .pending: "hourglass"
        case .rejected: "hand.raised"
        case .disabled: "pause.circle"
        case .approved: "checkmark.circle"
        }
    }

    private var title: String {
        switch status {
        case .pending: "Waiting for approval"
        case .rejected: "Request not approved"
        case .disabled: "Account turned off"
        case .approved: "Approved"
        }
    }

    private var message: String {
        switch status {
        case .pending: "The family approves each new Sethster account. You'll get a notification when yours is ready, or check again here."
        case .rejected: "This Sethster account request wasn't approved. You can still browse the demo recordings, or delete the account."
        case .disabled: "This Sethster account has been turned off. Ask the family if you think that's a mistake."
        case .approved: ""
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    VStack(spacing: 12) {
                        Image(systemName: icon).font(.system(size: 52)).foregroundStyle(.tint)
                        Text(title).font(.display(30, weight: .bold)).multilineTextAlignment(.center)
                        if let me = account.me {
                            Text("Signed in as \(me.username)").font(.uiFootnote).foregroundStyle(.secondary)
                        }
                        Text(message).font(.uiCallout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding(.top, 48)

                    VStack(spacing: 12) {
                        if status == .pending {
                            Button {
                                Task {
                                    checking = true
                                    await account.refresh()
                                    checking = false
                                }
                            } label: {
                                HStack {
                                    Text("Check Again").font(.uiHeadline)
                                    if checking { ProgressView() }
                                }
                                .frame(maxWidth: .infinity).padding(.vertical, 6)
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(checking)
                        }
                        Button { model.setBrowsingDemo(true) } label: {
                            Text("Browse Demo Data").font(.uiHeadline).frame(maxWidth: .infinity).padding(.vertical, 6)
                        }
                        .buttonStyle(.bordered)
                    }

                    VStack(spacing: 16) {
                        Button("Sign Out") { Task { await account.signOut() } }
                        Button("Delete Account", role: .destructive) { showDelete = true }
                        PrivacyLink()
                    }
                    .font(.uiBody)
                }
                .padding(.horizontal, 24)
                .frame(maxWidth: 480)
                .frame(maxWidth: .infinity)
            }
            .refreshable { await account.refresh() }
            .sheet(isPresented: $showDelete) { DeleteAccountView() }
        }
    }
}

/// Over the tabs while browsing demo data without an approved account.
struct DemoAccountBanner: View {
    @Environment(AppModel.self) private var model
    @Environment(AccountStore.self) private var account

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "info.circle").foregroundStyle(.secondary)
            Text("Demo recordings").font(.uiFootnote).foregroundStyle(.secondary)
            Spacer()
            Button(account.gate == .welcome ? "Sign In" : "Your Account") { model.setBrowsingDemo(false) }
                .font(.uiFootnote.weight(.semibold))
                .buttonStyle(.borderless)
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
        .background(.bar)
    }
}

// MARK: - Settings → Account

/// The Account (and, for admins, Admin) sections of Settings.
struct AccountSettingsSections: View {
    @Environment(AccountStore.self) private var account
    @Environment(AppModel.self) private var model
    @State private var showDelete = false

    var body: some View {
        if let me = account.me, account.gate == .app {
            Section {
                LabeledContent("Signed in as", value: me.username)
                if me.name != me.username { LabeledContent("Name", value: me.name) }
                NavigationLink("Change Password") { ChangePasswordView() }
                Button("Sign Out") { Task { await account.signOut() } }
                Button("Delete Account", role: .destructive) { showDelete = true }
                    .sheet(isPresented: $showDelete) { DeleteAccountView() }
                PrivacyLink()
            } header: {
                Text("Account")
            }
            if account.isAdmin {
                Section("Admin") {
                    NavigationLink { ApprovalsView() } label: {
                        Text("Approvals").badge(account.pendingApprovals)
                    }
                    NavigationLink("Everyone") { EveryoneView() }
                }
            }
        } else {
            Section {
                Button(account.gate == .welcome ? "Sign In" : "Your Account") { model.setBrowsingDemo(false) }
                PrivacyLink()
            } header: {
                Text("Account")
            } footer: {
                Text(model.browsingDemo ? "You're browsing demo recordings. Sign in to hear the family archive." : "")
            }
        }
    }
}

struct ChangePasswordView: View {
    @Environment(AccountStore.self) private var account
    @Environment(\.dismiss) private var dismiss
    @State private var old = ""
    @State private var new = ""
    @State private var confirm = ""
    @State private var working = false
    @State private var error: String?

    private var problem: String? {
        if old.isEmpty || new.isEmpty || confirm.isEmpty { return "" }
        if new.count < 8 { return "Passwords are at least 8 characters." }
        if new.count > 128 { return "Passwords are at most 128 characters." }
        if new != confirm { return "The new passwords don't match." }
        return nil
    }

    var body: some View {
        Form {
            Section {
                SecureField("Current password", text: $old).textContentType(.password)
                SecureField("New password", text: $new).textContentType(.newPassword)
                SecureField("New password again", text: $confirm).textContentType(.newPassword)
            } footer: {
                if let message = error ?? problem, !message.isEmpty {
                    Text(message).foregroundStyle(.red)
                }
            }
            Section {
                Button {
                    Task { await save() }
                } label: {
                    HStack {
                        Text("Change Password")
                        Spacer()
                        if working { ProgressView() }
                    }
                }
                .disabled(working || problem != nil)
            }
        }
        .navigationTitle("Change Password")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func save() async {
        working = true
        defer { working = false }
        error = nil
        do {
            try await account.changePassword(old: old, new: new)
            dismiss()
        } catch {
            self.error = accountMessage(error)
        }
    }
}

/// Delete Account: password, a confirmation, and a second one for the last admin (approvals stop until
/// `create-admin` is run on the server).
struct DeleteAccountView: View {
    @Environment(AccountStore.self) private var account
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var working = false
    @State private var error: String?
    @State private var confirm = false
    @State private var confirmLastAdmin = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Password", text: $password).textContentType(.password)
                } footer: {
                    Text("Deleting your account removes your username, password, Rotation and notification settings from the Sethster server, and your downloads from this iPhone. This can't be undone.")
                }
                if let error {
                    Section { Text(error).font(.uiFootnote).foregroundStyle(.red) }
                }
                Section {
                    Button(role: .destructive) {
                        confirm = true
                    } label: {
                        HStack {
                            Text("Delete Account")
                            Spacer()
                            if working { ProgressView() }
                        }
                    }
                    .disabled(password.isEmpty || working)
                }
            }
            .navigationTitle("Delete Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .confirmationDialog("Delete your Sethster account?", isPresented: $confirm, titleVisibility: .visible) {
                Button("Delete Account", role: .destructive) {
                    Task {
                        working = true
                        let last = await account.isLastAdmin()
                        working = false
                        if last { confirmLastAdmin = true } else { await delete() }
                    }
                }
            } message: {
                Text("This can't be undone.")
            }
            .alert("You're the only admin", isPresented: $confirmLastAdmin) {
                Button("Delete Anyway", role: .destructive) { Task { await delete() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("No one will be able to approve new accounts until an admin is created again on the server (create-admin).")
            }
        }
    }

    private func delete() async {
        working = true
        defer { working = false }
        error = nil
        do {
            try await account.deleteAccount(password: password)
            dismiss()
        } catch {
            self.error = accountMessage(error)
        }
    }
}

// MARK: - Admin

/// Sign-ups waiting for a decision.
struct ApprovalsView: View {
    @Environment(AccountStore.self) private var account
    @State private var users: [AccountUser]?
    @State private var error: String?
    @State private var busy: Set<String> = []

    var body: some View {
        List {
            if let error {
                Text(error).font(.uiFootnote).foregroundStyle(.red)
            }
            if let users {
                if users.isEmpty {
                    ContentUnavailableView("No one is waiting", systemImage: "checkmark.circle",
                                           description: Text("New sign-ups appear here."))
                }
                ForEach(users) { user in
                    VStack(alignment: .leading, spacing: 8) {
                        AccountUserRow(user: user)
                        HStack {
                            Button("Approve") { Task { await act(.approve, user) } }
                                .buttonStyle(.borderedProminent)
                            Button("Reject", role: .destructive) { Task { await act(.reject, user) } }
                                .buttonStyle(.bordered)
                            if busy.contains(user.id) { ProgressView().padding(.leading, 4) }
                        }
                        .disabled(busy.contains(user.id))
                        .font(.uiSubheadline)
                    }
                    .padding(.vertical, 4)
                }
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Approvals")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        do {
            users = try await account.users(status: .pending)
            error = nil
            await account.refreshPendingCount()
        } catch {
            self.error = accountMessage(error)
        }
    }

    private func act(_ action: AdminAction, _ user: AccountUser) async {
        busy.insert(user.id)
        defer { busy.remove(user.id) }
        do {
            try await account.perform(action, on: user)
            users?.removeAll { $0.id == user.id }
            error = nil
        } catch {
            self.error = accountMessage(error)
        }
    }
}

/// Every account, with Disable / Enable / Make Admin (none on yourself).
struct EveryoneView: View {
    @Environment(AccountStore.self) private var account
    @State private var users: [AccountUser]?
    @State private var error: String?
    @State private var busy: Set<String> = []

    var body: some View {
        List {
            if let error {
                Text(error).font(.uiFootnote).foregroundStyle(.red)
            }
            if let users {
                ForEach(users) { user in
                    HStack {
                        AccountUserRow(user: user)
                        Spacer()
                        if busy.contains(user.id) {
                            ProgressView()
                        } else if user.id != account.me?.userId, !actions(for: user).isEmpty {
                            Menu {
                                ForEach(actions(for: user), id: \.self) { action in
                                    Button(title(action), role: [.reject, .disable].contains(action) ? .destructive : nil) {
                                        Task { await act(action, user) }
                                    }
                                }
                            } label: {
                                Image(systemName: "ellipsis.circle").font(.uiTitle3)
                            }
                            .accessibilityLabel("Actions for \(user.username)")
                        }
                    }
                }
            } else if error == nil {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("Everyone")
        .task { await load() }
        .refreshable { await load() }
    }

    private func actions(for user: AccountUser) -> [AdminAction] {
        switch user.status {
        case .pending: [.approve, .reject]
        case .approved: user.isAdmin ? [.disable] : [.makeAdmin, .disable]
        case .rejected: [.approve]
        case .disabled: [.enable]
        }
    }

    private func title(_ action: AdminAction) -> String {
        switch action {
        case .approve: "Approve"
        case .reject: "Reject"
        case .disable: "Disable"
        case .enable: "Enable"
        case .makeAdmin: "Make Admin"
        }
    }

    private func load() async {
        do {
            users = try await account.users()
            error = nil
        } catch {
            self.error = accountMessage(error)
        }
    }

    private func act(_ action: AdminAction, _ user: AccountUser) async {
        busy.insert(user.id)
        defer { busy.remove(user.id) }
        do {
            try await account.perform(action, on: user)
            error = nil
            await load()
        } catch {
            self.error = accountMessage(error)
        }
    }
}

struct AccountUserRow: View {
    let user: AccountUser

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(user.name).font(.uiHeadline)
                if user.isAdmin {
                    Text("Admin").font(.uiCaption2.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 1)
                        .background(.tint.opacity(0.15), in: Capsule())
                }
            }
            Text(([user.name == user.username ? nil : "@" + user.username,
                   user.status == .approved ? nil : user.status.rawValue.capitalized,
                   user.createdAt.map { String($0.prefix(10)) }] as [String?])
                .compactMap { $0 }.joined(separator: " · "))
                .font(.uiCaption).foregroundStyle(.secondary)
        }
    }
}

extension View {
    /// A Done button for views shown in their own sheet.
    func closeButton() -> some View { modifier(CloseButton()) }
}

private struct CloseButton: ViewModifier {
    @Environment(\.dismiss) private var dismiss
    func body(content: Content) -> some View {
        content.toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
    }
}

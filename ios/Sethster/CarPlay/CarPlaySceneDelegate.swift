import CarPlay
import UIKit

/// Entry point for the CarPlay scene (declared in Info.plist's UIApplicationSceneManifest). The phone UI
/// stays on the SwiftUI `WindowGroup`; both scenes share the services in `AppServices`.
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var controller: CarPlayController?

    func templateApplicationScene(_ scene: CPTemplateApplicationScene,
                                  didConnect interfaceController: CPInterfaceController) {
        MainActor.assumeIsolated {
            let c = CarPlayController(interface: interfaceController, model: AppServices.model,
                                      rotation: AppServices.rotation, player: AppServices.player,
                                      radio: AppServices.radio, account: AppServices.account)
            controller = c
            c.start()
        }
    }

    func templateApplicationScene(_ scene: CPTemplateApplicationScene,
                                  didDisconnectInterfaceController interfaceController: CPInterfaceController) {
        MainActor.assumeIsolated {
            controller?.stop()
            controller = nil
        }
    }
}

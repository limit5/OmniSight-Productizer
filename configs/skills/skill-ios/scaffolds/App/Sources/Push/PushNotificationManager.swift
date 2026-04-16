// SKILL-IOS (P7 #292) — APNs push template.
//
// Responsibilities:
//   1. Request UNUserNotification authorization (alert + badge + sound).
//   2. Trigger remote-notification registration after authorization.
//   3. Forward the device token to the OmniSight backend (server then
//      drives APNs sandbox / production via the .p8 auth-key flow —
//      auth keys are NEVER bundled in the app; provisioning profile's
//      `aps-environment` entitlement decides sandbox vs production).
//   4. Surface foreground-presentation + tap-handling delegates.
//
// SECURITY:
//   - The .p8 APNs auth key lives in `backend/secret_store` (P3-equivalent
//     for server-side keys). The app sees ONLY its own device token.
//   - We forward a SHA256-hashed device token to logs, and the raw token
//     ONLY to the backend over HTTPS — never to a third-party analytics SDK.

import Foundation
import UIKit
import UserNotifications
import CryptoKit
import os

@MainActor
final class PushNotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = PushNotificationManager()

    private static let logger = Logger(subsystem: "com.omnisight.skill-ios", category: "Push")

    /// Backend endpoint that records device tokens. Override in tests.
    var deviceTokenEndpoint: URL? = nil

    private(set) var lastTokenFingerprint: String?

    private override init() {
        super.init()
    }

    func requestAuthorizationIfNeeded() async {
        let center = UNUserNotificationCenter.current()
        do {
            let granted = try await center.requestAuthorization(options: [.alert, .badge, .sound])
            if granted {
                await UIApplication.shared.registerForRemoteNotifications()
            } else {
                Self.logger.notice("User declined push authorization.")
            }
        } catch {
            Self.logger.error("Authorization error: \(error.localizedDescription, privacy: .public)")
        }
    }

    func handleDeviceToken(_ deviceToken: Data) async {
        let hexToken = deviceToken.map { String(format: "%02x", $0) }.joined()
        let fingerprint = Self.fingerprint(of: hexToken)
        self.lastTokenFingerprint = fingerprint
        Self.logger.notice("APNs token registered (fingerprint=\(fingerprint, privacy: .public))")
        await forward(token: hexToken)
    }

    private func forward(token: String) async {
        guard let endpoint = deviceTokenEndpoint else {
            Self.logger.debug("No deviceTokenEndpoint configured; skipping forward.")
            return
        }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload = ["device_token": token]
        request.httpBody = try? JSONEncoder().encode(payload)
        do {
            _ = try await URLSession.shared.data(for: request)
        } catch {
            Self.logger.error("Token forward failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    static func fingerprint(of hexToken: String) -> String {
        let digest = SHA256.hash(data: Data(hexToken.utf8))
        return digest.prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound, .badge])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // Hand off to a dedicated router in production; keep the
        // template's surface area small and predictable.
        completionHandler()
    }
}

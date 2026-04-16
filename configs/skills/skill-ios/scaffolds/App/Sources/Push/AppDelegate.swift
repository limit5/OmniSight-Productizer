// SKILL-IOS (P7 #292) — UIKit AppDelegate adapter for APNs callbacks.
//
// SwiftUI doesn't surface the APNs registration callbacks directly,
// so we keep a minimal AppDelegate. PushNotificationManager owns the
// state; the delegate just routes the callbacks.
//
// SECURITY: never log the device token or APNs payload contents
// (token is per-install PII; payload may include user data). Use the
// hashed forwarder in PushNotificationManager.

import UIKit
import UserNotifications
import os

final class AppDelegate: NSObject, UIApplicationDelegate {
    private static let logger = Logger(subsystem: "com.omnisight.skill-ios", category: "AppDelegate")

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = PushNotificationManager.shared

        Task {
            await PushNotificationManager.shared.requestAuthorizationIfNeeded()
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task {
            await PushNotificationManager.shared.handleDeviceToken(deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // os.Logger; never `print()` (P4 ios-swift role anti-pattern).
        Self.logger.error("APNs registration failed: \(error.localizedDescription, privacy: .public)")
    }
}

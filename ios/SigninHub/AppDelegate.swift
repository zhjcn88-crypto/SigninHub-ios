//
//  AppDelegate.swift — 应用入口与窗口装配
//
//  注意：C 入口在 main.m（它要先起 Python 服务线程再进 UIApplicationMain），
//  所以这里**不能**用 @main，否则会出现两个入口符号。
//  用 @objc(SigninHubAppDelegate) 把类名钉成 ObjC 运行时的固定名，
//  main.m 里的 UIApplicationMain(..., @"SigninHubAppDelegate") 才找得到它。
//

import UIKit

@objc(SigninHubAppDelegate)
final class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        BackgroundKeeper.shared.start()
        CheckinScheduler.shared.register()

        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = WebConsoleViewController.make()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}

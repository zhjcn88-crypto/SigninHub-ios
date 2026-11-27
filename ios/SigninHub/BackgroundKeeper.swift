//
//  BackgroundKeeper.swift — iOS 后台存活
//
//  后端 Scheduler 是进程内的 daemon 线程（每 30 秒一查），它不会自己
//  在 App 进后台后继续跑。iOS 的规则是：
//    · 进后台后有约 30 秒的宽限期（beginBackgroundTask 可以续到 ~3 分钟）；
//    · 之后进程被挂起（suspended），所有线程冻结，定时器不再触发。
//
//  TrollStore 装的应用通常是「无签名 + 常驻」形态，配合 TrollStore 的
//  「保持后台」能力会更稳，但 App 侧仍应主动申请后台任务，否则连
//  宽限期都拿不到。
//
//  这里做两件事：
//    1. 进后台时申请 beginBackgroundTask，到期前再续一次；
//    2. 回前台时结束后台任务，避免配额被白白耗尽。
//

import UIKit

final class BackgroundKeeper {

    static let shared = BackgroundKeeper()

    private var taskID: UIBackgroundTaskIdentifier = .invalid
    private var renewTimer: Timer?

    private init() {}

    func start() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(appDidEnterBackground),
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(appWillEnterForeground),
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
    }

    @objc private func appDidEnterBackground() {
        beginTask()
        // 宽限期快结束时再续一次，尽量把签到窗口撑过去
        renewTimer?.invalidate()
        renewTimer = Timer.scheduledTimer(withTimeInterval: 25, repeats: true) { [weak self] _ in
            self?.renewTask()
        }
    }

    @objc private func appWillEnterForeground() {
        renewTimer?.invalidate()
        renewTimer = nil
        endTask()
    }

    private func beginTask() {
        guard taskID == .invalid else { return }
        taskID = UIApplication.shared.beginBackgroundTask(withName: "SigninHub.Engine") { [weak self] in
            // 系统要收回时先干净地结束，否则会被强杀
            self?.endTask()
        }
        NSLog("[SigninHub] 后台任务开始: \(taskID.rawValue)")
    }

    private func renewTask() {
        endTask()
        beginTask()
    }

    private func endTask() {
        guard taskID != .invalid else { return }
        UIApplication.shared.endBackgroundTask(taskID)
        NSLog("[SigninHub] 后台任务结束: \(taskID.rawValue)")
        taskID = .invalid
    }
}

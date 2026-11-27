//
//  CheckinScheduler.swift — iOS 后台唤醒 + 触发签到
//
//  为什么需要这层：
//    后端自带 Scheduler 是进程内的 Python 线程（每 30 秒查一次时间表）。
//    但 iOS 会把后台进程挂起，线程随之冻结 —— 只靠它，用户不打开 App 就
//    不会签到。
//
//    BGTaskScheduler 是 iOS 官方允许「在后台被唤醒执行一小段任务」的机制。
//    这里注册一个 appRefresh 任务，被系统唤醒时直接调 Python 侧的
//    runner.run_all()，走的是和手动点「立即签到」完全相同的代码路径。
//
//  注意：BGTaskScheduler 的触发时机由系统决定（学习用户习惯 + 电量/网络状态），
//  不保证准点。要更稳，TrollStore 环境下建议同时开启「后台 App 刷新」，
//  并每天手动打开一次 App（打开时后端 Scheduler 会补跑）。
//

import BackgroundTasks
import Foundation
import UIKit

final class CheckinScheduler {

    static let shared = CheckinScheduler()

    /// 必须在 Info.plist 的 BGTaskSchedulerPermittedIdentifiers 里登记
    static let refreshTaskID = "com.signhub.app.refresh"

    private init() {}

    /// 在 didFinishLaunching 里调用：注册处理器 + 安排首次执行
    func register() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.refreshTaskID,
            using: nil
        ) { task in
            self.handle(task: task as! BGAppRefreshTask)
        }
        scheduleNext(after: 30 * 60)
    }

    /// 安排下一次后台刷新
    func scheduleNext(after seconds: TimeInterval = 60 * 60) {
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: seconds)
        do {
            try BGTaskScheduler.shared.submit(request)
            NSLog("[SigninHub] 已安排后台刷新，最早 \(Int(seconds / 60)) 分钟后")
        } catch {
            // 模拟器 / 未登记 identifier / 后台刷新被用户关闭时都会到这里
            NSLog("[SigninHub] 安排后台刷新失败: \(error.localizedDescription)")
        }
    }

    private func handle(task: BGAppRefreshTask) {
        // 先把下一次排上，否则这条链就断了
        scheduleNext()

        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1

        task.expirationHandler = {
            queue.cancelAllOperations()
        }

        queue.addOperation {
            let ok = PythonBridge.runScheduledCheckin()
            NSLog("[SigninHub] 后台签到结果: \(ok ? "已执行" : "失败")")
            task.setTaskCompleted(success: ok)
        }
    }
}

/// 调用嵌入式 Python 的桥。实现放在 PythonBridge.m，避免 Swift 直接碰 C API。
enum PythonBridge {

    /// 执行 runner.run_all(trigger="background")，返回是否成功。
    static func runScheduledCheckin() -> Bool {
        let rc = SHBRunCheckin("background")
        return rc == 0
    }
}

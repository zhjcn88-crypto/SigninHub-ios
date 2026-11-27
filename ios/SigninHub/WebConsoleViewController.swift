//
//  WebConsoleViewController.swift
//
//  全屏 WKWebView 加载本机 Python 服务。要点：
//    · Python 服务线程启动与 HTTP 监听有竞态 —— 用轮询 /api/ping 直到通路，
//      期间显示原生加载态，避免用户看到白屏或 "无法连接"。
//    · 只允许 127.0.0.1:8787 留在 WebView 内；点外链一律交给系统浏览器，
//      避免把签到引擎的上下文暴露给外部页面。
//    · 状态栏与安全区适配：页面的 viewport-fit=cover + 黑底导航依赖这个。
//

import UIKit
import WebKit

private let kServerOrigin = "http://127.0.0.1:8787"
private let kPingURL = URL(string: "http://127.0.0.1:8787/api/ping")!

final class WebConsoleViewController: UIViewController {

    private var webView: WKWebView!
    private let placeholder = LoadingViewController()
    private var pollTimer: Timer?
    private var attempts = 0
    private let maxAttempts = 120          // 约 60 秒
    private var didLoadConsole = false

    static func make() -> WebConsoleViewController {
        WebConsoleViewController()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1.0)
        setupWebView()
        setupPlaceholder()
        startPolling()
    }

    // MARK: - 视图装配

    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.defaultWebpagePreferences.allowsContentJavaScript = true

        // 页面是纯本机来源，不做跨站追踪隔离，避免 localStorage 被当成第三方存储
        config.websiteDataStore = .default()

        webView = WKWebView(frame: view.bounds, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .clear

        if #available(iOS 16.4, *) {
            webView.isInspectable = true      // 便于 Safari 远程调试
        }

        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        webView.isHidden = true
    }

    private func setupPlaceholder() {
        addChild(placeholder)
        placeholder.view.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(placeholder.view)
        NSLayoutConstraint.activate([
            placeholder.view.topAnchor.constraint(equalTo: view.topAnchor),
            placeholder.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            placeholder.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            placeholder.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        placeholder.didMove(toParent: self)
    }

    // MARK: - 等待 Python 服务就绪

    private func startPolling() {
        placeholder.setStatus("正在启动签到引擎…")
        ping()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.ping()
        }
    }

    private func ping() {
        guard !didLoadConsole else { return }
        attempts += 1
        if attempts > maxAttempts {
            pollTimer?.invalidate()
            pollTimer = nil
            placeholder.setError(
                "签到引擎启动超时。\n\n"
                + "请先确认 App 已完整解压（首次启动可能需要几秒）。\n"
                + "若反复失败，卸载后重装一次。"
            )
            return
        }

        var request = URLRequest(url: kPingURL)
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self else { return }
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
                && (data?.contains(Data("ok".utf8)) ?? false)
            DispatchQueue.main.async {
                if ok {
                    self.consoleReady()
                } else if self.attempts > 6 {
                    self.placeholder.setStatus("正在启动签到引擎…（首次启动稍慢）")
                }
            }
        }.resume()
    }

    private func consoleReady() {
        guard !didLoadConsole else { return }
        didLoadConsole = true
        pollTimer?.invalidate()
        pollTimer = nil

        guard let url = URL(string: kServerOrigin + "/") else { return }
        webView.isHidden = false
        webView.load(URLRequest(url: url))
    }

    // MARK: - 生命周期

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        if isMovingFromParent { pollTimer?.invalidate() }
    }

    deinit {
        pollTimer?.invalidate()
    }
}

// MARK: - 导航策略

extension WebConsoleViewController: WKNavigationDelegate {

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        // 本机控制台留在 App 内；其余（外链、扫码登录跳转）交给系统
        if url.scheme == "http" || url.scheme == "https" {
            if url.host == "127.0.0.1" || url.host == "localhost" {
                decisionHandler(.allow)
                return
            }
        }
        if navigationAction.navigationType == .linkActivated {
            UIApplication.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        placeholder.view.isHidden = true
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        // 页面本身失败时退回轮询，让用户看到可读原因而不是白屏
        placeholder.view.isHidden = false
        placeholder.setError("页面加载失败：\n\(error.localizedDescription)")
    }
}

extension WebConsoleViewController: WKUIDelegate {

    // WebView 内的 alert / confirm 交给原生弹窗
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "好", style: .default) { _ in completionHandler() })
        present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in completionHandler(true) })
        present(alert, animated: true)
    }
}

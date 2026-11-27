//
//  LoadingViewController.swift — 启动等待页 / 失败提示页
//

import UIKit

final class LoadingViewController: UIViewController {

    private let spinner = UIActivityIndicatorView(style: .large)
    private let statusLabel = UILabel()
    private let iconView = UIImageView()
    private let titleLabel = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1.0)

        iconView.image = UIImage(named: "AppIcon")
            ?? UIImage(systemName: "checkmark.seal.fill")
        iconView.tintColor = UIColor(red: 0.35, green: 0.85, blue: 0.65, alpha: 1)
        iconView.contentMode = .scaleAspectFit
        iconView.translatesAutoresizingMaskIntoConstraints = false

        titleLabel.text = "签到中心"
        titleLabel.font = .systemFont(ofSize: 24, weight: .semibold)
        titleLabel.textColor = .white
        titleLabel.textAlignment = .center
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        spinner.color = UIColor(red: 0.45, green: 0.75, blue: 1.0, alpha: 1)
        spinner.startAnimating()
        spinner.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.text = "正在启动签到引擎…"
        statusLabel.font = .systemFont(ofSize: 15, weight: .regular)
        statusLabel.textColor = UIColor(white: 0.72, alpha: 1)
        statusLabel.textAlignment = .center
        statusLabel.numberOfLines = 0
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        [iconView, titleLabel, spinner, statusLabel].forEach { view.addSubview($0) }

        NSLayoutConstraint.activate([
            iconView.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            iconView.centerYAnchor.constraint(equalTo: view.centerYAnchor, constant: -110),
            iconView.widthAnchor.constraint(equalToConstant: 84),
            iconView.heightAnchor.constraint(equalToConstant: 84),

            titleLabel.topAnchor.constraint(equalTo: iconView.bottomAnchor, constant: 18),
            titleLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            titleLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),

            spinner.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 36),
            spinner.centerXAnchor.constraint(equalTo: view.centerXAnchor),

            statusLabel.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 22),
            statusLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 32),
            statusLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -32),
        ])
    }

    func setStatus(_ text: String) {
        statusLabel.textColor = UIColor(white: 0.72, alpha: 1)
        statusLabel.text = text
        spinner.isHidden = false
        spinner.startAnimating()
    }

    func setError(_ text: String) {
        spinner.stopAnimating()
        spinner.isHidden = true
        statusLabel.textColor = UIColor(red: 1.0, green: 0.55, blue: 0.5, alpha: 1)
        statusLabel.text = text
    }
}

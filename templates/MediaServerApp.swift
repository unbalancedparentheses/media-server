// Media Server: the dashboard and every service's page in one window,
// switched from the toolbar (or Cmd+1…9). Each page keeps its place while
// you're on another. Links to another service open on its page; links
// off this Mac (IMDb, a trailer) open in your browser.
//
// While it runs (closing the window keeps it running), it reads the
// dashboard's status.json every 30 s: the Dock icon's badge counts what
// needs a look (or shows ↓ while something downloads), and a notification
// says when something is ready to watch or newly needs a look (a stuck
// download, the disk filling up, a service down).
//
// Written by setup (mediaserver/steps/macapp.py), compiled on this Mac;
// the services and their addresses come from services.json beside it.
import AppKit
import ServiceManagement
import UserNotifications
import WebKit

struct Service: Decodable {
    let name: String
    let url: String
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSToolbarDelegate, WKNavigationDelegate, WKUIDelegate,
                         UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var services: [Service] = []
    var views: [Int: WKWebView] = [:]
    var current = 0
    let picker = NSSegmentedControl()
    let container = NSView()
    let back = NSButton()
    let forward = NSButton()
    let finder = NSSearchField()

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let file = Bundle.main.url(forResource: "services", withExtension: "json"),
           let data = try? Data(contentsOf: file),
           let list = try? JSONDecoder().decode([Service].self, from: data) {
            services = list
        }
        if services.isEmpty { services = [Service(name: "Home", url: "http://localhost")] }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Media Server"
        window.minSize = NSSize(width: 800, height: 500)
        window.center()
        window.setFrameAutosaveName("MediaServerWindow")
        window.contentView = container
        window.isReleasedWhenClosed = false
        // Dark like the pages (the dashboard's background), not a white toolbar over them
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0x09 / 255.0, green: 0x09 / 255.0, blue: 0x0f / 255.0, alpha: 1)

        picker.segmentCount = services.count
        for (i, s) in services.enumerated() { picker.setLabel(s.name, forSegment: i) }
        picker.trackingMode = .selectOne
        picker.target = self
        picker.action = #selector(picked(_:))
        for (button, symbol, tip, action) in [(back, "chevron.left", "Back", #selector(goBack(_:))),
                                              (forward, "chevron.right", "Forward", #selector(goForward(_:)))] {
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: tip)
            button.bezelStyle = .texturedRounded
            button.toolTip = tip
            button.target = self
            button.action = action
        }

        finder.placeholderString = "Find on page"
        finder.sendsWholeSearchString = true
        finder.target = self
        finder.action = #selector(find(_:))
        finder.widthAnchor.constraint(equalToConstant: 180).isActive = true

        let toolbar = NSToolbar(identifier: "main")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.centeredItemIdentifiers = [.services]
        window.toolbar = toolbar
        window.toolbarStyle = .unified
        window.titleVisibility = .hidden

        buildMenu()
        show(0)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        poll()
        refreshServerStatus()
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.poll(); self?.refreshServerStatus() }
    }

    // Closing the window keeps it running (the badge and notifications);
    // the Dock icon brings the window back, Cmd+Q quits
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // Clicking the Dock icon with the window closed brings it back
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    // ─── Pages ───────────────────────────────────────────────────

    func page(_ i: Int) -> WKWebView {
        if let v = views[i] { return v }
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()   // logins kept between launches
        config.preferences.isElementFullscreenEnabled = true   // full-screen video
        config.mediaTypesRequiringUserActionForPlayback = []
        let v = WKWebView(frame: container.bounds, configuration: config)
        v.autoresizingMask = [.width, .height]
        v.allowsBackForwardNavigationGestures = true
        v.allowsMagnification = true
        v.underPageBackgroundColor = window.backgroundColor   // no white flash while a page loads
        v.navigationDelegate = self
        v.uiDelegate = self
        v.pageZoom = zoom(i)
        if let url = URL(string: services[i].url) { v.load(URLRequest(url: url)) }
        container.addSubview(v)
        views[i] = v
        return v
    }

    func show(_ i: Int, url: URL? = nil) {
        guard services.indices.contains(i) else { return }
        current = i
        picker.selectedSegment = i
        let v = page(i)
        for (j, other) in views { other.isHidden = j != i }
        v.frame = container.bounds
        if let url = url { v.load(URLRequest(url: url)) }
        window.title = i == 0 ? "Media Server" : "Media Server · \(services[i].name)"
        window.makeFirstResponder(v)
        updateButtons()
    }

    func updateButtons() {
        let v = views[current]
        back.isEnabled = v?.canGoBack ?? false
        forward.isEnabled = v?.canGoForward ?? false
    }

    // Which service a URL belongs to (same port on this Mac), if any
    func service(for url: URL) -> Int? {
        guard let host = url.host, ["localhost", "127.0.0.1", "::1"].contains(host) else { return nil }
        let port = url.port ?? (url.scheme == "https" ? 443 : 80)
        return services.firstIndex { s in
            guard let u = URL(string: s.url) else { return false }
            return (u.port ?? (u.scheme == "https" ? 443 : 80)) == port
        }
    }

    func isLocal(_ url: URL) -> Bool {
        guard let host = url.host else { return true }
        return ["localhost", "127.0.0.1", "::1"].contains(host) || host.hasSuffix(".local")
    }

    // ─── Links ───────────────────────────────────────────────────

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, action.targetFrame?.isMainFrame ?? true,
              ["http", "https"].contains(url.scheme ?? "") else { return decisionHandler(.allow) }
        if !isLocal(url) {
            // Off this Mac: your browser (but a page's own redirects stay)
            if action.navigationType == .linkActivated { NSWorkspace.shared.open(url); return decisionHandler(.cancel) }
            return decisionHandler(.allow)
        }
        if action.navigationType == .linkActivated, let target = service(for: url),
           let from = views.first(where: { $0.value === webView })?.key, target != from {
            decisionHandler(.cancel)
            show(target, url: url)
            return
        }
        decisionHandler(.allow)
    }

    // target="_blank" and window.open: on the right page, in this window
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = action.request.url else { return nil }
        if !isLocal(url) { NSWorkspace.shared.open(url) }
        else if let target = service(for: url) { show(target, url: url) }
        else { webView.load(action.request) }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { updateButtons() }
    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { updateButtons() }

    // A service that isn't answering: say so, with a retry
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let code = (error as NSError).code
        if code == NSURLErrorCancelled { return }
        let name = views.first(where: { $0.value === webView }).map { services[$0.key].name } ?? "This page"
        let retry = ((error as NSError).userInfo[NSURLErrorFailingURLErrorKey] as? URL)?.absoluteString ?? ""
        webView.loadHTMLString("""
            <html><body style="background:#0e0e14;color:#ccc;font:15px -apple-system;display:flex;align-items:center;justify-content:center;height:90vh">
            <div style="text-align:center"><h2 style="color:#e5b84b">\(name) isn't answering</h2>
            <p>It may still be starting. Check the dashboard's Manage page.</p>
            <p><a style="color:#e5b84b" href="\(retry)">Try again</a></p></div></body></html>
            """, baseURL: nil)
    }

    // Dialogs the pages use (confirm before deleting, alerts, file pickers)
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // ─── Status: the Dock badge and notifications ────────────────

    let defaults = UserDefaults.standard
    var polled = false

    func poll() {
        guard let url = URL(string: services[0].url + "/status.json") else { return }
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let data = data, (response as? HTTPURLResponse)?.statusCode == 200,
                  let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                NSLog("Media Server: couldn't read %@: %@", url.absoluteString,
                      error?.localizedDescription ?? "HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)")
                DispatchQueue.main.async { NSApp.dockTile.badgeLabel = self?.polled == true ? "?" : nil }
                return
            }
            DispatchQueue.main.async { self?.update(status) }
        }.resume()
    }

    // What needs a look, ignoring passing information; the key leaves out
    // numbers ("990 GB free" and "980 GB free" are the same warning)
    static func problems(_ status: [String: Any]) -> [(key: String, level: String, text: String, action: String)] {
        let items = status["attention"] as? [[String: Any]] ?? []
        return items.compactMap { item in
            let level = item["level"] as? String ?? ""
            guard level == "error" || level == "warn", let text = item["text"] as? String else { return nil }
            let key = text.replacingOccurrences(of: "[0-9.,]+", with: "#", options: .regularExpression)
            return (key, level, text, item["action"] as? String ?? "")
        }
    }

    func update(_ status: [String: Any]) {
        let problems = AppDelegate.problems(status)
        let downloading = (status["downloads"] as? [String: Any])?["downloading"] as? Int ?? 0
        NSApp.dockTile.badgeLabel = !problems.isEmpty ? "\(problems.count)" : downloading > 0 ? "↓\(downloading)" : nil

        // Notified once each: a problem until it goes away, a title for good
        let known = Set(defaults.stringArray(forKey: "notifiedProblems") ?? [])
        let current = Set(problems.map { $0.key })
        let latest = (status["latest"] as? [[String: Any]] ?? []).compactMap { item -> (id: String, title: String)? in
            guard let id = item["id"] as? String, let title = item["title"] as? String else { return nil }
            let detail = item["detail"] as? String ?? ""
            return (id, detail.isEmpty ? title : "\(title) (\(detail))")
        }
        var seen = defaults.stringArray(forKey: "notifiedReady") ?? []
        let firstRun = defaults.object(forKey: "notifiedReady") == nil
        if !firstRun {   // the first time, what's there already isn't news
            for p in problems where !known.contains(p.key) {
                notify(p.level == "error" ? "Problem: \(p.text)" : "Needs a look: \(p.text)", p.action, open: services[0].url + "/#manage")
            }
            for item in latest where !seen.contains(item.id) {
                notify("Ready to watch", item.title, open: watchURL(item.id))
            }
        }
        seen = Array((latest.map { $0.id } + seen).prefix(200))
        defaults.set(seen, forKey: "notifiedReady")
        defaults.set(Array(current), forKey: "notifiedProblems")
        polled = true
    }

    func watchURL(_ id: String) -> String {
        let watch = services.first { $0.name == "Watch" }?.url ?? services[0].url
        return watch + "?open=item/" + id
    }

    func notify(_ title: String, _ body: String, open: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.userInfo = ["open": open]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    // Clicking a notification: the window, on the page it's about
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let open = response.notification.request.content.userInfo["open"] as? String, let url = URL(string: open) {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            show(service(for: url) ?? 0, url: url)
        }
        done()
    }

    // Shown even while the app is in front
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    // ─── The services: stop, start, restart everything ───────────
    // The launchd agents setup installed (~/Library/LaunchAgents/
    // org.media-server.*.plist), driven with launchctl as the user they
    // belong to. Stopped ones start again at the next login.

    let serverStatus = NSMenuItem(title: "Checking the services…", action: nil, keyEquivalent: "")

    func agents() -> [(label: String, plist: String)] {
        let dir = NSHomeDirectory() + "/Library/LaunchAgents"
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []
        return names.filter { $0.hasPrefix("org.media-server.") && $0.hasSuffix(".plist") }.sorted()
            .map { (String($0.dropLast(".plist".count)), dir + "/" + $0) }
    }

    @discardableResult
    func launchctl(_ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return -1 }
        p.waitUntilExit()
        return p.terminationStatus
    }

    var domain: String { "gui/\(getuid())" }

    func loaded(_ label: String) -> Bool { launchctl(["print", "\(domain)/\(label)"]) == 0 }

    func refreshServerStatus() {
        DispatchQueue.global().async { [self] in
            let all = agents()
            let up = all.filter { loaded($0.label) }.count
            DispatchQueue.main.async {
                self.serverStatus.title = all.isEmpty ? "No services installed"
                    : up == all.count ? "All \(all.count) services running"
                    : up == 0 ? "All services stopped" : "\(up) of \(all.count) services running"
            }
        }
    }

    func confirm(_ title: String, _ text: String, _ button: String, then action: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { if $0 == .alertFirstButtonReturn { action() } }
    }

    // Runs off the main thread (stopping waits for each service), then says
    // how it went and reloads the page once things are back
    func serverAction(_ doing: String, _ done: String, reload: Bool, _ work: @escaping ((label: String, plist: String)) -> Bool) {
        serverStatus.title = doing
        window.title = "Media Server · \(doing)"
        DispatchQueue.global().async { [self] in
            let failed = agents().filter { !work($0) }.map { $0.label.replacingOccurrences(of: "org.media-server.", with: "") }
            DispatchQueue.main.async {
                self.window.title = self.current == 0 ? "Media Server" : "Media Server · \(self.services[self.current].name)"
                self.refreshServerStatus()
                if !failed.isEmpty {
                    let alert = NSAlert()
                    alert.messageText = "Some services didn't respond"
                    alert.informativeText = failed.joined(separator: ", ") + "\n\nTry again, or run nix run .#status in the media-server folder."
                    alert.beginSheetModal(for: self.window)
                } else {
                    self.notify(done, "", open: self.services[0].url)
                }
                if reload {
                    // The services take a moment to answer again
                    DispatchQueue.main.asyncAfter(deadline: .now() + 15) { self.views.values.forEach { $0.reload() } }
                }
            }
        }
    }

    @objc func restartAll(_ sender: Any?) {
        confirm("Restart everything?", "Every service restarts: what's playing stops for a moment, and downloads resume on their own.",
                "Restart") { [self] in
            serverAction("Restarting…", "Everything restarted", reload: true) { agent in
                self.loaded(agent.label) ? self.launchctl(["kickstart", "-k", "\(self.domain)/\(agent.label)"]) == 0
                    : self.launchctl(["bootstrap", self.domain, agent.plist]) == 0
            }
        }
    }

    @objc func stopAll(_ sender: Any?) {
        confirm("Stop everything?", "Nothing plays, downloads or answers until you start it again (Server → Start Everything) or log in again. The Mac may sleep.",
                "Stop") { [self] in
            serverAction("Stopping…", "Everything stopped", reload: false) { agent in
                !self.loaded(agent.label) || self.launchctl(["bootout", "\(self.domain)/\(agent.label)"]) == 0
            }
        }
    }

    // Update: in Terminal, so its progress (and a rollback, if the new
    // version's checks fail) can be followed
    @objc func updateServer(_ sender: Any?) {
        guard let file = Bundle.main.url(forResource: "server", withExtension: "json"),
              let data = try? Data(contentsOf: file),
              let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let repo = info["repo"] as? String, !repo.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Update from the media-server folder"
            alert.informativeText = "Run nix run .#update in your media-server checkout."
            alert.beginSheetModal(for: window)
            return
        }
        confirm("Update Media Server?", "It backs up, gets the latest version and sets it up again, in Terminal. If the new version's checks fail, it goes back to this one on its own.",
                "Update") {
            let script = FileManager.default.temporaryDirectory.appendingPathComponent("media-server-update.command")
            let quoted = "'" + repo.replacingOccurrences(of: "'", with: "'\\''") + "'"
            let text = "#!/bin/zsh -l\ncd \(quoted) && nix run .#update\necho\nread -k1 '?Done. Press a key to close.'\n"
            try? text.write(to: script, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
            NSWorkspace.shared.open(script)
        }
    }

    @objc func startAll(_ sender: Any?) {
        serverAction("Starting…", "Everything started", reload: true) { agent in
            self.loaded(agent.label) || self.launchctl(["bootstrap", self.domain, agent.plist]) == 0
        }
    }

    // ─── Toolbar ─────────────────────────────────────────────────

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.navigation, .services, .reload, .find, .flexibleSpace]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.navigation, .flexibleSpace, .services, .flexibleSpace, .find, .reload]
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: id)
        switch id {
        case .services:
            item.view = picker
            item.label = "Pages"
        case .navigation:
            let stack = NSStackView(views: [back, forward])
            stack.spacing = 2
            item.view = stack
            item.label = "Back/Forward"
        case .find:
            item.view = finder
            item.label = "Find"
        case .reload:
            let button = NSButton(image: NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Reload")!,
                                  target: self, action: #selector(reload(_:)))
            button.bezelStyle = .texturedRounded
            button.toolTip = "Reload"
            item.view = button
            item.label = "Reload"
        default:
            return nil
        }
        return item
    }

    // ─── Actions ─────────────────────────────────────────────────

    @objc func picked(_ sender: NSSegmentedControl) { show(sender.selectedSegment) }
    @objc func pickByKey(_ sender: NSMenuItem) { show(sender.tag) }
    @objc func goBack(_ sender: Any?) { views[current]?.goBack() }
    @objc func goForward(_ sender: Any?) { views[current]?.goForward() }
    @objc func reload(_ sender: Any?) { views[current]?.reload() }
    // ─── Zoom (remembered per page) and Find ─────────────────────

    func zoom(_ i: Int) -> CGFloat {
        let z = defaults.double(forKey: "zoom." + services[i].name)
        return z > 0 ? CGFloat(z) : 1
    }

    func setZoom(_ z: CGFloat) {
        guard let v = views[current] else { return }
        let clamped = min(max(z, 0.5), 3)
        v.pageZoom = clamped
        defaults.set(Double(clamped), forKey: "zoom." + services[current].name)
    }

    @objc func zoomIn(_ sender: Any?) { setZoom((views[current]?.pageZoom ?? 1) + 0.1) }
    @objc func zoomOut(_ sender: Any?) { setZoom((views[current]?.pageZoom ?? 1) - 0.1) }
    @objc func actualSize(_ sender: Any?) { setZoom(1) }

    @objc func showFind(_ sender: Any?) { window.makeFirstResponder(finder) }
    @objc func find(_ sender: Any?) { findNext(backwards: NSEvent.modifierFlags.contains(.shift)) }
    @objc func findNextItem(_ sender: Any?) { findNext(backwards: false) }
    @objc func findPreviousItem(_ sender: Any?) { findNext(backwards: true) }

    func findNext(backwards: Bool) {
        let text = finder.stringValue
        guard !text.isEmpty, let v = views[current] else { return }
        let config = WKFindConfiguration()
        config.backwards = backwards
        config.wraps = true
        v.find(text, configuration: config) { [weak self] result in
            if !result.matchFound { self?.finder.textColor = .systemRed } else { self?.finder.textColor = .labelColor }
        }
    }

    // ─── Open at Login (so the badge and notifications keep going) ──

    @objc func toggleLogin(_ sender: NSMenuItem) {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled { try service.unregister() } else { try service.register() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Couldn't change Open at Login"
            alert.informativeText = "\(error.localizedDescription)\n\nYou can add Media Server in System Settings → General → Login Items."
            alert.beginSheetModal(for: window)
        }
        if service.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(toggleLogin(_:)) {
            item.state = SMAppService.mainApp.status == .enabled ? .on : .off
        }
        return true
    }

    @objc func goHome(_ sender: Any?) {
        // The page's own start (the dashboard's Home, Sonarr's series list…)
        if let url = URL(string: services[current].url) { views[current]?.load(URLRequest(url: url)) }
    }

    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        func item(_ title: String, _ action: Selector?, _ key: String, _ mods: NSEvent.ModifierFlags = .command,
                  target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        add("Media Server", [
            item("About Media Server", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Open at Login", #selector(toggleLogin(_:)), "", target: self),
            .separator(),
            item("Hide Media Server", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Quit Media Server", #selector(NSApplication.terminate(_:)), "q"),
        ])
        add("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
            .separator(),
            item("Find…", #selector(showFind(_:)), "f", target: self),
            item("Find Next", #selector(findNextItem(_:)), "g", target: self),
            item("Find Previous", #selector(findPreviousItem(_:)), "g", [.command, .shift], target: self),
        ])
        var go = [
            item("Back", #selector(goBack(_:)), "[", target: self),
            item("Forward", #selector(goForward(_:)), "]", target: self),
            item("Reload", #selector(reload(_:)), "r", target: self),
            item("Start of This Page", #selector(goHome(_:)), "h", [.command, .shift], target: self),
            .separator(),
        ]
        for (i, s) in services.enumerated() where i < 9 {
            let m = item(s.name, #selector(pickByKey(_:)), "\(i + 1)", target: self)
            m.tag = i
            go.append(m)
        }
        add("Go", go)
        serverStatus.isEnabled = false
        add("Server", [
            serverStatus,
            .separator(),
            item("Restart Everything…", #selector(restartAll(_:)), "r", [.command, .shift], target: self),
            item("Stop Everything…", #selector(stopAll(_:)), "", target: self),
            item("Start Everything", #selector(startAll(_:)), "", target: self),
            .separator(),
            item("Update…", #selector(updateServer(_:)), "", target: self),
        ])
        add("View", [
            item("Actual Size", #selector(actualSize(_:)), "0", target: self),
            item("Zoom In", #selector(zoomIn(_:)), "=", target: self),
            item("Zoom Out", #selector(zoomOut(_:)), "-", target: self),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        add("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

extension NSToolbarItem.Identifier {
    static let services = NSToolbarItem.Identifier("services")
    static let navigation = NSToolbarItem.Identifier("navigation")
    static let reload = NSToolbarItem.Identifier("reload")
    static let find = NSToolbarItem.Identifier("find")
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

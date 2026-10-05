// Media Server: the dashboard and every service's page in one window,
// switched from the toolbar (or Cmd+1…9). Each page keeps its place while
// you're on another. Links to another service open on its page; links
// off this Mac (IMDb, a trailer) open in your browser.
//
// Written by setup (mediaserver/steps/macapp.py), compiled on this Mac;
// the services and their addresses come from services.json beside it.
import AppKit
import WebKit

struct Service: Decodable {
    let name: String
    let url: String
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSToolbarDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var services: [Service] = []
    var views: [Int: WKWebView] = [:]
    var current = 0
    let picker = NSSegmentedControl()
    let container = NSView()
    let back = NSButton()
    let forward = NSButton()

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
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

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
        v.navigationDelegate = self
        v.uiDelegate = self
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

    // ─── Toolbar ─────────────────────────────────────────────────

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.navigation, .services, .reload, .flexibleSpace]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.navigation, .flexibleSpace, .services, .flexibleSpace, .reload]
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
        add("View", [
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
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

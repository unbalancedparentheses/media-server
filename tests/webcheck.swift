// webcheck URL STEPS.json: loads a page in an off-screen WebKit view (the
// engine of Safari and the Media Server app) and runs the steps, each
// {"js": "...", "wait": ms}: the script's value (JSON-encodable) after
// waiting. Prints [results] as JSON. Used by tests/test_browser.py.
import AppKit
import WebKit

let args = CommandLine.arguments
let url = URL(string: args[1])!
let steps = (try? JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: args[2])))) as? [[String: Any]] ?? []

final class Runner: NSObject, WKNavigationDelegate {
    let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1400, height: 1000))
    var results: [Any] = []
    var started = false

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !started else { return }
        started = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.next(0) }   // the page fetches its data
    }

    func next(_ i: Int) {
        guard i < steps.count else {
            let data = try! JSONSerialization.data(withJSONObject: results, options: [.fragmentsAllowed])
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
        let js = steps[i]["js"] as? String ?? "null"
        let wait = (steps[i]["wait"] as? Double ?? 300) / 1000
        // Wrapped so every step's value comes back as JSON text
        view.evaluateJavaScript("JSON.stringify((function(){ \(js) })())") { value, error in
            if let text = value as? String, let data = text.data(using: .utf8),
               let parsed = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) {
                self.results.append(parsed)
            } else {
                self.results.append(["error": error?.localizedDescription ?? "no value"])
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + wait) { self.next(i + 1) }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let runner = Runner()
runner.view.navigationDelegate = runner
runner.view.load(URLRequest(url: url))
DispatchQueue.main.asyncAfter(deadline: .now() + 90) { print("timed out"); exit(2) }
app.run()

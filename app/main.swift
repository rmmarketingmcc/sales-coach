// Sales Coach: the app around the coach's live window (engine/coach.py serves it on http://127.0.0.1:8765).
//
// - a small window you can move, resize, minimise and close (closing only hides it); it stays on top of
//   other windows, also a full-screen call, unless "Hold øverst" is switched off
// - text size: ⌘+ / ⌘- / ⌘0, remembered between runs
// - a menu bar item: "Coach", "● 46%" (your talk share) during a call, a countdown near the end and to
//   the next meeting; its menu has the coach's own switches
// - your meetings: reads the Mac's Calendar (every calendar you have added in System Settings, so
//   Google, Outlook/Exchange and iCloud alike) and writes the next week to calendar.json for the coach
// - opening it never steals focus (the coach starts it with `open -g`); sharingType .none keeps it out
//   of screen recordings and screen sharing where macOS honours that
// - the window is a non-activating panel, so it can join a call's full-screen Space and never takes focus
// The page switches between a compact and a full layout by the window's size.
//
// Build: ./build.sh (swiftc, ad-hoc signed)

import Cocoa
import EventKit
import WebKit

let prefs = UserDefaults.standard
let home = FileManager.default.homeDirectoryForCurrentUser
let port = prefs.integer(forKey: "port") > 0 ? prefs.integer(forKey: "port") : 8765   // tests: defaults write dk.rmmarketing.salescoach port 8766
let base = "http://127.0.0.1:\(port)"
let pageURL = URL(string: "\(base)/?widget=1")!
let menubarURL = URL(string: "\(base)/state?from=menubar")!   // does not count as "window open"
let recordings = prefs.string(forKey: "recordings").map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent("CallRecordings")
let dataDir = prefs.string(forKey: "data").map { URL(fileURLWithPath: $0) }
    ?? home.appendingPathComponent("Library/Application Support/Sales Coach")
let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
let issuesURL = URL(string: "https://github.com/rmmarketingmcc/sales-coach/issues/new")!
// A phone call on speaker: the recorder records while this file exists (recorder/main.swift)
let phoneFlag = recordings.appendingPathComponent(".phone_call")
func phoneCallOn() -> Bool { FileManager.default.fileExists(atPath: phoneFlag.path) }

// MARK: - Calendar

/// The Mac's Calendar to calendar.json (engine/calendar_feed.py has the format): yesterday to a week ahead,
/// every 5 minutes and whenever the calendar changes. Without access the file is not written, and the
/// coach treats the calendar as unknown (it still coaches every call, without the invite's details).
final class CalendarFeed {
    let store = EKEventStore()
    var timer: Timer?
    let stamp: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    var allowed: Bool {
        let status = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) { return status == .fullAccess }
        return status.rawValue == 3   // .authorized before macOS 14
    }
    var undecided: Bool { EKEventStore.authorizationStatus(for: .event) == .notDetermined }

    func start() {
        NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: store, queue: .main) { [weak self] _ in
            self?.write()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.write() }
        if undecided { ask() } else { write() }
    }

    func ask() {
        let done: (Bool, Error?) -> Void = { [weak self] granted, _ in
            DispatchQueue.main.async {
                self?.store.reset()
                if granted { self?.write() }
            }
        }
        if #available(macOS 14.0, *) { store.requestFullAccessToEvents(completion: done) }
        else { store.requestAccess(to: .event, completion: done) }
    }

    func write() {
        guard allowed else { return }
        let now = Date()
        let predicate = store.predicateForEvents(withStart: now.addingTimeInterval(-86_400),
                                                 end: now.addingTimeInterval(7 * 86_400), calendars: nil)
        let events = store.events(matching: predicate).filter { !$0.isAllDay }.map(event)
        let out: [String: Any] = ["updated": stamp.string(from: now), "source": "mac-calendar", "events": events]
        do {
            try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: dataDir.appendingPathComponent("calendar.json"), options: .atomic)
        } catch {
            NSLog("Sales Coach: calendar.json not written: \(error)")
        }
    }

    func person(_ p: EKParticipant) -> [String: Any] {
        let url = p.url.absoluteString
        let email = url.lowercased().hasPrefix("mailto:") ? String(url.dropFirst(7)).removingPercentEncoding ?? "" : ""
        let status: String
        switch p.participantStatus {
        case .accepted: status = "accepted"
        case .declined: status = "declined"
        case .tentative: status = "tentative"
        default: status = "pending"
        }
        return ["email": email.lowercased(), "name": p.name ?? "", "self": p.isCurrentUser, "status": status]
    }

    func event(_ e: EKEvent) -> [String: Any] {
        var people = (e.attendees ?? []).map(person)
        if let organizer = e.organizer {
            let o = person(organizer)
            if !people.contains(where: { ($0["email"] as? String) == (o["email"] as? String) && !(o["email"] as? String ?? "").isEmpty }) {
                var org = o
                org["status"] = "accepted"
                people.append(org)
            }
        }
        // one id per occurrence: a repeating meeting shares its eventIdentifier
        let id = "\(e.eventIdentifier ?? e.calendarItemIdentifier)@\(Int(e.startDate.timeIntervalSince1970))"
        var out: [String: Any] = [
            "id": id, "title": e.title ?? "", "start": stamp.string(from: e.startDate), "end": stamp.string(from: e.endDate),
            "notes": e.notes ?? "", "location": e.location ?? "", "status": e.status == .canceled ? "cancelled" : "confirmed",
            "attendees": people, "calendar": e.calendar?.title ?? "",
        ]
        if let modified = e.lastModifiedDate { out["updated"] = stamp.string(from: modified) }
        if let link = e.url?.absoluteString, link.hasPrefix("https://") { out["meet"] = link }
        return out
    }
}

// MARK: - App

// A panel can be typed in (feedback box) without activating the app; the app is then not active, so
// ⌘+ / ⌘- / ⌘W / ⌘M reach the main menu from here.
final class CoachPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        NSApp.mainMenu?.performKeyEquivalent(with: event) == true || super.performKeyEquivalent(with: event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate,
                         NSMenuDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var item: NSStatusItem!
    let calendar = CalendarFeed()
    var settings: [String: Any] = [:]   // the coach's switches, from the poll
    var callKey = ""                    // the call the window was last placed for
    var upcoming: [String: Any]? = nil  // the next meeting between calls (title, in_s, meet), from the poll

    var zoom: Double {
        get { prefs.object(forKey: "zoom") as? Double ?? 1.0 }
        set { prefs.set(newValue, forKey: "zoom"); web.pageZoom = newValue }
    }
    var onTop: Bool {
        get { prefs.object(forKey: "onTop") as? Bool ?? true }
        set { prefs.set(newValue, forKey: "onTop"); window.level = newValue ? .statusBar : .normal }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // the Dock tile from the bundle's own icon, not a copy the Dock kept from an older build
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"), let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        let frame = NSRect(x: 0, y: 0, width: 340, height: 380)
        let panel = CoachPanel(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                                               .nonactivatingPanel],
                               backing: .buffered, defer: false)
        panel.hidesOnDeactivate = false   // a panel's default: it would vanish whenever another app is used
        window = panel
        window.title = "Sales Coach"
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.sharingType = .none
        window.minSize = NSSize(width: 240, height: 150)
        window.delegate = self
        window.isReleasedWhenClosed = false

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "copilot")
        web = WKWebView(frame: frame, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.pageZoom = zoom
        window.contentView = web
        window.level = onTop ? .statusBar : .normal

        if !window.setFrameUsingName("SalesCoachWindow"), let screen = NSScreen.main {
            let v = screen.visibleFrame   // first time: top right corner
            window.setFrameOrigin(NSPoint(x: v.maxX - frame.width - 24, y: v.maxY - frame.height - 24))
        }
        window.setFrameAutosaveName("SalesCoachWindow")
        web.load(URLRequest(url: pageURL))
        // --background: started at login or by the coach, so only the menu bar item shows until a call
        if !CommandLine.arguments.contains("--background") { window.orderFrontRegardless() }

        NSApp.mainMenu = mainMenu()
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Coach"
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        refreshMenubar()
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refreshMenubar() }
        calendar.start()
    }

    // ---- menus

    func entry(_ title: String, _ action: Selector?, _ key: String = "", on: Bool = false, tag: Any? = nil) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        it.target = self
        it.state = on ? .on : .off
        it.representedObject = tag
        return it
    }

    func mainMenu() -> NSMenu {
        let bar = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(entry("Start/stop telefonopkald", #selector(togglePhone), "p"))
        appMenu.addItem(entry("Skjul Sales Coach", #selector(hideWindow), "h"))
        appMenu.addItem(.separator())
        appMenu.addItem(entry("Afslut Sales Coach", #selector(quit), "q"))
        let view = NSMenu(title: "Vis")
        view.addItem(entry("Større tekst", #selector(bigger), "+"))
        let biggerEq = entry("Større tekst", #selector(bigger), "=")   // ⌘= is ⌘+ without shift on many keyboards
        biggerEq.isHidden = true
        view.addItem(biggerEq)
        view.addItem(entry("Mindre tekst", #selector(smaller), "-"))
        view.addItem(entry("Normal størrelse", #selector(actualSize), "0"))
        view.addItem(.separator())
        view.addItem(entry("Hold øverst", #selector(toggleOnTop), "t"))
        let win = NSMenu(title: "Vindue")
        win.addItem(entry("Minimér", #selector(minimize), "m"))
        win.addItem(entry("Luk", #selector(hideWindow), "w"))
        for (title, sub) in [("Sales Coach", appMenu), ("Vis", view), ("Vindue", win)] {
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            top.submenu = sub
            bar.addItem(top)
        }
        view.delegate = self
        return bar
    }

    // The menu bar item's menu (and the View menu's tick) are built fresh each time they open.
    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu.title == "Vis" {
            menu.items.first { $0.action == #selector(toggleOnTop) }?.state = onTop ? .on : .off
            return
        }
        menu.removeAllItems()
        if let up = upcoming, let link = up["meet"] as? String, !link.isEmpty, let inS = up["in_s"] as? Int {
            let when = inS > 0 ? "om \(inS / 60) min" : "i gang"
            menu.addItem(entry("Deltag: \(up["title"] as? String ?? "næste møde") (\(when))", #selector(joinNext), tag: link))
            menu.addItem(.separator())
        }
        menu.addItem(entry(phoneCallOn() ? "Stop telefonopkald" : "Start telefonopkald (højttaler på)",
                           #selector(togglePhone), on: phoneCallOn()))
        menu.addItem(.separator())
        menu.addItem(entry(window.isVisible && !window.isMiniaturized ? "Skjul vinduet" : "Vis vinduet", #selector(toggle)))
        menu.addItem(.separator())
        menu.addItem(entry("Større tekst", #selector(bigger)))
        menu.addItem(entry("Mindre tekst", #selector(smaller)))
        menu.addItem(entry("Hold øverst", #selector(toggleOnTop), on: onTop))
        menu.addItem(.separator())
        let enabled = (settings["enabled"] as? Bool) ?? true
        menu.addItem(entry(enabled ? "Coachen er til (klik for at slå fra)" : "Coachen er fra (klik for at slå til)",
                           #selector(power), on: enabled))
        let mode = (settings["auto_open"] as? String) ?? "all"
        let auto = entry("Åbner selv", nil)
        let sub = NSMenu()
        for (key, label) in [("all", "Ved hvert opkald"), ("calendar", "Kun kalendermøder"), ("never", "Aldrig, jeg åbner selv")] {
            sub.addItem(entry(label, #selector(setAuto(_:)), on: mode == key, tag: key))
        }
        auto.submenu = sub
        menu.addItem(auto)
        let join = (settings["auto_join"] as? Bool) ?? true
        menu.addItem(entry("Åbn mødelinket 2 min før næste møde", #selector(toggleJoin), on: join))
        if !calendar.allowed {
            menu.addItem(entry("Giv adgang til Kalender (så coachen kender dine møder)...", #selector(openCalendarAccess)))
        }
        menu.addItem(.separator())
        menu.addItem(entry("Rapportér et problem...", #selector(reportProblem)))
        let about = entry("Sales Coach \(appVersion), lavet af RM Marketing", nil)
        about.isEnabled = false
        menu.addItem(about)
        menu.addItem(entry("Afslut appen (coachen kører videre)", #selector(quit)))
    }

    // ---- actions

    @objc func toggle() {
        if window.isVisible && !window.isMiniaturized { window.orderOut(nil) } else { show() }
    }

    func show() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.orderFrontRegardless()
        web.evaluateJavaScript("typeof tick === 'function' && tick()", completionHandler: nil)
    }

    @objc func togglePhone() {
        if phoneCallOn() {
            try? FileManager.default.removeItem(at: phoneFlag)
        } else {
            FileManager.default.createFile(atPath: phoneFlag.path, contents: Data())
            show()
        }
    }

    @objc func hideWindow() { window.orderOut(nil) }
    @objc func minimize() { window.miniaturize(nil) }
    @objc func bigger() { zoom = min(2.5, (zoom * 1.1 * 100).rounded() / 100) }
    @objc func smaller() { zoom = max(0.6, (zoom / 1.1 * 100).rounded() / 100) }
    @objc func actualSize() { zoom = 1.0 }
    @objc func toggleOnTop() { onTop = !onTop }
    @objc func power() { post("/settings", ["enabled": !((settings["enabled"] as? Bool) ?? true)]) }
    @objc func quit() { NSApp.terminate(nil) }

    @objc func joinNext(_ sender: NSMenuItem) {
        if let link = sender.representedObject as? String, let url = URL(string: link) { NSWorkspace.shared.open(url) }
    }
    @objc func toggleJoin() { post("/settings", ["auto_join": !((settings["auto_join"] as? Bool) ?? true)]) }

    @objc func openCalendarAccess() {
        if calendar.undecided {
            calendar.ask()
        } else {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")!)
        }
    }

    // A GitHub issue with the technical facts filled in. Issues are public: never your name, company,
    // meetings or words, only versions and switches.
    @objc func reportProblem() {
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        URLSession.shared.dataTask(with: URL(string: "\(base)/health")!) { data, _, _ in
            var coach = "not answering"
            if let data, let h = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let s = h["settings"] as? [String: Any] ?? [:]
                coach = "\(h["version"] ?? "?"), AI \(h["ai"] ?? "?"), in a call: \(h["call"] ?? "?"), "
                    + "language \(s["language"] ?? "?"), on: \(s["enabled"] ?? "?"), opens: \(s["auto_open"] ?? "?"), "
                    + "add-ons: \((s["addons"] as? [Any])?.count ?? 0)"
            }
            let body = "What happened:\n\n\nWhat you expected:\n\n\n---\nSales Coach \(appVersion), macOS \(os)\n"
                + "Calendar access: \(self.calendar.allowed ? "yes" : "no")\nCoach: \(coach)\n"
            var parts = URLComponents(url: issuesURL, resolvingAgainstBaseURL: false)!
            parts.queryItems = [URLQueryItem(name: "body", value: body)]
            DispatchQueue.main.async { if let url = parts.url { NSWorkspace.shared.open(url) } }
        }.resume()
    }

    @objc func setAuto(_ sender: NSMenuItem) {
        if let key = sender.representedObject as? String { post("/settings", ["auto_open": key]) }
    }

    func post(_ path: String, _ body: [String: Any]) {
        var req = URLRequest(url: URL(string: base + path)!)
        req.httpMethod = "POST"
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refreshMenubar() }
        }.resume()
    }

    // The menu bar: your talk share during a call, so it is visible even with the window hidden.
    func refreshMenubar() {
        URLSession.shared.dataTask(with: menubarURL) { [weak self] data, _, _ in
            var title = "Coach"
            var tint: NSColor? = nil   // orange: the last 10 min; red: over time, or the next call is 5 min away
            var settings: [String: Any]? = nil
            var newCall: String? = nil
            var up: [String: Any]? = nil
            let mm = { (s: Int) in "\(s / 60):" + String(format: "%02d", s % 60) }
            if let data, let st = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                settings = st["settings"] as? [String: Any]
                up = st["upcoming"] as? [String: Any]
                if (st["active"] as? Bool) == true {
                    let share = (st["share"] as? Int).map { "\($0)%" } ?? "live"
                    title = "● \(share)"
                    if let cd = st["countdown"] as? [String: Any], let left = cd["left_s"] as? Int {
                        let next = cd["next"] as? [String: Any]
                        if let inS = next?["in_s"] as? Int, inS <= 300 {
                            title = inS > 0 ? "Næste om \(mm(inS))" : "Næste er startet"
                            tint = .systemRed
                        } else if left < 0 {
                            title = "● +\(mm(-left))"
                            tint = .systemRed
                        } else if left <= 600 {
                            title = "● \(share) · \(mm(left))"
                            tint = left <= 300 ? .systemOrange : nil
                        }
                    }
                    // one key per call: its recording, so a call is placed once and then left where you move it
                    newCall = (st["call_id"] as? String) ?? (st["title"] as? String ?? "")
                } else if (settings?["enabled"] as? Bool) == false {
                    title = "Coach fra"
                } else if let inS = up?["in_s"] as? Int, inS <= 900 {
                    // between calls, from 15 min before the next meeting: orange the last 5 min, red the last 2
                    title = inS > 0 ? "Næste \(mm(inS))" : "Mødet +\(mm(-inS))"
                    tint = inS <= 120 ? .systemRed : inS <= 300 ? .systemOrange : nil
                }
            } else {
                title = "Coach ?"   // the coach is not answering
            }
            DispatchQueue.main.async {
                guard let self else { return }
                if let tint {
                    self.item.button?.attributedTitle = NSAttributedString(string: title, attributes: [
                        .foregroundColor: tint, .font: NSFont.menuBarFont(ofSize: 0).withWeight(.semibold)])
                } else {
                    self.item.button?.attributedTitle = NSAttributedString(string: "")
                    self.item.button?.title = title
                }
                if let settings { self.settings = settings }
                self.upcoming = up
                // A new call: open next to it. A call already placed is left where you moved it.
                if let newCall, newCall != self.callKey, (self.settings["auto_open"] as? String) != "never" {
                    self.callKey = newCall
                    self.placeNextToCall()
                    self.show()
                }
            }
        }.resume()
    }

    // The call's own window: the frontmost normal window of a call app (browser, Zoom, Teams...). Window
    // bounds and owner names need no screen-recording permission. Outside its right edge when the screen
    // has room, else inside its top-right corner, below a browser's toolbar.
    func placeNextToCall() {
        let callApps = ["Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge", "Firefox", "Vivaldi",
                        "Opera", "zoom.us", "Microsoft Teams", "Slack", "FaceTime", "WhatsApp", "Discord", "Webex"]
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]], let primary = NSScreen.screens.first else { return }
        for w in list {   // front to back
            guard (w[kCGWindowLayer as String] as? Int) == 0,
                  let owner = w[kCGWindowOwnerName as String] as? String, callApps.contains(owner),
                  let b = w[kCGWindowBounds as String] as? NSDictionary,
                  let rect = CGRect(dictionaryRepresentation: b), rect.width >= 500, rect.height >= 300 else { continue }
            let call = NSRect(x: rect.minX, y: primary.frame.height - rect.maxY, width: rect.width, height: rect.height)
            guard let screen = NSScreen.screens.first(where: { $0.frame.intersects(call) }) else { return }
            let v = screen.visibleFrame, size = window.frame.size
            var x = call.maxX + 8 + size.width <= v.maxX ? call.maxX + 8 : call.maxX - size.width - 16
            var y = call.maxY - size.height - 90
            x = min(max(x, v.minX), v.maxX - size.width)
            y = min(max(y, v.minY), v.maxY - size.height)
            window.setFrameOrigin(NSPoint(x: x, y: y))
            return
        }
    }

    // The page says "type" when you click into the feedback box. The panel never activates the app, so
    // typed or dictated text would go to the app in front; a click of your own brings this app forward.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard (message.body as? String) == "type" else { return }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKey()
    }

    // ---- window and app behaviour

    // Closing hides; the app, its Dock icon and the menu bar item stay.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    // A click on the Dock icon, Spotlight, or the coach's `open -a` at a call or a result.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        show()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // The coach may still be starting: try again instead of showing an error page.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { webView.load(URLRequest(url: pageURL)) }
    }

    // Links open in the browser, never inside the window.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url,
           url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

extension NSFont {
    func withWeight(_ weight: NSFont.Weight) -> NSFont {
        NSFont.systemFont(ofSize: pointSize, weight: weight)
    }
}

// --export-calendar: write calendar.json once and quit (the installer's check that Calendar access works)
if CommandLine.arguments.contains("--export-calendar") {
    let feed = CalendarFeed()
    let finish = {
        feed.write()
        print(feed.allowed ? "calendar.json written" : "no Calendar access")
        exit(feed.allowed ? 0 : 1)
    }
    if feed.undecided {
        let done: (Bool, Error?) -> Void = { _, _ in DispatchQueue.main.async { feed.store.reset(); finish() } }
        if #available(macOS 14.0, *) { feed.store.requestFullAccessToEvents(completion: done) }
        else { feed.store.requestAccess(to: .event, completion: done) }
        RunLoop.main.run()
    }
    finish()
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)   // a Dock icon: open, minimise, close like any app
app.run()

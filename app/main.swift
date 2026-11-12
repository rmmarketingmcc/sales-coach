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

import ApplicationServices
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
// The version is the coach's (engine/VERSION): the apps themselves only change when their own code does
func installedVersion() -> String {
    if let v = try? String(contentsOf: dataDir.appendingPathComponent("engine/VERSION"), encoding: .utf8) {
        let t = v.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { return t }
    }
    return Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
}
var appVersion = installedVersion()
let latestURL = URL(string: "https://api.github.com/repos/rmmarketingmcc/sales-coach/releases/latest")!
let installURL = "https://rmmarketingmcc.github.io/sales-coach/install.sh"
/// "0.3.1" > "0.3.0": numbers compared part by part
func isNewer(_ a: String, than b: String) -> Bool {
    let x = a.split(separator: ".").map { Int($0) ?? 0 }, y = b.split(separator: ".").map { Int($0) ?? 0 }
    for i in 0..<max(x.count, y.count) {
        let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
        if p != q { return p > q }
    }
    return false
}
let issuesURL = URL(string: "https://github.com/rmmarketingmcc/sales-coach/issues/new")!
// A phone call on speaker: the recorder records while this file exists (recorder/main.swift)
let phoneFlag = recordings.appendingPathComponent(".phone_call")
func phoneCallOn() -> Bool { FileManager.default.fileExists(atPath: phoneFlag.path) }

// The next meeting on the calendar, from the coach's .schedule.json (the next 24 h of real meetings, kept
// current for the recorder). The menu bar always counts down to it, never just "Coach".
func nextMeeting() -> (title: String, start: Date)? {
    guard let data = try? Data(contentsOf: recordings.appendingPathComponent(".schedule.json")),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let list = obj["meetings"] as? [[String: Any]] else { return nil }
    let iso = ISO8601DateFormatter()
    let now = Date()
    return list.compactMap { m -> (title: String, start: Date)? in
        guard let s = m["start"] as? String, let d = iso.date(from: s), d > now else { return nil }
        return ((m["title"] as? String ?? "Møde").trimmingCharacters(in: .whitespaces), d)
    }.min { $0.start < $1.start }
}

/// "2t 46m", "46 min", "under 1 min"
func untilText(_ seconds: Int) -> String {
    let m = max(0, seconds) / 60
    if m >= 60 { return m % 60 == 0 ? "\(m / 60)t" : "\(m / 60)t \(m % 60)m" }
    return m < 1 ? "under 1 min" : "\(m) min"
}

func clockText(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    return f.string(from: d)
}

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

// MARK: - Telling them you record, in the meeting's chat

/// Writes the recording notice in a Google Meet call's chat from your own seat, the way a recording bot announces
/// itself, but without a bot: Sales Coach finds the Meet tab in Chrome, Brave, Edge or Safari, opens the chat,
/// types the message and sends it, then checks it went out. It needs the browser's "Allow JavaScript from Apple
/// Events" (once, in its Developer menu) and macOS's permission for Sales Coach to control the browser (asked
/// once). Zoom, Teams and the rest: the window's "Kopiér til chatten" instead.
enum ChatNotice {
    static let chromes = ["com.google.chrome": "Google Chrome", "com.brave.browser": "Brave Browser", "com.microsoft.edgemac": "Microsoft Edge"]

    static func jsString(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())   // "...", JSON-escaped
    }

    static func appleScript(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    /// The three steps in the page: open the chat if it is closed, type and send, check it went out.
    static func steps(_ text: String) -> [String] {
        let msg = jsString(text), probe = jsString(String(text.prefix(30)))
        return [
            "(function(){var b=document.querySelector('textarea');if(b&&b.offsetParent)return 'open';var c=Array.prototype.find.call(document.querySelectorAll('button[aria-label],[role=\"button\"][aria-label]'),function(e){var l=e.getAttribute('aria-label')||'';return /chat/i.test(l)&&!/(luk|close)/i.test(l);});if(!c)return 'no chat button';c.click();return 'opened';})()",
            "(function(){var m=\(msg);var b=document.querySelector('textarea');if(!b)return 'no chat box';b.focus();var ok=document.execCommand&&document.execCommand('insertText',false,m);if(!ok||b.value.indexOf(m.slice(0,20))<0){Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(b,m);b.dispatchEvent(new Event('input',{bubbles:true}));}var p=b,s=null;for(var i=0;i<8&&p&&!s;i++){p=p.parentElement;if(p)s=Array.prototype.find.call(p.querySelectorAll('button[aria-label]'),function(e){return /send/i.test(e.getAttribute('aria-label')||'')&&!e.disabled;});}if(s){s.click();return 'sent';}b.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));return 'entered';})()",
            "(function(){var b=document.querySelector('textarea');return (document.body.innerText.indexOf(\(probe))>=0&&(!b||b.value.length===0))?'ok':'not sent';})()",
        ]
    }

    /// (written, why not) for the call going on now.
    static func post(_ text: String) -> (Bool, String) {
        let running = Set(NSWorkspace.shared.runningApplications.compactMap { $0.bundleIdentifier?.lowercased() })
        let js = steps(text).map(appleScript)
        var tried = false
        for (bundle, name) in chromes where running.contains(bundle) {
            tried = true
            let script = """
            tell application "\(name)"
              repeat with w in windows
                repeat with t in tabs of w
                  if (URL of t) contains "meet.google.com/" and (URL of t) does not end with "meet.google.com/" then
                    set r1 to execute t javascript \(js[0])
                    delay 1.2
                    set r2 to execute t javascript \(js[1])
                    delay 1.5
                    set r3 to execute t javascript \(js[2])
                    return r1 & "|" & r2 & "|" & r3
                  end if
                end repeat
              end repeat
              return "no meet tab"
            end tell
            """
            let (out, err) = run(script)
            if out.hasSuffix("|ok") { return (true, "") }
            if out != "no meet tab" { return (false, explain(out, err, name)) }
        }
        if running.contains("com.apple.safari") {
            tried = true
            let script = """
            tell application "Safari"
              repeat with w in windows
                repeat with t in tabs of w
                  if (URL of t) contains "meet.google.com/" and (URL of t) does not end with "meet.google.com/" then
                    set r1 to do JavaScript \(js[0]) in t
                    delay 1.2
                    set r2 to do JavaScript \(js[1]) in t
                    delay 1.5
                    set r3 to do JavaScript \(js[2]) in t
                    return r1 & "|" & r2 & "|" & r3
                  end if
                end repeat
              end repeat
              return "no meet tab"
            end tell
            """
            let (out, err) = run(script)
            if out.hasSuffix("|ok") { return (true, "") }
            if out != "no meet tab" { return (false, explain(out, err, "Safari")) }
        }
        _ = tried
        return (false, "Kun et Google Meet-møde i Chrome, Brave, Edge eller Safari kan få beskeden skrevet af sig selv. Kopiér den til chatten.")
    }

    static func explain(_ out: String, _ err: String, _ browser: String) -> String {
        let e = err.lowercased()
        if e.contains("apple events") || e.contains("javascript via applescript") || e.contains("javascript through applescript")
            || e.contains("allow javascript") {
            return browser == "Safari"
                ? "Slå det til én gang i Safari: Udvikler > Tillad JavaScript fra Apple Events. Indtil da: Kopiér til chatten."
                : "Slå det til én gang i \(browser): Se > Udvikler > Tillad JavaScript via Apple Events. Indtil da: Kopiér til chatten."
        }
        if e.contains("-1743") || e.contains("not authorized") || e.contains("ikke tilladelse") {
            return "Giv Sales Coach lov til at styre \(browser): Systemindstillinger > Anonymitet og sikkerhed > Automatisering. Indtil da: Kopiér til chatten."
        }
        return "Beskeden kom ikke ud i chatten (\(out.isEmpty ? String(err.prefix(120)) : out)). Kopiér den til chatten."
    }

    static func run(_ script: String) -> (String, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do { try p.run() } catch { return ("", "\(error)") }
        p.waitUntilExit()
        let o = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let e = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return (o.trimmingCharacters(in: .whitespacesAndNewlines), e.trimmingCharacters(in: .whitespacesAndNewlines))
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
    var inCall = false                  // a call going on, from the poll
    // A new version (at most one a week): looked up once a day, offered in the menu, installed on one click
    var latest: String? = nil
    var updating = false
    var updateFailed = false
    var noticeTried: Set<String> = []   // calls the chat message was tried for (once per call)
    // Muted in the meeting: the coach writes nothing down from your side. The meeting's own mute button, read
    // through Accessibility (only once you allow it), sent to the coach on every change
    var meetingMuted: Bool? = nil
    var muteButton: AXUIElement? = nil   // owned by muteQueue
    var muteBusy = false
    var muteAt = Date.distantPast
    let muteQueue = DispatchQueue(label: "salescoach.mute")

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
        item.button?.title = "Sales Coach"   // replaced by the countdown within a second
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        refreshMenubar()
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refreshMenubar() }
        calendar.start()
        // whether the meeting's mute button can be read (muteWatch), in the coach's log at every start
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            self?.note("mute watch: Accessibility \(AXIsProcessTrusted() ? "on" : "off (Sales Coach is not ticked under Accessibility)")")
        }
        checkForUpdate()
        Timer.scheduledTimer(withTimeInterval: 24 * 3600, repeats: true) { [weak self] _ in self?.checkForUpdate() }
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
        if updating {
            let busy = entry("Opdaterer Sales Coach...", nil)
            busy.isEnabled = false
            menu.addItem(busy)
            menu.addItem(.separator())
        } else if let v = latest, isNewer(v, than: appVersion), !inCall {
            menu.addItem(entry(updateFailed ? "Opdateringen fejlede: prøv igen (\(v))" : "Ny version \(v): opdater", #selector(runUpdate)))
            menu.addItem(.separator())
        }
        if let n = nextMeeting() {
            let head = entry("Næste møde: \(n.title), kl. \(clockText(n.start))", nil)
            head.isEnabled = false
            menu.addItem(head)
            menu.addItem(.separator())
        }
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
        // how long calls are kept, and telling the other side you record (GDPR)
        let audioDays = (settings["keep_audio_days"] as? Int) ?? 7
        let keepAudio = entry("Slet lyden", nil)
        let audioMenu = NSMenu()
        for (days, label) in [(0, "Lige efter opkaldet"), (7, "Efter 7 dage"), (30, "Efter 30 dage")] {
            audioMenu.addItem(entry(label, #selector(setKeep(_:)), on: audioDays == days, tag: ["keep_audio_days": days]))
        }
        keepAudio.submenu = audioMenu
        menu.addItem(keepAudio)
        let notesDays = settings["keep_notes_days"] as? Int   // nil: never
        let keepNotes = entry("Slet noter og udskrift", nil)
        let notesMenu = NSMenu()
        for (days, label) in [(30, "Efter 30 dage"), (90, "Efter 90 dage"), (365, "Efter 1 år"), (0, "Aldrig")] {
            let on = days == 0 ? notesDays == nil : notesDays == days
            notesMenu.addItem(entry(label, #selector(setKeep(_:)), on: on, tag: ["keep_notes_days": days == 0 ? "never" as Any : days as Any]))
        }
        keepNotes.submenu = notesMenu
        menu.addItem(keepNotes)
        let notice = (settings["notice"] as? Bool) ?? true
        menu.addItem(entry("Fortæl dem, at jeg optager", #selector(toggleNotice), on: notice))
        if notice {
            let auto = (settings["notice_auto"] as? Bool) ?? true
            menu.addItem(entry("    Skriv det selv i Meet-chatten", #selector(toggleNoticeAuto), on: auto))
        }
        if inCall {
            menu.addItem(entry("Stop og slet dette opkald...", #selector(forgetCall)))
        }
        if !calendar.allowed {
            menu.addItem(entry("Giv adgang til Kalender (så coachen kender dine møder)...", #selector(openCalendarAccess)))
        }
        if !AXIsProcessTrusted() {
            menu.addItem(entry("Giv adgang til Tilgængelighed (så det, du siger på mute, ikke skrives ned)...", #selector(openMuteAccess)))
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

    // ---- updates

    func checkForUpdate() {
        var req = URLRequest(url: latestURL)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let tag = obj["tag_name"] as? String else { return }
            DispatchQueue.main.async { self?.latest = tag.hasPrefix("v") ? String(tag.dropFirst()) : tag }
        }.resume()
    }

    /// The install line with --update, in the background: only the coach is swapped while the apps' code is the
    /// same (they keep macOS's permissions); new apps restart this one by themselves. Log: <data>/update.log.
    @objc func runUpdate() {
        guard !updating, !inCall else { return }
        updating = true
        updateFailed = false
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        let log = dataDir.appendingPathComponent("update.log").path.replacingOccurrences(of: "'", with: "'\\''")
        p.arguments = ["-c", "curl -fsSL '\(installURL)' | /bin/bash -s -- --update > '\(log)' 2>&1"]
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self else { return }
                self.updating = false
                self.updateFailed = proc.terminationStatus != 0
                appVersion = installedVersion()
                if !self.updateFailed {   // the new coach serves the new window
                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { self.web.load(URLRequest(url: pageURL)) }
                }
            }
        }
        do { try p.run() } catch { updating = false; updateFailed = true }
    }

    @objc func joinNext(_ sender: NSMenuItem) {
        if let link = sender.representedObject as? String, let url = URL(string: link) { NSWorkspace.shared.open(url) }
    }
    @objc func toggleJoin() { post("/settings", ["auto_join": !((settings["auto_join"] as? Bool) ?? true)]) }

    @objc func setKeep(_ sender: NSMenuItem) {
        if let change = sender.representedObject as? [String: Any] { post("/settings", change) }
    }
    @objc func toggleNoticeAuto() { post("/settings", ["notice_auto": !((settings["notice_auto"] as? Bool) ?? true)]) }
    func note(_ msg: String) { post("/note", ["msg": msg]) }
    @objc func toggleNotice() { post("/settings", ["notice": !((settings["notice"] as? Bool) ?? true)]) }

    // The other side said no, or you changed your mind: the recording stops and nothing of the call is kept.
    @objc func forgetCall() {
        let alert = NSAlert()
        alert.messageText = "Stop og slet dette opkald?"
        alert.informativeText = "Optagelsen stopper, og lyd, udskrift og noter fra opkaldet slettes. Det kan ikke fortrydes."
        alert.addButton(withTitle: "Stop og slet")
        alert.addButton(withTitle: "Annullér")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn { post("/forget", [:]) }
    }

    @objc func openCalendarAccess() {
        if calendar.undecided {
            calendar.ask()
        } else {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")!)
        }
    }

    /// macOS's own Accessibility prompt: it puts Sales Coach in the list, ticked off until you tick it
    @objc func openMuteAccess() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        if !AXIsProcessTrustedWithOptions(opts) {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
        }
    }

    // ---- the meeting's mute button (Google Meet in Chrome, Brave, Edge, Arc or Safari)

    static let browsers: Set<String> = ["com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac",
                                        "company.thebrowser.Browser", "com.apple.Safari"]

    /// true: muted ("Slå mikrofon til" / "Turn on microphone"), false: open, nil: not a mic toggle.
    /// Meet also has "Denne side har adgang til ... din mikrofon", which is no toggle and must not count.
    static func micState(_ e: AXUIElement) -> Bool? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, kAXDescriptionAttribute as CFString, &v) == .success,
              let d = (v as? String)?.lowercased() else { return nil }
        if d.hasPrefix("slå mikrofon til") || d.hasPrefix("turn on microphone") { return true }
        if d.hasPrefix("slå mikrofon fra") || d.hasPrefix("turn off microphone") { return false }
        return nil
    }

    static func findMicButton(_ e: AXUIElement, depth: Int, budget: inout Int) -> AXUIElement? {
        budget -= 1
        if budget < 0 || depth > 60 { return nil }
        var role: CFTypeRef?
        AXUIElementCopyAttributeValue(e, kAXRoleAttribute as CFString, &role)
        if (role as? String) == (kAXButtonRole as String), micState(e) != nil { return e }
        var kids: CFTypeRef?
        guard AXUIElementCopyAttributeValue(e, kAXChildrenAttribute as CFString, &kids) == .success,
              let list = kids as? [AXUIElement] else { return nil }
        for k in list {
            if let hit = findMicButton(k, depth: depth + 1, budget: &budget) { return hit }
        }
        return nil
    }

    /// The meeting's mute state now, or nil when no meeting tab shows a mic button (Zoom, a phone call...).
    func readMeetingMute() -> Bool? {
        if let b = muteButton, let m = AppDelegate.micState(b) { return m }
        muteButton = nil
        for app in NSWorkspace.shared.runningApplications where AppDelegate.browsers.contains(app.bundleIdentifier ?? "") {
            let ax = AXUIElementCreateApplication(app.processIdentifier)
            AXUIElementSetAttributeValue(ax, "AXManualAccessibility" as CFString, kCFBooleanTrue)   // Chrome: expose the page
            var wins: CFTypeRef?
            guard AXUIElementCopyAttributeValue(ax, kAXWindowsAttribute as CFString, &wins) == .success,
                  let list = wins as? [AXUIElement] else { continue }
            for w in list {
                var t: CFTypeRef?
                AXUIElementCopyAttributeValue(w, kAXTitleAttribute as CFString, &t)
                guard ((t as? String) ?? "").contains("Meet") else { continue }
                var budget = 40000
                if let b = AppDelegate.findMicButton(w, depth: 0, budget: &budget) {
                    muteButton = b
                    return AppDelegate.micState(b)
                }
            }
        }
        return nil
    }

    /// Every 2 s during a call: a change in the meeting's mute goes to the coach (POST /mute), which keeps
    /// the muted stretch out of the transcript and the talk share.
    func muteWatch(active: Bool) {
        guard active, AXIsProcessTrusted() else {
            if meetingMuted == true { post("/mute", ["muted": false]) }
            meetingMuted = nil
            muteQueue.async { [weak self] in self?.muteButton = nil }   // the reader's own queue owns it
            return
        }
        guard !muteBusy, Date().timeIntervalSince(muteAt) >= 2 else { return }
        muteBusy = true
        muteAt = Date()
        muteQueue.async { [weak self] in
            guard let self else { return }
            let m = self.readMeetingMute()
            DispatchQueue.main.async {
                self.muteBusy = false
                guard let m, m != self.meetingMuted else { return }
                self.meetingMuted = m
                self.post("/mute", ["muted": m])
            }
        }
    }

    // A GitHub issue with the technical facts filled in. Issues are public: never your name, company,
    // meetings or words, only versions and switches.
    @objc func reportProblem() {
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        URLSession.shared.dataTask(with: URL(string: "\(base)/health")!) { data, _, _ in
            var coach = "svarer ikke"
            if let data, let h = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let s = h["settings"] as? [String: Any] ?? [:]
                coach = "\(h["version"] ?? "?"), AI \(h["ai"] ?? "?"), i et opkald: \(h["call"] ?? "?"), "
                    + "sprog \(s["language"] ?? "?"), slået til: \(s["enabled"] ?? "?"), åbner: \(s["auto_open"] ?? "?"), "
                    + "tilføjelser: \((s["addons"] as? [Any])?.count ?? 0)"
            }
            let body = "Hvad skete der:\n\n\nHvad forventede du:\n\n\n---\nSales Coach \(appVersion), macOS \(os)\n"
                + "Adgang til Kalender: \(self.calendar.allowed ? "ja" : "nej")\n"
                + "Adgang til Tilgængelighed: \(AXIsProcessTrusted() ? "ja" : "nej")\nCoach: \(coach)\n"
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
            let next = nextMeeting()
            let countdown = next.map { "om \(untilText(Int($0.start.timeIntervalSinceNow)))" }
            var title = countdown.map { "Næste \($0)" } ?? "Ingen møder det næste døgn"
            var tint: NSColor? = nil   // orange: the last 10 min; red: over time, or the next call is 5 min away
            var settings: [String: Any]? = nil
            var newCall: String? = nil
            var up: [String: Any]? = nil
            var inCall = false
            var chatNotice: (call: String, text: String)? = nil
            let mm = { (s: Int) in "\(s / 60):" + String(format: "%02d", s % 60) }
            if let data, let st = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                settings = st["settings"] as? [String: Any]
                up = st["upcoming"] as? [String: Any]
                inCall = (st["active"] as? Bool) == true
                if inCall, (st["confirmed"] as? Bool) == true, let n = st["notice"] as? [String: Any], (n["auto"] as? Bool) == true,
                   let call = st["call_id"] as? String, let text = n["text"] as? String {
                    chatNotice = (call, text)
                }
                if inCall {
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
                    // one key per call: its recording, so a call is placed once and then left where you move it.
                    // Only a confirmed call (calendar, phone, or the other side talked 3 s): a voice message holds
                    // the mic like a call does and must not pop the window
                    if (st["confirmed"] as? Bool) == true {
                        newCall = (st["call_id"] as? String) ?? (st["title"] as? String ?? "")
                    }
                } else if (settings?["enabled"] as? Bool) == false {
                    title = "Coach fra" + (countdown.map { " · næste \($0)" } ?? "")
                } else if let inS = up?["in_s"] as? Int, inS <= 900 {
                    // between calls, from 15 min before the next meeting: orange the last 5 min, red the last 2
                    title = inS > 0 ? "Næste \(mm(inS))" : "Mødet +\(mm(-inS))"
                    tint = inS <= 120 ? .systemRed : inS <= 300 ? .systemOrange : nil
                }
            } else {
                title = "Coachen svarer ikke" + (countdown.map { " · næste \($0)" } ?? "")
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
                self.inCall = inCall
                if data != nil { self.muteWatch(active: inCall) }
                if let chatNotice, !self.noticeTried.contains(chatNotice.call) {
                    self.noticeTried.insert(chatNotice.call)
                    DispatchQueue.global(qos: .userInitiated).async {
                        let (ok, why) = ChatNotice.post(chatNotice.text)
                        DispatchQueue.main.async {
                            if ok { self.post("/notice", ["how": "chat_auto"]) } else { self.post("/notice_fail", ["why": why]) }
                            self.note(ok ? "recording notice written in the Meet chat" : "recording notice not written: \(why)")
                        }
                    }
                }
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
        if let body = message.body as? [String: Any], let text = body["copy"] as? String {   // "Kopiér til chatten"
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            return
        }
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

// --chat-js <text>: the chat steps and their AppleScript quoting, as JSON (tests/chat_notice_test.py checks them)
if let i = CommandLine.arguments.firstIndex(of: "--chat-js"), i + 1 < CommandLine.arguments.count {
    let steps = ChatNotice.steps(CommandLine.arguments[i + 1])
    let out: [String: Any] = ["steps": steps, "quoted": steps.map(ChatNotice.appleScript)]
    print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
    exit(0)
}

// --ask-browser: macOS asks once for Sales Coach to control the browsers that are open, so the question comes at
// install and not in the middle of the first meeting (writing in the Meet chat needs it)
if CommandLine.arguments.contains("--ask-browser") {
    let running = Set(NSWorkspace.shared.runningApplications.compactMap { $0.bundleIdentifier?.lowercased() })
    for (bundle, name) in ChatNotice.chromes.merging(["com.apple.safari": "Safari"], uniquingKeysWith: { a, _ in a })
        where running.contains(bundle) {
        let (_, err) = ChatNotice.run("tell application \"\(name)\" to count windows")
        print("\(name): \(err.isEmpty ? "ok" : err)")
    }
    exit(0)
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

// Sales Coach Recorder: records your online calls on your own Mac, for Sales Coach's live coach.
//
// Runs in the background (a LaunchAgent) and records on its own, with no bot in the call: when a
// browser or call app holds the microphone, it records the call audio (a Core Audio process tap,
// so the other side, also with headphones) and your microphone to separate tracks under
// ~/CallRecordings/<UTC timestamp>/. The coach (engine/coach.py) reads both tracks as they grow.
//
// Build: ./build.sh. Debug (launch through `open` so macOS checks THIS app's permissions, not the terminal's):
//   open -W "~/Applications/Sales Coach Recorder.app" --args --list      who holds the mic / plays audio now
//   open -W "~/Applications/Sales Coach Recorder.app" --args --test 15   record 15 s unconditionally
// It never records without a call: it only starts while a call app holds the mic, stops 30 s after
// the app lets go, and outside calendar meetings (.schedule.json, written by the coach) it also stops
// once nobody else has spoken for 10 min.
// Everything is logged to ~/CallRecordings/callrec.log.

import Accelerate
import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin
import Foundation

// MARK: - Config

let rootDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("CallRecordings")
let queueDir = rootDir.appendingPathComponent(".queue")
let logFile = rootDir.appendingPathComponent("callrec.log")
let pollInterval: TimeInterval = 1.0
let startAfterTicks = 3                   // a meeting app must hold the mic this many polls in a row
let stopAfterSilence: TimeInterval = 30   // ...and release it this long before we stop
let minKeepDuration: TimeInterval = 90    // shorter recordings (mic tests, voice notes) are deleted
let scheduleFile = rootDir.appendingPathComponent(".schedule.json")
var quietLimitOutsideMeetings: TimeInterval = 600  // stop when nobody else has spoken this long (--quiet-limit)
let remoteSoundRMS: Float = 0.003         // call audio louder than this (~-50 dBFS) counts as someone talking
let maxDuration: TimeInterval = 4 * 3600  // safety cap; a call still running starts a new recording
// A phone call on speaker: no app on the Mac holds the mic, so this file is the call. Sales Coach's menu creates it
// ("Start telefonopkald") and deletes it ("Stop"); recording runs exactly that long, both voices on the mic.
let phoneFlag = rootDir.appendingPathComponent(".phone_call")
// The next meeting's video link was opened: the coach writes this, and the recording in
// progress ends and a new one starts at once, so each meeting gets its own recording, coach and notes.
let splitFlag = rootDir.appendingPathComponent(".split")
// "Stop og slet" in the coach: the recording going on now is stopped and deleted, and nothing new starts until the
// call app lets go of the mic (the coach writes this file and deletes everything else of the call).
let stopFlag = rootDir.appendingPathComponent(".stop")

// Apps whose microphone use means "I'm in a call". Lowercase prefix of the Core Audio
// process bundle ID (browser helpers report e.g. com.google.chrome.helper).
var meetingApps = [
    "com.google.chrome", "com.apple.safari", "com.apple.webkit", "company.thebrowser",
    "com.brave.browser", "com.microsoft.edgemac", "org.mozilla.firefox", "com.vivaldi.vivaldi",
    "com.operasoftware.opera", "us.zoom.xos", "com.microsoft.teams", "com.tinyspeck.slackmacgap",
    "com.apple.facetime", "net.whatsapp.whatsapp", "desktop.whatsapp", "com.hnc.discord",
    "com.cisco.webex", "com.webex",
]
// Kept out of the system-audio track: music during a call is only noise for the transcript.
let excludedFromTap = ["com.spotify.client", "com.apple.music", "com.roli"]

// MARK: - Logging

let logStamp: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

func log(_ message: String) {
    let line = "[\(logStamp.string(from: Date()))] \(message)\n"
    print(line, terminator: "")
    guard let data = line.data(using: .utf8) else { return }
    if let handle = try? FileHandle(forWritingTo: logFile) {
        handle.seekToEndOfFile()
        handle.write(data)
        try? handle.close()
    } else {
        try? data.write(to: logFile)
    }
}

// MARK: - Core Audio helpers

enum RecError: Error, CustomStringConvertible {
    case os(String, OSStatus)
    case other(String)

    var description: String {
        switch self {
        case .os(let what, let status): return "\(what) failed (OSStatus \(status))"
        case .other(let text): return text
        }
    }
}

func propertyAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func readValue<T: FixedWidthInteger>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var address = propertyAddress(selector)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = propertyAddress(selector)
    var ref: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &ref)
    guard status == noErr, let ref else { return nil }
    return ref.takeRetainedValue() as String
}

func readObjectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var address = propertyAddress(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

struct AudioProcess {
    let object: AudioObjectID
    let pid: pid_t
    let bundleID: String   // lowercase, "" for command-line tools
    let name: String       // lowercase executable name
    let input: Bool
    let output: Bool

    var label: String { bundleID.isEmpty ? name : bundleID }
}

func executableName(_ pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4096)
    guard pid > 0, proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return "" }
    return URL(fileURLWithPath: String(cString: buffer)).lastPathComponent.lowercased()
}

func describeProcess(_ object: AudioObjectID, input: Bool, output: Bool) -> AudioProcess {
    let pid = readValue(object, kAudioProcessPropertyPID, pid_t(-1)) ?? -1
    return AudioProcess(object: object, pid: pid,
                        bundleID: (readString(object, kAudioProcessPropertyBundleID) ?? "").lowercased(),
                        name: executableName(pid), input: input, output: output)
}

func processObjects() -> [AudioObjectID] {
    readObjectList(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
}

func isRunning(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> Bool {
    (readValue(object, selector, UInt32(0)) ?? 0) != 0
}

/// Every process Core Audio knows about (used for --list and the tap exclusion list).
func audioProcesses() -> [AudioProcess] {
    processObjects().map {
        describeProcess($0, input: isRunning($0, kAudioProcessPropertyIsRunningInput),
                        output: isRunning($0, kAudioProcessPropertyIsRunningOutput))
    }
}

/// Processes holding the microphone right now. Cheap enough for a 1 s poll: names are
/// only resolved for the (usually zero or one) processes with input running.
func micHolders() -> [AudioProcess] {
    processObjects()
        .filter { isRunning($0, kAudioProcessPropertyIsRunningInput) }
        .map { describeProcess($0, input: true, output: false) }
        .filter { $0.pid != getpid() }
}

func isMeetingApp(_ process: AudioProcess) -> Bool {
    meetingApps.contains { process.bundleID.hasPrefix($0) || (!process.name.isEmpty && process.name == $0) }
}

// MARK: - Writers

/// 16-bit PCM CAF: survives a crash mid-call (an unfinished AAC/m4a file is unreadable).
func pcmFileSettings(_ format: AVAudioFormat) -> [String: Any] {
    [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: format.sampleRate,
     AVNumberOfChannelsKey: format.channelCount, AVLinearPCMBitDepthKey: 16,
     AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false]
}

let hostTimebase: Double = {
    var info = mach_timebase_info_data_t()
    mach_timebase_info(&info)
    return Double(info.numer) / Double(info.denom) / 1_000_000_000
}()

func hostSeconds(_ ticks: UInt64) -> Double { Double(ticks) * hostTimebase }

func closeFile(_ file: AVAudioFile?) {
    guard let file else { return }
    if #available(macOS 15.0, *) { file.close() }
}

func bufferRMS(_ buffer: AVAudioPCMBuffer) -> Float {
    guard let data = buffer.floatChannelData else { return 0 }
    let count = Int(buffer.frameLength) * (buffer.format.isInterleaved ? Int(buffer.format.channelCount) : 1)
    guard count > 0 else { return 0 }
    var rms: Float = 0
    vDSP_rmsqv(data[0], 1, &rms, vDSP_Length(count))
    return rms
}

func bufferPeak(_ buffer: AVAudioPCMBuffer) -> Float {
    guard let data = buffer.floatChannelData else { return 0 }
    let count = Int(buffer.frameLength) * (buffer.format.isInterleaved ? Int(buffer.format.channelCount) : 1)
    guard count > 0 else { return 0 }
    var peak: Float = 0
    vDSP_maxmgv(data[0], 1, &peak, vDSP_Length(count))
    return peak
}

/// System audio (everything the Mac plays, minus music apps) via a Core Audio process
/// tap on a tap-only aggregate device. No physical sub-device, so switching output
/// (AirPods <-> speakers) mid-call does not change what we read.
final class SystemTap {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: AudioDeviceIOProcID?
    private var file: AVAudioFile?
    private let queue = DispatchQueue(label: "callrec.systemtap", qos: .userInitiated)
    private(set) var peak: Float = 0
    private var callbacks = 0
    private var unreadable = 0
    private var framesWritten: AVAudioFramePosition = 0
    private var lastSoundAt: Date?
    var onFormatChange: (() -> Void)?

    /// When the call audio last had someone talking (survives segment rolls).
    var lastSound: Date? { queue.sync { lastSoundAt } }

    private func writeSilence(_ frames: AVAudioFramePosition, format: AVAudioFormat, to file: AVAudioFile) {
        let chunk = AVAudioFrameCount(format.sampleRate)
        guard let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunk) else { return }
        var remaining = frames
        while remaining > 0 {
            silence.frameLength = AVAudioFrameCount(min(AVAudioFramePosition(chunk), remaining))
            for buffer in UnsafeMutableAudioBufferListPointer(silence.mutableAudioBufferList) {
                if let data = buffer.mData { memset(data, 0, Int(buffer.mDataByteSize)) }
            }
            do { try file.write(from: silence) } catch {
                log("system audio silence write failed: \(error)")
                return
            }
            framesWritten += AVAudioFramePosition(silence.frameLength)
            remaining -= AVAudioFramePosition(silence.frameLength)
        }
    }

    func start(writingTo url: URL) throws {
        let excluded = audioProcesses()
            .filter { process in excludedFromTap.contains { process.bundleID.hasPrefix($0) } || process.pid == getpid() }
            .map(\.object)
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        description.uuid = UUID()
        description.name = "Sales Coach Recorder"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var status = AudioHardwareCreateProcessTap(description, &tapID)
        guard status == noErr else { throw RecError.os("AudioHardwareCreateProcessTap", status) }

        var address = propertyAddress(kAudioTapPropertyFormat)
        var streamDescription = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &streamDescription)
        guard status == noErr else { throw RecError.os("read tap format", status) }
        guard let format = AVAudioFormat(streamDescription: &streamDescription) else {
            throw RecError.other("unsupported tap format")
        }

        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Sales Coach Recorder tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [] as [Any],
            kAudioAggregateDeviceTapListKey: [
                [kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: description.uuid.uuidString],
            ],
        ]
        status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID)
        guard status == noErr else { throw RecError.os("AudioHardwareCreateAggregateDevice", status) }

        let file = try AVAudioFile(forWriting: url, settings: pcmFileSettings(format),
                                   commonFormat: .pcmFormatFloat32, interleaved: format.isInterleaved)
        self.file = file
        peak = 0
        callbacks = 0
        unreadable = 0
        framesWritten = 0
        let startSeconds = hostSeconds(mach_absolute_time())
        status = AudioDeviceCreateIOProcIDWithBlock(&ioProc, aggregateID, queue) { [weak self] now, input, inputTime, _, _ in
            guard let self, let file = self.file else { return }
            self.callbacks += 1
            // Keep the file on the wall clock: if the tap went quiet (nothing playing),
            // write the gap as silence so this track stays aligned with the mic track.
            let host = inputTime.pointee.mFlags.contains(.hostTimeValid) ? inputTime.pointee.mHostTime : now.pointee.mHostTime
            let expected = AVAudioFramePosition((hostSeconds(host) - startSeconds) * format.sampleRate)
            if expected - self.framesWritten > AVAudioFramePosition(format.sampleRate / 10) {
                self.writeSilence(expected - self.framesWritten, format: format, to: file)
            }
            if self.callbacks == 1 {
                let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
                log("system audio: first buffer list has \(list.count) buffer(s): "
                    + list.map { "\($0.mNumberChannels) ch / \($0.mDataByteSize) B" }.joined(separator: ", "))
            }
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil),
                  buffer.frameLength > 0 else {
                self.unreadable += 1
                return
            }
            do { try file.write(from: buffer) } catch { log("system audio write failed: \(error)") }
            self.framesWritten += AVAudioFramePosition(buffer.frameLength)
            self.peak = max(self.peak, bufferPeak(buffer))
            if bufferRMS(buffer) > remoteSoundRMS { self.lastSoundAt = Date() }
        }
        guard status == noErr else { throw RecError.os("AudioDeviceCreateIOProcIDWithBlock", status) }
        status = AudioDeviceStart(aggregateID, ioProc)
        guard status == noErr else { throw RecError.os("AudioDeviceStart", status) }

        // A format change (e.g. AirPods switching to headset mode) would make the buffers
        // above lie about their layout; the owner rolls to a new segment instead.
        status = AudioObjectAddPropertyListenerBlock(tapID, &address, DispatchQueue.main) { [weak self] _, _ in
            self?.onFormatChange?()
        }
        if status != noErr { log("tap format listener not installed (OSStatus \(status))") }
        log("system audio: \(Int(format.sampleRate)) Hz, \(format.channelCount) ch, excluding \(excluded.count) music process(es)")
    }

    func stop() {
        if aggregateID != kAudioObjectUnknown {
            if let ioProc {
                AudioDeviceStop(aggregateID, ioProc)
                AudioDeviceDestroyIOProcID(aggregateID, ioProc)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        queue.sync {
            if file != nil { log("system audio stopped: \(callbacks) callbacks, \(unreadable) unreadable") }
            closeFile(file)
            file = nil
        }
        ioProc = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

/// Your microphone (default input device) via AVAudioEngine.
final class MicRecorder {
    private var engine: AVAudioEngine?
    private var file: AVAudioFile?
    private var observer: NSObjectProtocol?
    private let lock = NSLock()
    private(set) var peak: Float = 0
    var onConfigChange: (() -> Void)?

    func start(writingTo url: URL) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw RecError.other("no microphone input (permission denied or no input device)")
        }
        file = try AVAudioFile(forWriting: url, settings: pcmFileSettings(format),
                               commonFormat: .pcmFormatFloat32, interleaved: false)
        peak = 0
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            self.lock.lock()
            defer { self.lock.unlock() }
            guard let file = self.file else { return }
            do { try file.write(from: buffer) } catch { log("mic write failed: \(error)") }
            self.peak = max(self.peak, bufferPeak(buffer))
        }
        // Default input changed (headset plugged in, AirPods connected): the engine stops,
        // so the owner rolls to a new segment.
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                          object: engine, queue: .main) { [weak self] _ in
            self?.onConfigChange?()
        }
        engine.prepare()
        try engine.start()
        self.engine = engine
        log("mic: \(Int(format.sampleRate)) Hz, \(format.channelCount) ch")
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        lock.lock()
        closeFile(file)
        file = nil
        lock.unlock()
    }
}

// MARK: - One recording

let folderStamp: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.timeZone = TimeZone(identifier: "UTC")
    f.dateFormat = "yyyy-MM-dd'T'HH-mm-ss'Z'"
    return f
}()

final class Recording {
    let started: Date
    let dir: URL
    let trigger: String
    private let system = SystemTap()
    private let mic = MicRecorder()
    private var segments: [[String: Any]] = []
    private var systemPeak: Float = 0
    private var micPeak: Float = 0
    private var errors: [String] = []

    init(trigger: String) {
        let now = Date()
        self.trigger = trigger
        started = now
        dir = rootDir.appendingPathComponent(folderStamp.string(from: now))
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        system.onFormatChange = { [weak self] in self?.rollSystem() }
        mic.onConfigChange = { [weak self] in self?.rollMic() }
    }

    var duration: TimeInterval { Date().timeIntervalSince(started) }

    func start() {
        startSegment(track: "system")
        startSegment(track: "mic")
        writeMeta(status: "recording")
    }

    private func startSegment(track: String) {
        let index = segments.filter { ($0["track"] as? String) == track }.count + 1
        let name = "\(track)_\(index).caf"
        let url = dir.appendingPathComponent(name)
        do {
            if track == "system" { try system.start(writingTo: url) } else { try mic.start(writingTo: url) }
            segments.append(["track": track, "file": name, "offset": duration])
        } catch {
            let message = "\(track) track not recording: \(error)"
            errors.append(message)
            log(message)
        }
    }

    private func rollSystem() {
        log("system audio format changed, starting a new segment")
        systemPeak = max(systemPeak, system.peak)
        system.stop()
        startSegment(track: "system")
        writeMeta(status: "recording")
    }

    private func rollMic() {
        log("mic configuration changed, starting a new segment")
        micPeak = max(micPeak, mic.peak)
        mic.stop()
        startSegment(track: "mic")
        writeMeta(status: "recording")
    }

    func stop() {
        systemPeak = max(systemPeak, system.peak)
        micPeak = max(micPeak, mic.peak)
        system.stop()
        mic.stop()
    }

    var peaks: (system: Float, mic: Float) { (systemPeak, micPeak) }

    var lastRemoteSound: Date { max(started, system.lastSound ?? started) }

    func writeMeta(status: String, reason: String? = nil) {
        var meta: [String: Any] = [
            "status": status,
            "started": logStamp.string(from: started),
            "trigger": trigger,
            "segments": segments,
            "pid": Int(getpid()),
        ]
        if status != "recording" {
            meta["ended"] = logStamp.string(from: Date())
            meta["duration_s"] = Int(duration)
            meta["system_peak"] = Double(systemPeak)
            meta["mic_peak"] = Double(micPeak)
            if let reason { meta["stop_reason"] = reason }
        }
        if !errors.isEmpty { meta["errors"] = errors }
        if let data = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: dir.appendingPathComponent("meta.json"), options: .atomic)
        }
    }
}

// MARK: - Detection loop

func queueProcessing() {
    try? FileManager.default.createDirectory(at: queueDir, withIntermediateDirectories: true)
    try? logStamp.string(from: Date()).write(to: queueDir.appendingPathComponent("trigger"),
                                             atomically: true, encoding: .utf8)
}

/// A crash or reboot mid-call leaves meta.json at "recording": mark them done so the coach can finish them.
func recoverInterruptedRecordings() {
    guard let folders = try? FileManager.default.contentsOfDirectory(at: rootDir, includingPropertiesForKeys: nil) else { return }
    var recovered = 0
    for folder in folders {
        let metaURL = folder.appendingPathComponent("meta.json")
        guard let data = try? Data(contentsOf: metaURL),
              var meta = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              meta["status"] as? String == "recording" else { continue }
        meta["status"] = "done"
        meta["stop_reason"] = "recorder restarted mid-call"
        if let out = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys]) {
            try? out.write(to: metaURL, options: .atomic)
            recovered += 1
        }
    }
    if recovered > 0 {
        log("recovered \(recovered) interrupted recording(s)")
        queueProcessing()
    }
}

/// Calendar meetings with guests or a meeting link, written every minute by the coach
/// (the recorder reads no calendar of its own).
struct Schedule {
    let fresh: Bool        // file updated within the last 2 hours
    let inMeeting: Bool    // now is within [start - 10 min, end + 30 min] of one of them

    static func now() -> Schedule {
        guard let data = try? Data(contentsOf: scheduleFile),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let updated = (json["updated"] as? String).flatMap(logStamp.date(from:)) else {
            return Schedule(fresh: false, inMeeting: false)
        }
        let now = Date()
        let meetings = json["meetings"] as? [[String: Any]] ?? []
        let inMeeting = meetings.contains { meeting in
            guard let start = (meeting["start"] as? String).flatMap(logStamp.date(from:)),
                  let end = (meeting["end"] as? String).flatMap(logStamp.date(from:)) else { return false }
            return now >= start.addingTimeInterval(-600) && now <= end.addingTimeInterval(1800)
        }
        return Schedule(fresh: now.timeIntervalSince(updated) < 7200, inMeeting: inMeeting)
    }
}

final class Controller {
    private var recording: Recording?
    private var holdTicks = 0
    private var lastMicUse = Date.distantPast
    // After a quiet-stop the call tab is usually still open; don't start again until it lets go of the mic.
    private var waitForMicRelease = false
    private var schedule = Schedule(fresh: false, inMeeting: false)
    private var scheduleReadAt = Date.distantPast

    func tick() {
        let holders = micHolders().filter(isMeetingApp)
        let now = Date()
        let phone = FileManager.default.fileExists(atPath: phoneFlag.path)
        if FileManager.default.fileExists(atPath: stopFlag.path) {
            let at = (try? FileManager.default.attributesOfItem(atPath: stopFlag.path))?[.modificationDate] as? Date
            try? FileManager.default.removeItem(at: stopFlag)
            if let current = recording, let at, now.timeIntervalSince(at) < 30 {   // an old one is ignored
                current.stop()
                recording = nil
                try? FileManager.default.removeItem(at: current.dir)
                try? FileManager.default.removeItem(at: phoneFlag)
                waitForMicRelease = current.trigger != "phone"
                log("recording deleted on request: \(current.dir.lastPathComponent)")
                return
            }
        }
        if FileManager.default.fileExists(atPath: splitFlag.path) {
            let at = (try? FileManager.default.attributesOfItem(atPath: splitFlag.path))?[.modificationDate] as? Date
            try? FileManager.default.removeItem(at: splitFlag)
            if let current = recording, let at, now.timeIntervalSince(at) < 30 {   // an old one is ignored
                let trigger = current.trigger
                finish(current, reason: "the next meeting's Meet was opened")
                let next = Recording(trigger: trigger)
                log("recording started (\(trigger), next meeting) -> \(next.dir.lastPathComponent)")
                next.start()
                self.recording = next
                lastMicUse = now
                return
            }
        }
        if let recording, recording.trigger == "phone" {
            if !phone {
                finish(recording, reason: "phone call stopped in Sales Coach")
            } else if recording.duration >= maxDuration {
                try? FileManager.default.removeItem(at: phoneFlag)
                finish(recording, reason: "max duration reached")
            }
            return
        }
        if recording == nil && phone {
            let recording = Recording(trigger: "phone")
            log("recording started (phone call on speaker) -> \(recording.dir.lastPathComponent)")
            recording.start()
            self.recording = recording
            return
        }
        if let recording {
            if !holders.isEmpty { lastMicUse = now }
            if now.timeIntervalSince(scheduleReadAt) >= 30 {
                schedule = Schedule.now()
                scheduleReadAt = now
            }
            // Never cut a calendar meeting. Outside one, a call where nobody else has said
            // anything for a while is a forgotten tab or a solo session, not a meeting.
            let quietLimit: TimeInterval? = schedule.inMeeting ? nil : (schedule.fresh ? quietLimitOutsideMeetings : 1800)
            if now.timeIntervalSince(lastMicUse) >= stopAfterSilence {
                finish(recording, reason: "call app released the mic")
            } else if let quietLimit, now.timeIntervalSince(recording.lastRemoteSound) >= quietLimit {
                let span = quietLimit >= 120 ? "\(Int(quietLimit / 60)) min" : "\(Int(quietLimit)) s"
                finish(recording, reason: "nobody else spoke for \(span) outside a calendar meeting")
                waitForMicRelease = true
            } else if recording.duration >= maxDuration {
                finish(recording, reason: "max duration reached")
            }
            return
        }
        if waitForMicRelease {
            if holders.isEmpty { waitForMicRelease = false }
            return
        }
        holdTicks = holders.isEmpty ? 0 : holdTicks + 1
        guard holdTicks >= startAfterTicks else { return }
        holdTicks = 0
        lastMicUse = now
        let trigger = Array(Set(holders.map(\.label))).sorted().joined(separator: ", ")
        let recording = Recording(trigger: trigger)
        log("recording started (\(trigger)) -> \(recording.dir.lastPathComponent)")
        recording.start()
        self.recording = recording
    }

    func finish(_ recording: Recording, reason: String) {
        recording.stop()
        self.recording = nil
        let duration = recording.duration
        if duration < minKeepDuration {
            try? FileManager.default.removeItem(at: recording.dir)
            log("discarded \(Int(duration)) s recording (\(reason), shorter than \(Int(minKeepDuration)) s)")
            return
        }
        recording.writeMeta(status: "done", reason: reason)
        let peaks = recording.peaks
        log("recording saved: \(recording.dir.lastPathComponent), \(Int(duration)) s, \(reason), "
            + String(format: "peaks system %.3f mic %.3f", peaks.system, peaks.mic))
        if peaks.system == 0 { log("WARNING: system audio was silent. Check System Settings > Privacy > Screen & System Audio Recording") }
        if peaks.mic == 0 { log("WARNING: mic was silent. Check System Settings > Privacy > Microphone") }
        queueProcessing()
    }

    func shutdown() {
        if let recording { finish(recording, reason: "recorder shut down") }
    }
}

// MARK: - Main

setvbuf(stdout, nil, _IOLBF, 0)
try? FileManager.default.createDirectory(at: queueDir, withIntermediateDirectories: true)
let arguments = CommandLine.arguments

if arguments.contains("--list") {
    for process in audioProcesses() where process.input || process.output {
        log("\(process.input ? "MIC " : "    ")\(process.output ? "OUT " : "    ")pid \(process.pid) \(process.label)"
            + (isMeetingApp(process) ? "  [call app]" : ""))
    }
    exit(0)
}

if let index = arguments.firstIndex(of: "--quiet-limit"), index + 1 < arguments.count,
   let seconds = Double(arguments[index + 1]) {
    quietLimitOutsideMeetings = seconds   // for testing the quiet-stop
}

if let index = arguments.firstIndex(of: "--extra-app"), index + 1 < arguments.count {
    meetingApps.append(arguments[index + 1].lowercased())   // for testing detection with e.g. ffmpeg
}

// Ask for microphone access up front so the prompt never lands in the middle of a call.
AVCaptureDevice.requestAccess(for: .audio) { granted in log("microphone permission: \(granted ? "granted" : "DENIED")") }

if let index = arguments.firstIndex(of: "--test") {
    let seconds = index + 1 < arguments.count ? Double(arguments[index + 1]) ?? 10 : 10
    let recording = Recording(trigger: "test")
    log("test recording for \(Int(seconds)) s -> \(recording.dir.lastPathComponent)")
    recording.start()
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
        recording.stop()
        recording.writeMeta(status: "test", reason: "test")
        let peaks = recording.peaks
        log(String(format: "test done: peaks system %.3f mic %.3f", peaks.system, peaks.mic))
        exit(0)
    }
    RunLoop.main.run()
}

log("Sales Coach Recorder started (pid \(getpid()))")
recoverInterruptedRecordings()
let controller = Controller()
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        log("received signal \(sig), stopping")
        controller.shutdown()
        exit(0)
    }
    source.resume()
    signalSources.append(source)
}
Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { _ in controller.tick() }
RunLoop.main.run()

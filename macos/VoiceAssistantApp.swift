import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Security
import SwiftUI
import UniformTypeIdentifiers

private enum ExternalLLMKeychain {
    static let service = "local.codex.voice-assistant.external-llm"

    private static func account(for canonicalEndpoint: String) -> String {
        "endpoint:\(canonicalEndpoint)"
    }

    enum KeychainError: LocalizedError {
        case unexpectedStatus(OSStatus)
        case invalidData

        var errorDescription: String? {
            switch self {
            case .unexpectedStatus(let status):
                let detail = SecCopyErrorMessageString(status, nil) as String?
                return detail ?? "Ошибка Связки ключей (код \(status))"
            case .invalidData:
                return "Сохранённый ключ API имеет неверный формат"
            }
        }
    }

    static func read(canonicalEndpoint: String) throws -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(for: canonicalEndpoint),
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
        guard let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else {
            throw KeychainError.invalidData
        }
        return value
    }

    static func save(_ value: String, canonicalEndpoint: String) throws {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(for: canonicalEndpoint),
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(updateStatus)
        }
        var item = query
        attributes.forEach { item[$0.key] = $0.value }
        let addStatus = SecItemAdd(item as CFDictionary, nil)
        guard addStatus == errSecSuccess else { throw KeychainError.unexpectedStatus(addStatus) }
    }

    static func delete(canonicalEndpoint: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(for: canonicalEndpoint),
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }
}

private enum ExternalLLMEndpoint {
    private static let casefoldLocale = Locale(identifier: "en_US_POSIX")

    private static func rawHostname(_ rawValue: String) -> String? {
        guard let schemeEnd = rawValue.range(of: "://")?.upperBound else { return nil }
        let remainder = rawValue[schemeEnd...]
        let authorityEnd = remainder.firstIndex { character in
            character == "/" || character == "?" || character == "#"
        } ?? remainder.endIndex
        var authority = String(remainder[..<authorityEnd])
        if let at = authority.lastIndex(of: "@") {
            authority = String(authority[authority.index(after: at)...])
        }
        if authority.hasPrefix("[") {
            guard let closing = authority.firstIndex(of: "]") else { return nil }
            return String(authority[authority.index(after: authority.startIndex)..<closing])
        }
        if let colon = authority.lastIndex(of: ":"),
           authority[authority.index(after: colon)...].allSatisfy(\.isNumber) {
            return String(authority[..<colon])
        }
        return authority
    }

    private static func normalizedHost(_ rawHost: String) -> String? {
        var host = rawHost
            .folding(options: [.caseInsensitive], locale: casefoldLocale)
            .lowercased()
        while host.hasSuffix(".") { host.removeLast() }
        guard !host.isEmpty else { return nil }

        if isIPAddress(host) { return host }
        if let percent = host.firstIndex(of: "%"), isIPv6Address(String(host[..<percent])) {
            let zone = host[host.index(after: percent)...]
            return zone.isEmpty ? nil : host
        }

        var idnaComponents = URLComponents()
        idnaComponents.scheme = "https"
        idnaComponents.host = host
        guard let idnaURL = idnaComponents.url,
              let asciiHost = idnaURL.host?.lowercased() else { return nil }
        let labels = asciiHost.split(separator: ".", omittingEmptySubsequences: false)
        guard asciiHost.count <= 253,
              labels.allSatisfy({ label in
                  !label.isEmpty
                      && label.count <= 63
                      && !label.hasPrefix("-")
                      && !label.hasSuffix("-")
                      && label.unicodeScalars.allSatisfy { scalar in
                          (48...57).contains(scalar.value)
                              || (97...122).contains(scalar.value)
                              || scalar.value == 45
                      }
              }) else { return nil }
        return asciiHost
    }

    private static func isIPAddress(_ host: String) -> Bool {
        var ipv4 = in_addr()
        if host.withCString({ inet_pton(AF_INET, $0, &ipv4) }) == 1 { return true }
        return isIPv6Address(host)
    }

    private static func isIPv6Address(_ host: String) -> Bool {
        var ipv6 = in6_addr()
        return host.withCString { inet_pton(AF_INET6, $0, &ipv6) } == 1
    }

    private static func isLoopbackHost(_ host: String) -> Bool {
        if host == "localhost" { return true }
        var ipv4 = in_addr()
        if host.withCString({ inet_pton(AF_INET, $0, &ipv4) }) == 1 {
            return withUnsafeBytes(of: &ipv4) { bytes in bytes.first == 127 }
        }
        let addressOnly = String(host.split(separator: "%", maxSplits: 1)[0])
        var ipv6 = in6_addr()
        guard addressOnly.withCString({ inet_pton(AF_INET6, $0, &ipv6) }) == 1 else { return false }
        return withUnsafeBytes(of: &ipv6) { rawBytes in
            let bytes = Array(rawBytes)
            let nativeLoopback = bytes.dropLast().allSatisfy { $0 == 0 } && bytes.last == 1
            let mappedIPv4Loopback = bytes.prefix(10).allSatisfy { $0 == 0 }
                && bytes[10] == 0xff && bytes[11] == 0xff && bytes[12] == 127
            return nativeLoopback || mappedIPv4Loopback
        }
    }

    static func isLoopback(_ rawValue: String) -> Bool {
        guard let rawHost = rawHostname(rawValue),
              let host = normalizedHost(rawHost) else { return false }
        return isLoopbackHost(host)
    }

    static func validationError(_ rawValue: String) -> String? {
        let trimmedValue = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmedValue.unicodeScalars.contains(where: {
            CharacterSet.whitespacesAndNewlines.contains($0) || $0.value < 32
        }) {
            return "Адрес API содержит недопустимые символы"
        }
        guard let components = URLComponents(string: trimmedValue),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let rawHost = rawHostname(trimmedValue),
              normalizedHost(rawHost) != nil,
              components.string != nil else {
            return "Укажите полный адрес HTTP(S), например https://api.example.com/v1"
        }
        if components.user != nil || components.password != nil {
            return "Не помещайте логин, пароль или ключ API в адрес"
        }
        if components.query?.isEmpty == false || components.fragment?.isEmpty == false {
            return "Укажите базовый адрес без query-параметров и фрагмента"
        }
        if let port = components.port, !(0...65_535).contains(port) {
            return "В адресе API указан некорректный порт"
        }
        if scheme == "http" && !isLoopback(rawValue) {
            return "Для удалённого провайдера обязателен HTTPS; HTTP разрешён только для localhost"
        }
        return nil
    }

    static func canonicalized(_ rawValue: String) -> String? {
        let trimmedValue = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard validationError(trimmedValue) == nil,
              var components = URLComponents(string: trimmedValue),
              let scheme = components.scheme?.lowercased(),
              let rawHost = rawHostname(trimmedValue),
              let host = normalizedHost(rawHost) else { return nil }

        components.scheme = scheme
        components.host = host.contains(":") ? "[\(host)]" : host
        if (scheme == "https" && components.port == 443)
            || (scheme == "http" && components.port == 80) {
            components.port = nil
        }
        let pathParts = components.percentEncodedPath.split(separator: "/", omittingEmptySubsequences: true)
        var path = pathParts.isEmpty ? "" : "/" + pathParts.joined(separator: "/")
        let completionSuffix = "/chat/completions"
        if path.lowercased().hasSuffix(completionSuffix) {
            path.removeLast(completionSuffix.count)
            while path.hasSuffix("/") { path.removeLast() }
        }
        components.percentEncodedPath = path
        components.query = nil
        components.fragment = nil
        return components.string
    }
}

enum RnDTheme {
    static let navy = Color(red: 0.000, green: 0.184, blue: 0.424)
    static let blue = Color(red: 0.114, green: 0.341, blue: 0.639)
    static let steel = Color(red: 0.576, green: 0.667, blue: 0.780)
    static let red = Color(red: 1.000, green: 0.000, blue: 0.000)
    static let canvas = Color(red: 0.957, green: 0.969, blue: 0.984)
    static let panel = Color.white
    static let ink = Color(red: 0.047, green: 0.125, blue: 0.243)
    static let line = Color(red: 0.855, green: 0.890, blue: 0.929)
}

enum AssistantState: String {
    case starting, loading, ready, calibrating, listening, transcribing, thinking, speaking, stopping, error

    var color: Color {
        switch self {
        case .listening: return RnDTheme.blue
        case .thinking, .transcribing: return RnDTheme.steel
        case .speaking: return RnDTheme.navy
        case .error: return RnDTheme.red
        case .ready: return RnDTheme.blue
        default: return RnDTheme.steel
        }
    }

    var symbol: String {
        switch self {
        case .listening: return "waveform.circle.fill"
        case .thinking, .transcribing: return "sparkles"
        case .speaking: return "speaker.wave.2.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .ready: return "mic.circle.fill"
        default: return "circle.dotted"
        }
    }

    var isBusy: Bool {
        [.starting, .loading, .calibrating, .transcribing, .thinking, .speaking, .stopping].contains(self)
    }
}

enum AppSection: String, CaseIterable, Identifiable {
    case today, workspaces, tasks, meetings, search, inbox, skills, capabilities, artifacts, automations, approvals, settings
    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "Сегодня"
        case .workspaces: return "Рабочие пространства"
        case .tasks: return "Задачи агента"
        case .meetings: return "Встречи"
        case .search: return "Поиск / Исследования"
        case .inbox: return "Уведомления"
        case .skills: return "Скиллы"
        case .capabilities: return "Навыки"
        case .artifacts: return "Материалы"
        case .automations: return "Автоматизации"
        case .approvals: return "Согласования"
        case .settings: return "Настройки"
        }
    }

    var icon: String {
        switch self {
        case .today: return "sun.max.fill"
        case .workspaces: return "square.stack.3d.up.fill"
        case .tasks: return "checklist"
        case .meetings: return "person.2.wave.2.fill"
        case .search: return "magnifyingglass"
        case .inbox: return "tray.full.fill"
        case .skills: return "wand.and.stars"
        case .capabilities: return "puzzlepiece.extension.fill"
        case .artifacts: return "doc.richtext.fill"
        case .automations: return "clock.arrow.2.circlepath"
        case .approvals: return "checkmark.shield.fill"
        case .settings: return "gearshape.fill"
        }
    }
}

struct AppNavigationRequest: Equatable {
    let id = UUID()
    let section: AppSection
}

struct SidebarGroup: Identifiable {
    let title: String
    let items: [AppSection]
    var id: String { title }
}

private let sidebarGroups = [
    SidebarGroup(title: "Работа", items: [.today, .workspaces, .tasks]),
    SidebarGroup(title: "Знания", items: [.meetings, .search, .artifacts]),
    SidebarGroup(title: "Инструменты", items: [.skills, .capabilities, .automations]),
    SidebarGroup(title: "Контроль", items: [.inbox, .approvals]),
    SidebarGroup(title: "Система", items: [.settings]),
]

enum CompactMode: String, CaseIterable, Identifiable {
    case voice, chat
    var id: String { rawValue }
    var title: String { self == .voice ? "Голос" : "Чат" }
    var icon: String { self == .voice ? "waveform" : "bubble.left.fill" }
}

enum AssistantPresentationMode: String {
    case full, compact
}

private enum SystemTextInsertionResult {
    case accessibility
    case keyboardUnverified
    case failed(String)

    var statusText: String {
        switch self {
        case .accessibility:
            return "Текст вставлен в активное поле"
        case .keyboardUnverified:
            return "Команда вставки отправлена; проверьте поле"
        case .failed(let message):
            return message
        }
    }
}

private final class SystemTextInserter {
    func captureFocusedElement() -> AXUIElement? {
        guard AXIsProcessTrusted() else { return nil }
        let systemWide = AXUIElementCreateSystemWide()
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            systemWide,
            kAXFocusedUIElementAttribute as CFString,
            &value
        ) == .success,
        let value,
        CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        let element = value as! AXUIElement
        return isEditableTarget(element) ? element : nil
    }

    func insert(_ rawText: String, into capturedElement: AXUIElement?) -> SystemTextInsertionResult {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return .failed("Речь не распознана") }
        guard AXIsProcessTrusted() else {
            return .failed("Для вставки текста нужен доступ «Универсальный доступ»")
        }

        guard let element = capturedElement ?? captureFocusedElement() else {
            return .failed("Не найдено активное редактируемое поле для вставки")
        }
        guard isEditableTarget(element) else {
            return .failed("Активный элемент не поддерживает ввод текста")
        }
        guard !isSensitiveField(element) else {
            return .failed("Вставка диктовки в парольные и защищённые поля отключена")
        }
        guard let focusedNow = captureFocusedElement(), CFEqual(focusedNow, element) else {
            return .failed("Фокус изменился — текст не вставлен")
        }
        guard !isSensitiveField(focusedNow) else {
            return .failed("Вставка диктовки в парольные и защищённые поля отключена")
        }
        if insertWithAccessibility(text, into: element) { return .accessibility }
        return insertWithKeyboardEvents(text, into: element)
    }

    func isEditableTarget(_ element: AXUIElement) -> Bool {
        if boolAttribute(kAXEnabledAttribute as String, of: element) == false { return false }
        if boolAttribute("AXReadOnly", of: element) == true { return false }
        if boolAttribute("AXEditable", of: element) == false { return false }

        let editableRoles: Set<String> = [
            "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXTextView",
        ]
        let role = stringAttribute(kAXRoleAttribute as String, of: element) ?? ""

        var selectedTextIsSettable = DarwinBoolean(false)
        let selectedTextStatus = AXUIElementIsAttributeSettable(
            element,
            kAXSelectedTextAttribute as CFString,
            &selectedTextIsSettable
        )
        var valueIsSettable = DarwinBoolean(false)
        let valueStatus = AXUIElementIsAttributeSettable(
            element,
            kAXValueAttribute as CFString,
            &valueIsSettable
        )
        return editableRoles.contains(role)
            || (selectedTextStatus == .success && selectedTextIsSettable.boolValue)
            || (valueStatus == .success && valueIsSettable.boolValue)
    }

    func isSensitiveField(_ element: AXUIElement) -> Bool {
        let attributes = [
            kAXRoleAttribute as String,
            kAXSubroleAttribute as String,
            kAXDescriptionAttribute as String,
            kAXTitleAttribute as String,
            kAXHelpAttribute as String,
            "AXIdentifier",
            "AXDOMIdentifier",
            "AXDOMClassList",
        ]
        let descriptors = attributes.compactMap { stringAttribute($0, of: element) }
            .joined(separator: " ")
            .lowercased()
        if descriptors.contains((kAXSecureTextFieldSubrole as String).lowercased()) {
            return true
        }
        let protectedMarkers = [
            "password", "passwd", "passcode", "securetext", "secure text",
            "one-time code", "otp", "pin code", "credit card", "cvv",
            "парол", "пин-код", "одноразов", "код подтверждения",
        ]
        return protectedMarkers.contains { descriptors.contains($0) }
    }

    private func stringAttribute(_ attribute: String, of element: AXUIElement) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let value else { return nil }
        if let text = value as? String { return text }
        if let values = value as? [String] { return values.joined(separator: " ") }
        return nil
    }

    private func boolAttribute(_ attribute: String, of element: AXUIElement) -> Bool? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let value else { return nil }
        if CFGetTypeID(value) == CFBooleanGetTypeID() {
            return CFBooleanGetValue((value as! CFBoolean))
        }
        return nil
    }

    private func insertWithAccessibility(_ text: String, into element: AXUIElement) -> Bool {
        var selectedTextIsSettable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(
            element,
            kAXSelectedTextAttribute as CFString,
            &selectedTextIsSettable
        ) == .success,
        selectedTextIsSettable.boolValue else { return false }
        return AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            text as CFString
        ) == .success
    }

    private func insertWithKeyboardEvents(
        _ text: String,
        into capturedElement: AXUIElement
    ) -> SystemTextInsertionResult {
        guard CGPreflightPostEventAccess() else {
            return .failed("Активное поле не поддерживает прямую вставку; разрешите управление компьютером для резервного ввода")
        }
        guard isEditableTarget(capturedElement), !isSensitiveField(capturedElement),
              let focusedNow = captureFocusedElement(), CFEqual(focusedNow, capturedElement),
              isEditableTarget(focusedNow), !isSensitiveField(focusedNow) else {
            return .failed("Фокус или тип поля изменился — текст не вставлен")
        }
        var targetPid: pid_t = 0
        guard AXUIElementGetPid(capturedElement, &targetPid) == .success,
              targetPid > 0 else {
            return .failed("Не удалось определить приложение для резервного ввода")
        }
        guard let source = CGEventSource(stateID: .combinedSessionState),
              let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
            return .failed("Не удалось подготовить резервный ввод текста")
        }
        let utf16 = Array(text.utf16)
        guard !utf16.isEmpty else { return .failed("Речь не распознана") }
        utf16.withUnsafeBufferPointer { buffer in
            guard let address = buffer.baseAddress else { return }
            keyDown.keyboardSetUnicodeString(
                stringLength: buffer.count,
                unicodeString: address
            )
            keyUp.keyboardSetUnicodeString(
                stringLength: 0,
                unicodeString: address
            )
        }
        // Target the captured process, not the globally active app, and avoid
        // exposing potentially corporate text to the system pasteboard.
        keyDown.postToPid(targetPid)
        keyUp.postToPid(targetPid)
        // Quartz does not acknowledge whether the focused control accepted the
        // Unicode event. The caller keeps a review copy instead of claiming
        // verified delivery.
        return .keyboardUnverified
    }
}

private final class GlobalPushToTalkMonitor {
    static let rightOptionKeyCode: CGKeyCode = 61

    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    var onStatusChange: ((_ accessibility: Bool, _ inputMonitoring: Bool, _ eventPosting: Bool, _ running: Bool) -> Void)?

    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var rightOptionIsDown = false

    deinit { stop() }

    func refresh(requestPermission: Bool = false) {
        if requestPermission {
            let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            _ = CGRequestListenEventAccess()
            _ = CGRequestPostEventAccess()
        }

        let accessibility = AXIsProcessTrusted()
        let inputMonitoring = CGPreflightListenEventAccess()
        let eventPosting = CGPreflightPostEventAccess()
        guard accessibility, inputMonitoring else {
            stop(notify: false)
            onStatusChange?(accessibility, inputMonitoring, eventPosting, false)
            return
        }
        if eventTap == nil { installEventTap() }
        onStatusChange?(accessibility, inputMonitoring, eventPosting, eventTap != nil)
    }

    func stop() { stop(notify: true) }

    private func installEventTap() {
        let mask = CGEventMask(1) << CGEventType.flagsChanged.rawValue
        eventTap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: Self.callback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        )
        guard let eventTap else { return }
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, eventTap, 0)
        runLoopSource = source
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: eventTap, enable: true)
    }

    private func stop(notify: Bool) {
        if rightOptionIsDown {
            rightOptionIsDown = false
            DispatchQueue.main.async { [weak self] in self?.onRelease?() }
        }
        if let eventTap { CGEvent.tapEnable(tap: eventTap, enable: false) }
        if let runLoopSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), runLoopSource, .commonModes) }
        runLoopSource = nil
        eventTap = nil
        if notify {
            onStatusChange?(
                AXIsProcessTrusted(),
                CGPreflightListenEventAccess(),
                CGPreflightPostEventAccess(),
                false
            )
        }
    }

    private func handle(type: CGEventType, event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            // A disabled tap may swallow the physical key-up notification.
            // End an active capture immediately instead of recording until the
            // backend's 120-second safety cap and inserting unexpected text.
            if rightOptionIsDown {
                rightOptionIsDown = false
                DispatchQueue.main.async { [weak self] in self?.onRelease?() }
            }
            if let eventTap { CGEvent.tapEnable(tap: eventTap, enable: true) }
            return
        }
        guard type == .flagsChanged,
              CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode)) == Self.rightOptionKeyCode else {
            return
        }
        // Device-independent flags cannot distinguish left and right Option.
        // Query the physical key so releasing Right Option always stops PTT,
        // even while Left Option remains held.
        let isDown = CGEventSource.keyState(
            .combinedSessionState,
            key: Self.rightOptionKeyCode
        )
        guard isDown != rightOptionIsDown else { return }
        rightOptionIsDown = isDown
        DispatchQueue.main.async { [weak self] in
            if isDown { self?.onPress?() } else { self?.onRelease?() }
        }
    }

    private static let callback: CGEventTapCallBack = { _, type, event, userInfo in
        guard let userInfo else { return Unmanaged.passUnretained(event) }
        let monitor = Unmanaged<GlobalPushToTalkMonitor>.fromOpaque(userInfo).takeUnretainedValue()
        monitor.handle(type: type, event: event)
        return Unmanaged.passUnretained(event)
    }
}

enum MessageRole { case user, assistant }

enum DataClassification: String, CaseIterable, Identifiable {
    case `public`
    case `internal`
    case confidential
    case restricted

    var id: String { rawValue }
    var title: String {
        switch self {
        case .public: return "Публичные"
        case .internal: return "Внутренние"
        case .confidential: return "Конфиденциальные"
        case .restricted: return "Строго ограниченные"
        }
    }
    var shortTitle: String {
        switch self {
        case .public: return "Публичные"
        case .internal: return "Внутренние"
        case .confidential: return "Конфиденц."
        case .restricted: return "Ограниченные"
        }
    }
    var icon: String {
        switch self {
        case .public: return "globe"
        case .internal: return "building.2"
        case .confidential: return "lock.shield"
        case .restricted: return "lock.fill"
        }
    }
    var color: Color {
        switch self {
        case .public: return RnDTheme.blue
        case .internal: return RnDTheme.steel
        case .confidential: return .orange
        case .restricted: return RnDTheme.red
        }
    }
}

enum MemoryKind: String, CaseIterable, Identifiable {
    case note
    case preference
    case fact
    case commitment
    case explicit
    case taskResult = "task_result"

    var id: String { rawValue }
    var title: String {
        switch self {
        case .note: return "Рабочая заметка"
        case .preference: return "Предпочтение"
        case .fact: return "Факт"
        case .commitment: return "Обязательство"
        case .explicit: return "Явно сохранённое"
        case .taskResult: return "Результат задачи"
        }
    }
}

struct ChatMessage: Identifiable {
    let id: String
    let role: MessageRole
    var text: String
    var isStreaming = false
    var wasInterrupted = false
    var sources: [EntityRecord] = []

    init(id: String = UUID().uuidString, role: MessageRole, text: String, isStreaming: Bool = false, wasInterrupted: Bool = false, sources: [EntityRecord] = []) {
        self.id = id
        self.role = role
        self.text = text
        self.isStreaming = isStreaming
        self.wasInterrupted = wasInterrupted
        self.sources = sources
    }
}

struct WorkspaceRecord: Identifiable, Hashable {
    let id: String
    let name: String
    let description: String
    let status: String
    var classification = "internal"
}

struct TaskRecord: Identifiable, Hashable {
    let id: String
    let workspaceID: String
    let title: String
    let status: String
    let plan: [String]
    let result: String
    let skillID: String?
    let updatedAt: String
    var classification = "internal"
}

struct MeetingRecord: Identifiable, Hashable {
    let id: String
    let workspaceID: String
    let sourceID: String
    let title: String
    let occurredAt: String
    let participants: [String]
    let summary: String
    let status: String
    let analyzedAt: String
    let itemCounts: [String: Int]
    let openAttention: Int
    let sourcePath: String?
}

struct MeetingItemRecord: Identifiable, Hashable {
    let id: String
    let meetingID: String
    let kind: String
    let text: String
    let owner: String
    let dueAt: String
    let topic: String
    let status: String
    let sourceQuote: String
    let sourceStart: Int?
    let sourceEnd: Int?
    let confidence: Double?
}

struct AttentionEventRecord: Identifiable, Hashable {
    let key: String
    let title: String
    let reason: String
    let score: Double
    let severity: String
    let kind: String
    let entityID: String
    let workspaceID: String
    let sourceID: String
    let sourcePath: String?
    let dueAt: String
    let actionLabel: String

    var id: String { key }

    var sourceRecord: EntityRecord? {
        guard let sourcePath, !sourcePath.isEmpty else { return nil }
        return EntityRecord(
            id: sourceID.isEmpty ? "attention-source-\(key)" : sourceID,
            title: title,
            subtitle: "Источник приоритета",
            kind: kind,
            path: sourcePath
        )
    }
}

struct EntityRecord: Identifiable, Hashable {
    let id: String
    let title: String
    var subtitle = ""
    var detail = ""
    var content = ""
    var status = ""
    var kind = ""
    var path: String?
    var sourceRef: String?
    var chunkID: String?
    var charStart: Int?
    var charEnd: Int?
    var excerpt = ""
    var command = ""
    var risk = ""
    var enabled = true
    var version = 0
    var classification = "internal"

    var hasExactExcerpt: Bool {
        !excerpt.isEmpty && charStart != nil && charEnd != nil
    }
}

struct PilotPreflightCheckRecord: Identifiable, Hashable {
    let id: String
    let title: String
    let status: String
    let detail: String
    let action: String
}

struct ArtifactVersionRecord: Identifiable, Hashable {
    let id: String
    let artifactID: String
    let version: Int
    let path: String?
    let createdAt: String
    let isCurrent: Bool
    let restoredFromVersion: Int?
    let metadataSummary: String
}

struct ArtifactRelationRecord: Identifiable, Hashable {
    let id: String
    let artifactVersion: Int
    let relationType: String
    let taskID: String?
    let sourceID: String?
    let relatedArtifactID: String?
    let relatedArtifactVersion: Int?
    let metadataSummary: String
    let createdAt: String
}

struct ComposerSuggestion: Identifiable, Hashable {
    enum Kind: String, Hashable { case skill, source }
    let id: String
    let kind: Kind
    let title: String
    let subtitle: String
    let insertion: String
}

struct PendingAttachmentRecord: Identifiable, Hashable {
    let id: String
    let path: String
    let name: String
    let kind: String?
}

struct QuickActionRecord: Identifiable, Hashable {
    let id: String
    let title: String
    let command: String
    let taskID: String?
    let artifactID: String?
}

struct WorkspaceTimelineRecord: Identifiable, Hashable {
    let id: String
    let type: String
    let title: String
    let detail: String
    let timestamp: String
    let status: String
    let targetSection: String
    let targetType: String
    let targetID: String
    let sourceID: String?
    let sourceTitle: String?
    let sourcePath: String?
    let sourceStart: Int?
    let sourceEnd: Int?
    let sourceExcerpt: String
    let decisionThreadKey: String?
    let decisionSequence: Int?
    let decisionCount: Int?
    let isCurrentDecision: Bool
    let currentDecisionText: String

    var sourceRecord: EntityRecord? {
        guard let sourceID, !sourceID.isEmpty else { return nil }
        return EntityRecord(
            id: sourceID,
            title: sourceTitle?.isEmpty == false ? sourceTitle! : title,
            subtitle: type == "decision" || type == "meeting" ? "meeting" : "document",
            kind: type == "decision" || type == "meeting" ? "meeting" : "document",
            path: sourcePath,
            charStart: sourceStart,
            charEnd: sourceEnd,
            excerpt: sourceExcerpt
        )
    }
}

struct DashboardStats {
    var activeTasks = 0
    var attention = 0
    var sources = 0
    var artifacts = 0
}

@MainActor
final class BackendController: ObservableObject {
    @Published var state: AssistantState = .starting
    @Published var statusText = "Запускаю локальный движок…"
    @Published var messages: [ChatMessage] = []
    @Published var isSessionActive = false
    @Published var isReady = false
    @Published var errorMessage: String?
    @Published var speechErrorMessage: String?
    @Published var speechErrorRetryable = true
    @Published var speechErrorTaskID: String?
    @Published var lastAssistantSpoken: Bool?
    @Published var sttSeconds: Double?
    @Published var firstTokenSeconds: Double?
    @Published var firstAudioSeconds: Double?
    @Published var responseSeconds: Double?
    @Published var actualLLMRoute = "local_mlx"
    @Published var actualLLMModel = ""
    @Published var routingFallbackMessage: String?
    @Published private(set) var javaCorePolicyConfigured = false
    @Published private(set) var javaCorePolicyReady = false
    @Published private(set) var javaActionJournalReady = false
    @Published private(set) var javaAutonomyPolicyReady = false
    @Published private(set) var actionRecoveryAttention = 0
    @Published private(set) var dictationReviewSequence = 0
    @Published private(set) var presentationMode: AssistantPresentationMode = .full
    @Published private(set) var accessibilityPermissionGranted = false
    @Published private(set) var inputMonitoringPermissionGranted = false
    @Published private(set) var eventPostingPermissionGranted = false
    @Published private(set) var pushToTalkMonitorRunning = false
    @Published private(set) var externalDictationStartPending = false
    @Published private(set) var externalDictationActive = false
    @Published private(set) var externalDictationTranscribing = false
    @Published private(set) var externalDictationStatus: String?
    @Published var loadedModels: Set<String> = []
    @Published var workspaces: [WorkspaceRecord] = []
    @Published var tasks: [TaskRecord] = []
    @Published var meetings: [MeetingRecord] = []
    @Published var meetingItems: [MeetingItemRecord] = []
    @Published var meetingDiff: [EntityRecord] = []
    @Published var meetingBriefing: String?
    @Published var attentionEvents: [AttentionEventRecord] = []
    @Published var sources: [EntityRecord] = []
    @Published var memory: [EntityRecord] = []
    @Published var skills: [EntityRecord] = []
    @Published var capabilities: [EntityRecord] = []
    @Published var artifacts: [EntityRecord] = []
    @Published var inbox: [EntityRecord] = []
    @Published var approvals: [EntityRecord] = []
    @Published var automations: [EntityRecord] = []
    @Published var taskEvents: [EntityRecord] = []
    @Published var audit: [EntityRecord] = []
    @Published var searchResults: [EntityRecord] = []
    @Published var activeSources: [EntityRecord] = []
    @Published var sourcePreview: EntityRecord?
    @Published var navigationRequest: AppNavigationRequest?
    @Published private(set) var artifactHistoryArtifact: EntityRecord?
    @Published private(set) var artifactVersions: [ArtifactVersionRecord] = []
    @Published private(set) var artifactRelations: [ArtifactRelationRecord] = []
    @Published private(set) var artifactHistoryLoading = false
    @Published private(set) var artifactRelationsLoading = false
    @Published private(set) var artifactRestorePendingVersion: Int?
    @Published private(set) var artifactHistoryError: String?
    @Published private(set) var quickActions: [QuickActionRecord] = []
    @Published private(set) var quickActionPendingID: String?
    @Published private(set) var completedQuickActionKeys: Set<String> = []
    @Published private(set) var workspaceTimeline: [WorkspaceTimelineRecord] = []
    @Published var currentWorkspaceID = ""
    @Published var currentTaskID: String?
    @Published var currentMeetingID: String?
    @Published var settings: [String: String] = [:]
    @Published private(set) var pilotMetrics: [String: Any] = [:]
    @Published private(set) var pilotPreflightOverall = "limited"
    @Published private(set) var pilotPreflightChecks: [PilotPreflightCheckRecord] = []
    @Published private(set) var pilotOnboardingStatus = "active"
    @Published private(set) var pilotOnboardingTitle = "Быстрый старт"
    @Published private(set) var pilotOnboardingDetail = "Определяю следующий полезный шаг."
    @Published private(set) var pilotOnboardingActionID = ""
    @Published private(set) var pilotOnboardingActionLabel = ""
    @Published private(set) var pilotOnboardingCompleted = 0
    @Published private(set) var pilotOnboardingTotal = 4
    @Published var dashboard = DashboardStats()
    @Published var modelName = "Локальная MLX"
    @Published var composerDraft = ""
    @Published var composerSuggestionIndex = 0
    @Published private(set) var pendingAttachments: [PendingAttachmentRecord] = []
    @Published var llmConfigurationError: String?
    @Published var llmConfigurationPending = false
    @Published var externalLLMReady = true
    @Published var hasExternalLLMAPIKey = false
    @Published private(set) var isLLMTurnPending = false
    @Published private(set) var isVoiceStartPending = false
    @Published private(set) var meetingAudioImportInProgress = false
    @Published private(set) var meetingAudioImportStage: String?
    @Published private(set) var expressConnectorConfigured = false
    @Published private(set) var expressConnectorConnected = false
    @Published private(set) var expressSyncInProgress = false
    private var meetingImportKind = "audio"

    private var process: Process?
    private var inputPipe: Pipe?
    private var outputBuffer = ""
    private let ioQueue = DispatchQueue(label: "local.voice.backend.output")
    private var stderrTail = ""
    private var didHydrateExternalLLM = false
    private var pendingLLMMode: String?
    private var pendingCredentialDeletionEndpoint: String?
    private var meetingIDsBeforeAudioImport: Set<String> = []
    private var pendingImportedMeetingID: String?
    private var pendingImportedMeetingSourceID: String?
    private var submittedAttachmentIDs: Set<String> = []
    private var pendingQuickAction: QuickActionRecord?
    private let systemTextInserter = SystemTextInserter()
    private var externalDictationTarget: AXUIElement?
    private var externalDictationFocusChanged = false
    private var globalPushToTalkMonitor: GlobalPushToTalkMonitor?

    init() {
        launch()
        configureGlobalPushToTalk()
    }

    var llmMode: String { settings["llm_mode"] ?? "local" }

    var pilotMetricSampleCount: Int { int(pilotMetrics["sample_count"]) }
    private var pilotUsage: [String: Any] {
        pilotMetrics["usage"] as? [String: Any] ?? [:]
    }
    var pilotActiveDays: Int { int(pilotUsage["active_days"]) }
    var pilotCompletedTurns: Int { int(pilotUsage["completed_turns"]) }
    var pilotUsefulnessRating: Int { int(pilotUsage["usefulness_rating"]) }
    var pilotUsageSummaryLabel: String {
        let voiceTurns = int(pilotUsage["voice_turns"])
        let meetingImports = int(pilotUsage["meeting_imports"])
        let meetingBriefings = int(pilotUsage["meeting_briefings"])
        let observedExits = int(pilotUsage["observed_session_exits"])
        let cleanExits = int(pilotUsage["clean_session_exits"])
        let crashFreeRate = number(pilotUsage["crash_free_session_rate"])
        let firstValue = number(pilotUsage["first_value_seconds"])
        var parts = [
            "активных дней: \(pilotActiveDays)",
            "запросов: \(pilotCompletedTurns)",
            "голосом: \(voiceTurns)",
        ]
        if meetingImports > 0 { parts.append("встреч импортировано: \(meetingImports)") }
        if meetingBriefings > 0 { parts.append("брифингов: \(meetingBriefings)") }
        if observedExits > 0, let crashFreeRate {
            parts.append(String(
                format: "штатных завершений %.1f%% (%d/%d)",
                crashFreeRate * 100,
                cleanExits,
                observedExits
            ))
        } else {
            parts.append("надёжность: ожидает завершений")
        }
        if let firstValue { parts.append(String(format: "первый результат %.1f мин", firstValue / 60)) }
        return parts.joined(separator: " · ")
    }

    var pilotPreflightOverallLabel: String {
        switch pilotPreflightOverall {
        case "ready": return "Готово"
        case "blocked": return "Не готово"
        default: return "Ограниченно"
        }
    }

    var pilotPreflightSummaryLabel: String {
        let blockers = pilotPreflightChecks.filter { $0.status == "block" }.count
        let attention = pilotPreflightChecks.filter {
            $0.status == "warn" || $0.status == "unverified"
        }.count
        guard !pilotPreflightChecks.isEmpty else {
            return "Определяю готовность этой установки к пилоту."
        }
        return "Проверок: \(pilotPreflightChecks.count) · блокирует: \(blockers) · требует внимания: \(attention)"
    }

    var pilotOnboardingProgressLabel: String {
        "\(min(pilotOnboardingCompleted, pilotOnboardingTotal)) из \(pilotOnboardingTotal)"
    }

    var pilotMetricsSummaryLabel: String {
        let metrics = pilotMetrics["metrics"] as? [String: Any] ?? [:]
        func statistic(_ metric: String, _ name: String) -> Double? {
            guard let values = metrics[metric] as? [String: Any] else { return nil }
            return number(values[name])
        }
        var parts: [String] = []
        if let value = statistic("listen_ready_seconds", "p95") {
            parts.append(String(format: "готовность p95 %.2f с", value))
        }
        if let p50 = statistic("transcript_ready_seconds", "p50"),
           let p95 = statistic("transcript_ready_seconds", "p95") {
            parts.append(String(format: "текст p50/p95 %.2f/%.2f с", p50, p95))
        }
        if let p50 = statistic("first_audio_seconds", "p50"),
           let p95 = statistic("first_audio_seconds", "p95") {
            parts.append(String(format: "звук p50/p95 %.2f/%.2f с", p50, p95))
        }
        if let p95 = statistic("tts_rtf", "p95") {
            parts.append(String(format: "TTS RTF p95 %.2f", p95))
        }
        if let maximum = statistic("output_clipping_ratio", "max") {
            parts.append(String(format: "клиппинг %.3f%%", maximum * 100))
        }
        return parts.isEmpty
            ? "Сделайте несколько голосовых запросов на этом устройстве."
            : parts.joined(separator: " · ")
    }
    var externalLLMBaseURL: String { settings["external_llm_base_url"] ?? "" }
    var externalLLMModel: String { settings["external_llm_model"] ?? "" }
    var externalProviderType: String { settings["external_provider_type"] ?? "external" }
    var autoRemotePolicy: String { settings["auto_remote_policy"] ?? "local_only" }
    var isExternalLLMActive: Bool { llmMode == "external" }
    var isAutoLLMActive: Bool { llmMode == "auto" }
    var isCorporateLLMActive: Bool {
        (isExternalLLMActive || isAutoLLMActive) && externalProviderType == "corporate"
    }
    var isRemoteRouteActive: Bool {
        actualLLMRoute == "corporate_api" || actualLLMRoute == "external_api"
    }
    var actualLLMRouteLabel: String {
        switch actualLLMRoute {
        case "corporate_api": return "Корпоративная API"
        case "external_api": return "Внешняя API"
        case "local_api": return "Локальная API"
        case "local_deterministic": return "Локальный навык"
        default: return "Локальная MLX"
        }
    }
    var actualLLMRouteIcon: String {
        switch actualLLMRoute {
        case "corporate_api": return "building.2.fill"
        case "external_api": return "network"
        case "local_api": return "server.rack"
        case "local_deterministic": return "bolt.fill"
        default: return "lock.shield.fill"
        }
    }
    var actualLLMRouteStatusLabel: String {
        let route = isAutoLLMActive ? "Авто → \(actualLLMRouteLabel)" : actualLLMRouteLabel
        if routingFallbackMessage != nil { return "Резерв → \(actualLLMRouteLabel)" }
        if javaCorePolicyConfigured && !javaCorePolicyReady {
            return "\(route) · резервная политика"
        }
        return route
    }
    var compactLLMRouteStatusLabel: String {
        let shortRoute: String
        switch actualLLMRoute {
        case "corporate_api": shortRoute = "корпоративно"
        case "external_api": shortRoute = "внешне"
        case "local_api": shortRoute = "локальная API"
        case "local_deterministic": shortRoute = "локальный навык"
        default: shortRoute = "локально"
        }
        if routingFallbackMessage != nil { return "Резерв → \(shortRoute)" }
        let route = isAutoLLMActive ? "Авто → \(shortRoute)" : shortRoute
        return javaCorePolicyConfigured && !javaCorePolicyReady
            ? "\(route) · резерв" : route
    }
    var compactActivityLabel: String {
        switch state {
        case .starting, .loading: return "Загрузка моделей"
        case .ready: return "Готов к работе"
        case .calibrating: return "Калибровка"
        case .listening: return "Слушаю"
        case .transcribing: return "Распознаю"
        case .thinking: return "Думаю"
        case .speaking: return "Отвечаю голосом"
        case .stopping: return "Останавливаю"
        case .error: return "Нужна настройка"
        }
    }
    var javaCorePolicyStatusLabel: String {
        if javaCorePolicyReady { return "Java 21 · активна" }
        if javaCorePolicyConfigured { return "Встроенная резервная политика" }
        return "Не настроена в development-режиме"
    }
    var javaActionJournalStatusLabel: String {
        if actionRecoveryAttention > 0 {
            return "Нужна сверка: \(actionRecoveryAttention)"
        }
        if javaActionJournalReady && javaAutonomyPolicyReady {
            return "Java 21 · политика и защита от дублей активны"
        }
        if javaActionJournalReady {
            return "Журнал Java 21 активен · политика резервная"
        }
        return "Недоступен · внешние действия блокируются"
    }
    var routeStatusHelp: String {
        if let routingFallbackMessage { return routingFallbackMessage }
        if javaCorePolicyConfigured && !javaCorePolicyReady {
            return "Java core недоступен; действует проверенная резервная Python-политика"
        }
        return javaCorePolicyReady
            ? "Маршрут проверен общей политикой Java 21"
            : "Фактический маршрут последнего ответа"
    }
    var configuredLLMModeLabel: String {
        if isAutoLLMActive {
            return autoRemotePolicy == "eligible" ? "Авто · безопасная маршрутизация" : "Авто · только локально"
        }
        if isCorporateLLMActive { return "Корпоративная API" }
        if isExternalLLMActive { return "Внешняя API" }
        return "Локальная MLX"
    }
    var isExternalDictationBusy: Bool {
        externalDictationStartPending || externalDictationActive || externalDictationTranscribing
    }
    var canChangeLLMConfiguration: Bool {
        isReady && !meetingAudioImportInProgress && !expressSyncInProgress && !isSessionActive
            && !isVoiceStartPending && !isLLMTurnPending && !isExternalDictationBusy
            && !state.isBusy && !llmConfigurationPending
    }
    var canSendText: Bool {
        isReady && !meetingAudioImportInProgress && !expressSyncInProgress && !isSessionActive
            && !isVoiceStartPending && !isLLMTurnPending && !isExternalDictationBusy
            && !state.isBusy && !llmConfigurationPending
            && (!isExternalLLMActive || externalLLMReady)
    }
    var canToggleVoiceSession: Bool {
        if isSessionActive || isVoiceStartPending { return true }
        return isReady && !meetingAudioImportInProgress && !expressSyncInProgress
            && !isVoiceStartPending && !isLLMTurnPending && !isExternalDictationBusy
            && !state.isBusy && !llmConfigurationPending
            && (!isExternalLLMActive || externalLLMReady)
    }
    var canRetrySpeech: Bool {
        speechErrorRetryable && isReady
            && !isSessionActive && !isVoiceStartPending
            && !isLLMTurnPending && !state.isBusy && !isExternalDictationBusy
    }
    var speechRetryUnavailableReason: String? {
        if !speechErrorRetryable { return "Повтор для этого ответа недоступен." }
        if isSessionActive || isVoiceStartPending {
            return "Сначала остановите голосовой режим."
        }
        if isExternalDictationBusy {
            return "Дождитесь завершения глобальной диктовки."
        }
        if isLLMTurnPending || state.isBusy {
            return "Дождитесь завершения текущего ответа."
        }
        if !isReady { return "Локальный движок пока не готов." }
        return nil
    }
    var voiceSessionActionLabel: String {
        if isVoiceStartPending { return "Отменить запуск голосового режима" }
        return isSessionActive ? "Остановить голосовой режим" : "Начать голосовой разговор"
    }
    var voiceSessionActionHint: String {
        if isVoiceStartPending { return "Отменяет запуск микрофона" }
        return isSessionActive
            ? "Останавливает прослушивание"
            : "Включает микрофон и начинает прослушивание"
    }
    var voiceSessionAccessibilityValue: String {
        if isVoiceStartPending { return "Запускается" }
        return isSessionActive ? "Микрофон включён" : "Микрофон выключен"
    }
    var globalPushToTalkReady: Bool {
        accessibilityPermissionGranted && inputMonitoringPermissionGranted
            && pushToTalkMonitorRunning
    }
    var globalPushToTalkStatusLabel: String {
        if !accessibilityPermissionGranted { return "Нужен доступ «Универсальный доступ»" }
        if !inputMonitoringPermissionGranted { return "Нужен «Мониторинг ввода»" }
        if !pushToTalkMonitorRunning { return "Глобальная клавиша недоступна" }
        if externalDictationStartPending { return "Запускаю диктовку…" }
        if externalDictationActive { return "Диктовка активна" }
        if externalDictationTranscribing { return "Распознаю диктовку…" }
        return "Готово · удерживайте правую ⌥"
    }
    var globalPushToTalkStatusDetail: String {
        if !accessibilityPermissionGranted || !inputMonitoringPermissionGranted {
            return "macOS должна разрешить чтение глобальной клавиши и вставку текста в активное поле. Аудио и распознавание остаются на устройстве."
        }
        if !eventPostingPermissionGranted {
            return "Прямая AX-вставка доступна. Для полей без AX-поддержки разрешите управление компьютером — тогда сработает безопасный Cmd+V fallback."
        }
        return "Удерживайте только правую клавишу Option, говорите и отпустите. Текст попадёт в поле, которое было активно до начала диктовки."
    }
    var canImportMeetingAudio: Bool {
        isReady && !meetingAudioImportInProgress && !isSessionActive
            && !isVoiceStartPending && !isLLMTurnPending
            && !isExternalDictationBusy && !state.isBusy
    }
    var canDeleteEntities: Bool {
        isReady && !meetingAudioImportInProgress && !isSessionActive
            && !isVoiceStartPending && !isLLMTurnPending
            && !isExternalDictationBusy && !state.isBusy
            && !llmConfigurationPending
    }
    var currentWorkspace: WorkspaceRecord? { workspaces.first { $0.id == currentWorkspaceID } }
    var currentTask: TaskRecord? { tasks.first { $0.id == currentTaskID } }
    var currentMeeting: MeetingRecord? { meetings.first { $0.id == currentMeetingID } }

    var composerSuggestions: [ComposerSuggestion] {
        let trimmed = composerDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("/"), !trimmed.dropFirst().contains(where: { $0.isWhitespace }) {
            let query = String(trimmed.dropFirst()).lowercased()
            return skills
                .filter { item in
                    item.enabled && (
                        query.isEmpty
                            || item.command.dropFirst().lowercased().hasPrefix(query)
                            || item.title.lowercased().contains(query)
                    )
                }
                .sorted { $0.command.localizedCaseInsensitiveCompare($1.command) == .orderedAscending }
                .prefix(6)
                .map { item in
                    ComposerSuggestion(
                        id: "skill:\(item.id)",
                        kind: .skill,
                        title: item.command,
                        subtitle: item.title,
                        insertion: item.command
                    )
                }
        }

        guard let at = composerDraft.lastIndex(of: "@") else { return [] }
        let queryText = String(composerDraft[composerDraft.index(after: at)...])
        guard !queryText.contains(where: { $0.isWhitespace || $0 == "[" || $0 == "]" || $0 == "\"" }) else {
            return []
        }
        let query = queryText.lowercased()
        return sources
            .filter { source in
                query.isEmpty
                    || source.title.lowercased().contains(query)
                    || source.kind.lowercased().contains(query)
            }
            .sorted { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }
            .prefix(6)
            .map { source in
                let safeTitle = source.title.replacingOccurrences(of: "]", with: ")")
                return ComposerSuggestion(
                    id: "source:\(source.id)",
                    kind: .source,
                    title: source.title,
                    subtitle: source.kind == "meeting" ? "Встреча" : "Источник",
                    insertion: "@[\(safeTitle)]"
                )
            }
    }

    func moveComposerSuggestion(_ delta: Int) {
        let count = composerSuggestions.count
        guard count > 0 else { composerSuggestionIndex = 0; return }
        composerSuggestionIndex = (composerSuggestionIndex + delta + count) % count
    }

    @discardableResult
    func applySelectedComposerSuggestion() -> Bool {
        let suggestions = composerSuggestions
        guard !suggestions.isEmpty else { return false }
        let suggestion = suggestions[min(max(composerSuggestionIndex, 0), suggestions.count - 1)]
        applyComposerSuggestion(suggestion)
        return true
    }

    func applyComposerSuggestion(_ suggestion: ComposerSuggestion) {
        switch suggestion.kind {
        case .skill:
            composerDraft = suggestion.insertion + " "
        case .source:
            guard let at = composerDraft.lastIndex(of: "@") else { return }
            composerDraft.replaceSubrange(at..<composerDraft.endIndex, with: suggestion.insertion + " ")
        }
        composerSuggestionIndex = 0
    }

    func insertSkillCommand(_ skill: EntityRecord) {
        let command = skill.command.isEmpty ? skill.subtitle : skill.command
        guard command.hasPrefix("/") else {
            errorMessage = "У скилла «\(skill.title)» не настроена slash-команда"
            return
        }
        composerDraft = command + " "
        composerSuggestionIndex = 0
        statusText = "Скилл «\(skill.title)» добавлен в запрос"
    }

    func runSkillCommand(_ skill: EntityRecord) {
        let command = skill.command.isEmpty ? skill.subtitle : skill.command
        guard command.hasPrefix("/") else {
            errorMessage = "У скилла «\(skill.title)» не настроена slash-команда"
            return
        }
        guard canSendText else {
            errorMessage = "Дождитесь завершения текущей операции перед применением скилла"
            return
        }
        let request = composerDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let text = request.isEmpty ? command : "\(command) \(request)"
        composerDraft = ""
        composerSuggestionIndex = 0
        submit(text: text, speak: false)
    }

    func isQuickActionCompleted(_ action: QuickActionRecord) -> Bool {
        completedQuickActionKeys.contains(quickActionKey(action))
    }

    func launch() {
        guard process == nil else { return }
        guard let resourcePath = Bundle.main.resourcePath else {
            fail("В приложении не найдены локальные ресурсы")
            return
        }
        let runtimePath = resourcePath + "/runtime"
        let pythonPath = runtimePath + "/python/bin/python3.12"
        guard FileManager.default.isExecutableFile(atPath: pythonPath) else {
            fail("Не найден Python-бэкенд: \(pythonPath)")
            return
        }
        let processEnvironment = ProcessInfo.processInfo.environment
        let supportRoot: URL
        if let override = processEnvironment["RND_WORKBENCH_SUPPORT_DIR"],
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            supportRoot = URL(fileURLWithPath: override, isDirectory: true)
        } else {
            supportRoot = FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            )[0].appendingPathComponent("LocalVoiceAssistant", isDirectory: true)
        }
        do {
            try FileManager.default.createDirectory(
                at: supportRoot,
                withIntermediateDirectories: true
            )
        } catch {
            fail("Не удалось создать локальное хранилище: \(error.localizedDescription)")
            return
        }
        state = .loading
        statusText = "Загружаю локальные модели…"
        errorMessage = nil

        let task = Process()
        let stdout = Pipe()
        let stderr = Pipe()
        let stdin = Pipe()
        task.executableURL = URL(fileURLWithPath: pythonPath)
        task.arguments = [
            "-m", "voice_assistant.ui_backend",
            "--config", resourcePath + "/config.toml",
            "--data", supportRoot.appendingPathComponent("assistant.sqlite3").path
        ]
        var environment = processEnvironment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["HF_HUB_OFFLINE"] = "1"
        environment["PYTHONHOME"] = runtimePath + "/python"
        environment["PYTHONPATH"] = [
            runtimePath + "/site-packages",
            runtimePath + "/src"
        ].joined(separator: ":")
        environment["RND_WORKBENCH_JAVA_CORE_JAVA"] = runtimePath
            + "/java-core/runtime/bin/java"
        environment["RND_WORKBENCH_JAVA_CORE_LIB_DIR"] = runtimePath
            + "/java-core/lib"
        // Public external models remain separately gated by classification and
        // explicit selection. Corporate and local routes do not need this flag.
        environment["RND_WORKBENCH_JAVA_CORE_EXTERNAL_MODELS_ENABLED"] = "1"
        environment["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        task.environment = environment
        task.standardOutput = stdout
        task.standardError = stderr
        task.standardInput = stdin

        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            self?.ioQueue.async { [weak self] in self?.consumeOutput(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.stderrTail = String((self.stderrTail + text).suffix(3_000))
            }
        }
        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.process = nil
                self.isReady = false
                self.isSessionActive = false
                self.isVoiceStartPending = false
                self.isLLMTurnPending = false
                self.externalDictationStartPending = false
                self.externalDictationActive = false
                self.externalDictationTranscribing = false
                self.externalDictationTarget = nil
                self.externalDictationFocusChanged = false
                self.meetingAudioImportInProgress = false
                self.meetingAudioImportStage = nil
                self.pendingCredentialDeletionEndpoint = nil
                if finished.terminationStatus != 0 {
                    let detail = self.stderrTail.split(separator: "\n").suffix(3).joined(separator: " ")
                    self.fail(detail.isEmpty ? "Локальный движок завершился с ошибкой" : detail)
                }
            }
        }
        do {
            try task.run()
            process = task
            inputPipe = stdin
        } catch {
            fail("Не удалось запустить локальный движок: \(error.localizedDescription)")
        }
    }

    func toggleSession() {
        if isSessionActive || isVoiceStartPending { stopVoiceSession() }
        else {
            guard canToggleVoiceSession, ensureLLMRouteAvailable() else { return }
            isVoiceStartPending = true
            statusText = "Запускаю голосовой режим…"
            send(["command": "start"])
        }
    }

    func stopVoiceSession() {
        guard isSessionActive || isVoiceStartPending else { return }
        statusText = "Останавливаю голосовой режим…"
        send(["command": "stop"])
    }

    func presentCompact() {
        AssistantWindowBridge.captureFullFrame()
        presentationMode = .compact
        AssistantWindowBridge.reveal()
    }

    func presentFull() {
        presentationMode = .full
        AssistantWindowBridge.reveal()
    }

    func hideAssistantWindow() {
        stopVoiceSession()
        AssistantWindowBridge.hide()
    }

    func refreshGlobalPushToTalkPermissions() {
        globalPushToTalkMonitor?.refresh()
    }

    func requestGlobalPushToTalkPermissions() {
        globalPushToTalkMonitor?.refresh(requestPermission: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.globalPushToTalkMonitor?.refresh()
        }
    }

    func startExternalDictation() {
        refreshGlobalPushToTalkPermissions()
        guard globalPushToTalkReady else {
            externalDictationStatus = globalPushToTalkStatusLabel
            return
        }
        guard !externalDictationStartPending, !externalDictationActive,
              !externalDictationTranscribing else { return }
        guard isReady, !meetingAudioImportInProgress,
              !isSessionActive, !isVoiceStartPending,
              !isLLMTurnPending, !state.isBusy,
              !llmConfigurationPending else {
            externalDictationStatus = "Диктовка недоступна, пока помощник занят"
            return
        }
        guard let target = systemTextInserter.captureFocusedElement() else {
            externalDictationStatus = "Поместите курсор в текстовое поле и повторите"
            statusText = "Глобальная диктовка: нет активного поля"
            return
        }
        externalDictationTarget = target
        externalDictationFocusChanged = false
        externalDictationTranscribing = false
        if systemTextInserter.isSensitiveField(target) {
            externalDictationTarget = nil
            externalDictationStatus = "Диктовка в парольных и защищённых полях отключена"
            statusText = "Защищённое поле: диктовка не запущена"
            return
        }
        externalDictationStartPending = true
        externalDictationStatus = "Говорите — отпустите правую ⌥ для вставки"
        statusText = "Глобальная диктовка: слушаю…"
        send(["command": "dictation_start", "destination": "system"])
    }

    func stopExternalDictation() {
        guard externalDictationStartPending || externalDictationActive else { return }
        if let target = externalDictationTarget {
            guard let focusedNow = systemTextInserter.captureFocusedElement() else {
                externalDictationFocusChanged = true
                externalDictationStatus = "Фокус потерян — распознаю текст и сохраню его в черновик"
                statusText = "Глобальная диктовка: распознаю…"
                send(["command": "dictation_stop", "destination": "system"])
                return
            }
            externalDictationFocusChanged = !CFEqual(focusedNow, target)
        }
        externalDictationStatus = externalDictationFocusChanged
            ? "Фокус изменился — распознаю текст и сохраню его в черновик"
            : "Распознаю и вставляю текст…"
        statusText = "Глобальная диктовка: распознаю…"
        send(["command": "dictation_stop", "destination": "system"])
    }

    private func configureGlobalPushToTalk() {
        let monitor = GlobalPushToTalkMonitor()
        monitor.onPress = { [weak self] in self?.startExternalDictation() }
        monitor.onRelease = { [weak self] in self?.stopExternalDictation() }
        monitor.onStatusChange = { [weak self] accessibility, inputMonitoring, eventPosting, running in
            guard let self else { return }
            self.accessibilityPermissionGranted = accessibility
            self.inputMonitoringPermissionGranted = inputMonitoring
            self.eventPostingPermissionGranted = eventPosting
            self.pushToTalkMonitorRunning = running
        }
        globalPushToTalkMonitor = monitor
        monitor.refresh()
    }

    func submit(text: String, speak: Bool = true) {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, isReady, !meetingAudioImportInProgress,
              !isSessionActive, !isVoiceStartPending,
              !isLLMTurnPending, !state.isBusy,
              !llmConfigurationPending, ensureLLMRouteAvailable() else { return }
        isLLMTurnPending = true
        var payload: [String: Any] = ["command": "text", "text": clean, "speak": speak]
        if !pendingAttachments.isEmpty {
            payload["attachments"] = pendingAttachments.map { attachment in
                var item: [String: Any] = ["path": attachment.path]
                if let kind = attachment.kind { item["kind"] = kind }
                return item
            }
            submittedAttachmentIDs = Set(pendingAttachments.map(\.id))
            statusText = "Добавляю вложения и запускаю задачу…"
        }
        send(payload)
    }

    func retrySpeech() {
        guard canRetrySpeech else { return }
        statusText = "Повторяю озвучивание…"
        send(["command": "retry_speech"])
    }

    func dismissSpeechError() {
        speechErrorMessage = nil
        speechErrorRetryable = true
        speechErrorTaskID = nil
    }

    func newTask() { send(["command": "new_task", "title": "Новая задача"]) }
    func selectTask(_ id: String) { send(["command": "select_task", "id": id]) }
    func selectMeeting(_ id: String) { send(["command": "select_meeting", "id": id, "meeting_id": id]) }
    func reanalyzeMeeting(_ id: String) { send(["command": "reanalyze_meeting", "id": id, "meeting_id": id]) }
    func compareMeetings(_ id: String, with otherID: String) {
        send(["command": "compare_meetings", "id": id, "meeting_id": id, "other_id": otherID, "other_meeting_id": otherID])
    }
    func setMeetingItemStatus(_ id: String, status: String) {
        send(["command": "meeting_item_status", "id": id, "item_id": id, "status": status])
    }
    func prepareBriefing(_ id: String) { send(["command": "prepare_briefing", "id": id, "meeting_id": id]) }
    func syncExpressMeetings() {
        guard expressConnectorConfigured, !expressSyncInProgress,
              isReady, !meetingAudioImportInProgress, !isSessionActive else { return }
        expressSyncInProgress = true
        meetingAudioImportStage = "Получаю новые встречи eXpress…"
        send(["command": "sync_express_meetings"])
    }
    func explainAttention() { send(["command": "explain_attention"]) }
    func selectWorkspace(_ id: String) { send(["command": "select_workspace", "id": id]) }
    func createWorkspace(name: String, description: String) { send(["command": "create_workspace", "name": name, "description": description]) }
    func updateWorkspace(name: String, description: String) {
        send(["command": "update_workspace", "id": currentWorkspaceID, "name": name, "description": description])
    }
    func archiveCurrentWorkspace() {
        guard currentWorkspaceID != "personal" else { return }
        send(["command": "update_workspace", "id": currentWorkspaceID, "status": "archived"])
    }
    func clearConversation() { send(["command": "clear"]) }
    func updateTaskPlan(_ taskID: String, plan: [String]) {
        send(["command": "update_task_plan", "id": taskID, "plan": plan])
    }
    func deleteTask(_ id: String) {
        guard canDeleteEntities else {
            errorMessage = "Дождитесь завершения текущей операции перед удалением задачи"
            return
        }
        statusText = "Удаляю задачу…"
        send(["command": "delete_task", "task_id": id])
    }
    func deleteSource(_ id: String) {
        guard canDeleteEntities else {
            errorMessage = "Дождитесь завершения текущей операции перед удалением источника"
            return
        }
        statusText = "Удаляю источник…"
        send(["command": "delete_source", "source_id": id])
    }
    func search(_ query: String, globally: Bool) { send(["command": "search", "query": query, "global": globally]) }
    func saveMemory(title: String, content: String, kind: String) {
        send(["command": "save_memory", "title": title, "content": content, "kind": kind])
    }
    func updateMemory(id: String, title: String, content: String, kind: String) {
        send(["command": "update_memory", "id": id, "title": title, "content": content, "kind": kind])
    }
    func deleteMemory(_ id: String) { send(["command": "delete_memory", "id": id]) }
    func saveSkill(id: String? = nil, name: String, command: String, description: String, instruction: String) {
        var payload: [String: Any] = ["command": "save_skill", "name": name, "command_name": command, "description": description, "instruction": instruction, "scope": "personal"]
        if let id { payload["id"] = id }
        send(payload)
    }
    func createAutomation(name: String, prompt: String, schedule: String) {
        send(["command": "create_automation", "name": name, "prompt": prompt, "schedule": schedule])
    }
    func toggleAutomation(_ item: EntityRecord) { send(["command": "toggle_automation", "id": item.id, "enabled": !item.enabled]) }
    func updateAutomation(id: String, name: String, prompt: String, schedule: String) {
        send(["command": "update_automation", "id": id, "name": name, "prompt": prompt, "schedule": schedule])
    }
    func deleteAutomation(_ id: String) { send(["command": "delete_automation", "id": id]) }
    func resolveApproval(_ id: String, status: String) { send(["command": "resolve_approval", "id": id, "status": status]) }
    func updateApproval(_ id: String, payload: String) { send(["command": "update_approval", "id": id, "payload": payload]) }
    func markInboxRead(_ id: String) { send(["command": "inbox_status", "id": id, "status": "read"]) }
    func setSetting(_ key: String, _ value: String) { send(["command": "setting", "key": key, "value": value]) }

    func setClassification(entityType: String, id: String, value: String) {
        send([
            "command": "set_classification",
            "entity_type": entityType,
            "entity_id": id,
            "classification": value,
        ])
    }

    func configureExternalLLM(
        baseURL: String,
        model: String,
        apiKey: String,
        providerType: String,
        mode: String = "external",
        autoRemotePolicy: String = "eligible"
    ) {
        guard canChangeLLMConfiguration else {
            errorMessage = "Дождитесь завершения текущего ответа перед сменой модели"
            return
        }
        let targetMode = mode == "auto" ? "auto" : "external"
        let targetAutoRemotePolicy = autoRemotePolicy == "eligible" ? "eligible" : "local_only"
        let cleanBaseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
        let suppliedKey = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let autoStaysLocal = targetMode == "auto" && targetAutoRemotePolicy == "local_only"

        if autoStaysLocal && cleanBaseURL.isEmpty && cleanModel.isEmpty {
            didHydrateExternalLLM = true
            pendingCredentialDeletionEndpoint = nil
            pendingLLMMode = "auto"
            llmConfigurationPending = true
            llmConfigurationError = nil
            externalLLMReady = false
            statusText = "Включаю безопасный режим Авто…"
            send([
                "command": "configure_llm",
                "mode": "auto",
                "base_url": "",
                "model": "",
                "api_key": "",
                "provider_type": providerType,
                "auto_remote_policy": targetAutoRemotePolicy,
            ])
            return
        }
        if let validationError = ExternalLLMEndpoint.validationError(cleanBaseURL) {
            showLLMConfigurationError(validationError)
            return
        }
        guard !cleanModel.isEmpty else {
            showLLMConfigurationError("Укажите идентификатор модели у провайдера")
            return
        }
        guard let canonicalEndpoint = ExternalLLMEndpoint.canonicalized(cleanBaseURL) else {
            showLLMConfigurationError("Не удалось безопасно нормализовать адрес провайдера")
            return
        }

        do {
            if !suppliedKey.isEmpty {
                try ExternalLLMKeychain.save(suppliedKey, canonicalEndpoint: canonicalEndpoint)
            }
            let storedKey = suppliedKey.isEmpty
                ? (try ExternalLLMKeychain.read(canonicalEndpoint: canonicalEndpoint) ?? "")
                : suppliedKey
            hasExternalLLMAPIKey = !storedKey.isEmpty
            if storedKey.isEmpty && !ExternalLLMEndpoint.isLoopback(canonicalEndpoint) {
                showLLMConfigurationError(
                    "Для этого внешнего адреса нужен собственный ключ API. Ключи других провайдеров не используются."
                )
                externalLLMReady = false
                return
            }
            didHydrateExternalLLM = true
            pendingCredentialDeletionEndpoint = nil
            pendingLLMMode = targetMode
            llmConfigurationPending = true
            llmConfigurationError = nil
            externalLLMReady = false
            statusText = targetMode == "auto"
                ? "Настраиваю безопасную маршрутизацию…"
                : "Настраиваю удалённую модель…"
            send([
                "command": "configure_llm",
                "mode": targetMode,
                "base_url": canonicalEndpoint,
                "model": cleanModel,
                "api_key": storedKey,
                "provider_type": providerType,
                "auto_remote_policy": targetAutoRemotePolicy,
            ])
        } catch {
            showLLMConfigurationError("Не удалось обратиться к Связке ключей: \(error.localizedDescription)")
        }
    }

    func useLocalLLM() {
        guard canChangeLLMConfiguration else {
            errorMessage = "Дождитесь завершения текущего ответа перед сменой модели"
            return
        }
        pendingCredentialDeletionEndpoint = nil
        beginLocalLLMSwitch()
    }

    private func beginLocalLLMSwitch() {
        didHydrateExternalLLM = true
        pendingLLMMode = "local"
        llmConfigurationPending = true
        llmConfigurationError = nil
        statusText = "Переключаю на локальную MLX…"
        send([
            "command": "configure_llm",
            "mode": "local",
            "base_url": "",
            "model": "",
            "api_key": "",
        ])
    }

    func hasStoredExternalLLMKey(baseURL: String) -> Bool {
        guard let canonicalEndpoint = ExternalLLMEndpoint.canonicalized(baseURL) else { return false }
        return (try? ExternalLLMKeychain.read(canonicalEndpoint: canonicalEndpoint))?.isEmpty == false
    }

    func deleteExternalLLMAPIKey(baseURL: String) {
        guard canChangeLLMConfiguration else {
            errorMessage = "Дождитесь завершения текущего ответа перед удалением ключа API"
            return
        }
        guard let canonicalEndpoint = ExternalLLMEndpoint.canonicalized(baseURL) else {
            showLLMConfigurationError("Сначала укажите корректный адрес провайдера")
            return
        }
        do {
            let activeEndpoint = ExternalLLMEndpoint.canonicalized(externalLLMBaseURL)
            let deletesActiveCredential = (isExternalLLMActive || isAutoLLMActive)
                && activeEndpoint == canonicalEndpoint
            if deletesActiveCredential {
                pendingCredentialDeletionEndpoint = canonicalEndpoint
                beginLocalLLMSwitch()
            } else {
                try ExternalLLMKeychain.delete(canonicalEndpoint: canonicalEndpoint)
                statusText = "Ключ API для выбранного адреса удалён"
                llmConfigurationError = nil
            }
        } catch {
            showLLMConfigurationError("Не удалось удалить ключ API: \(error.localizedDescription)")
        }
    }

    func chooseFile(kind: String? = nil, taskID: String? = nil) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = ["txt", "md", "markdown", "csv", "tsv", "json", "log", "xml", "docx", "pdf"].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK, let path = panel.url?.path else { return }
        var payload: [String: Any] = ["command": "import_file", "path": path, "workspace_id": currentWorkspaceID]
        if let kind { payload["kind"] = kind }
        if let taskID { payload["task_id"] = taskID }
        send(payload)
        statusText = "Импортирую \(URL(fileURLWithPath: path).lastPathComponent)…"
    }

    func chooseComposerAttachments() {
        let panel = NSOpenPanel()
        panel.title = "Добавить файлы к запросу"
        panel.prompt = "Добавить"
        panel.message = "Файлы будут добавлены только в контекст задачи при отправке запроса."
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [
            "txt", "md", "markdown", "csv", "tsv", "json", "log", "xml", "docx", "pdf",
        ].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK else { return }
        let knownPaths = Set(pendingAttachments.map(\.path))
        let additions = panel.urls.compactMap { url -> PendingAttachmentRecord? in
            guard !knownPaths.contains(url.path) else { return nil }
            return PendingAttachmentRecord(
                id: UUID().uuidString,
                path: url.path,
                name: url.lastPathComponent,
                kind: nil
            )
        }
        pendingAttachments.append(contentsOf: additions)
        if !additions.isEmpty {
            statusText = additions.count == 1
                ? "Файл подготовлен к отправке"
                : "Файлы подготовлены к отправке: \(additions.count)"
        }
    }

    func removePendingAttachment(_ id: String) {
        guard !isLLMTurnPending else { return }
        pendingAttachments.removeAll { $0.id == id }
        submittedAttachmentIDs.remove(id)
    }

    func chooseMeetingAudio() {
        guard canImportMeetingAudio else {
            errorMessage = meetingAudioImportInProgress
                ? "Дождитесь завершения обработки текущей аудиозаписи"
                : "Дождитесь завершения текущей операции перед добавлением аудио"
            return
        }
        let panel = NSOpenPanel()
        panel.title = "Добавить аудио встречи"
        panel.prompt = "Добавить"
        panel.message = "Аудиозапись будет локально распознана и преобразована в карточку встречи."
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.audio]
        guard panel.runModal() == .OK, let path = panel.url?.path else { return }

        meetingIDsBeforeAudioImport = Set(meetings.map(\.id))
        pendingImportedMeetingID = nil
        pendingImportedMeetingSourceID = nil
        meetingImportKind = "audio"
        meetingAudioImportInProgress = true
        meetingAudioImportStage = "Подготавливаю аудиозапись…"
        statusText = "Подготавливаю \(URL(fileURLWithPath: path).lastPathComponent)…"
        send([
            "command": "import_meeting_audio",
            "path": path,
            "workspace_id": currentWorkspaceID,
        ])
    }

    func chooseSynapseMeetingPackage() {
        guard canImportMeetingAudio else {
            errorMessage = meetingAudioImportInProgress
                ? "Дождитесь завершения текущего импорта встречи"
                : "Дождитесь завершения текущей операции перед импортом пакета"
            return
        }
        let panel = NSOpenPanel()
        panel.title = "Импортировать пакет встречи из eXpress (Синапс)"
        panel.prompt = "Импортировать"
        panel.message = "Выберите папку или ZIP. Поддерживается manifest.json либо быстрый набор: один transcript/расшифровка, один description/описание и остальные вложения. Импорт выполняется локально."
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.zip]
        guard panel.runModal() == .OK, let path = panel.url?.path else { return }

        meetingIDsBeforeAudioImport = Set(meetings.map(\.id))
        pendingImportedMeetingID = nil
        pendingImportedMeetingSourceID = nil
        meetingImportKind = "synapse"
        meetingAudioImportInProgress = true
        meetingAudioImportStage = "Проверяю пакет eXpress (Синапс) и происхождение данных…"
        statusText = "Импортирую пакет встречи локально…"
        send([
            "command": "import_synapse_package",
            "path": path,
            "workspace_id": currentWorkspaceID,
        ])
    }

    func openSource(_ item: EntityRecord) {
        if item.hasExactExcerpt {
            sourcePreview = item
            return
        }
        openSourceFile(item)
    }

    func openSourceFile(_ item: EntityRecord) {
        guard let path = item.path, !path.isEmpty else {
            errorMessage = "Для источника «\(item.title)» не найден локальный файл"
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func openInboxItem(_ item: EntityRecord) {
        if item.status == "new" { markInboxRead(item.id) }
        guard let rawReference = item.sourceRef?.trimmingCharacters(in: .whitespacesAndNewlines),
              !rawReference.isEmpty else { return }

        if let task = tasks.first(where: { $0.id == rawReference }) {
            selectTask(task.id)
            navigationRequest = AppNavigationRequest(section: .tasks)
            return
        }
        if let meeting = meetings.first(where: {
            $0.id == rawReference || $0.sourceID == rawReference
        }) {
            selectMeeting(meeting.id)
            navigationRequest = AppNavigationRequest(section: .meetings)
            return
        }
        if let artifact = artifacts.first(where: { $0.id == rawReference }) {
            navigationRequest = AppNavigationRequest(section: .artifacts)
            openArtifact(artifact)
            return
        }
        if let source = (sources + activeSources).first(where: { $0.id == rawReference }) {
            openSource(source)
            return
        }
        if item.kind.hasPrefix("meeting") {
            navigationRequest = AppNavigationRequest(section: .meetings)
            return
        }
        errorMessage = "Связанный объект уведомления больше недоступен"
    }

    func openWorkspaceTimelineItem(_ item: WorkspaceTimelineRecord) {
        switch item.targetSection {
        case AppSection.tasks.rawValue:
            selectTask(item.targetID)
            navigationRequest = AppNavigationRequest(section: .tasks)
        case AppSection.meetings.rawValue:
            selectMeeting(item.targetID)
            navigationRequest = AppNavigationRequest(section: .meetings)
        case AppSection.artifacts.rawValue:
            navigationRequest = AppNavigationRequest(section: .artifacts)
            let artifact = artifacts.first { $0.id == item.targetID }
                ?? EntityRecord(
                    id: item.targetID,
                    title: item.title.replacingOccurrences(of: "Материал: ", with: ""),
                    subtitle: "markdown",
                    kind: "markdown"
                )
            loadArtifactHistory(artifact)
        case AppSection.approvals.rawValue:
            navigationRequest = AppNavigationRequest(section: .approvals)
        case AppSection.workspaces.rawValue:
            if let source = item.sourceRecord { openSource(source) }
        default:
            if let section = AppSection(rawValue: item.targetSection) {
                navigationRequest = AppNavigationRequest(section: section)
            }
        }
    }

    func openWorkspaceTimelineSource(_ item: WorkspaceTimelineRecord) {
        guard let source = item.sourceRecord else {
            errorMessage = "Для события хронологии не найден связанный источник"
            return
        }
        openSource(source)
    }

    func openArtifact(_ item: EntityRecord) {
        guard let path = item.path else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func deleteArtifact(_ id: String) {
        guard canDeleteEntities else {
            errorMessage = "Дождитесь завершения текущей операции перед удалением материала"
            return
        }
        statusText = "Удаляю материал…"
        send(["command": "delete_artifact", "artifact_id": id])
    }

    func updateArtifact(_ id: String, content: String) {
        send(["command": "update_artifact", "id": id, "content": content])
    }

    func loadArtifactHistory(_ artifact: EntityRecord) {
        artifactHistoryArtifact = artifact
        artifactVersions = []
        artifactRelations = []
        artifactHistoryError = nil
        artifactHistoryLoading = true
        artifactRelationsLoading = true
        artifactRestorePendingVersion = nil
        send(["command": "artifact_versions", "artifact_id": artifact.id])
        send(["command": "artifact_relations", "artifact_id": artifact.id])
    }

    func refreshArtifactHistory() {
        guard let artifactID = artifactHistoryArtifact?.id else { return }
        artifactHistoryError = nil
        artifactHistoryLoading = true
        artifactRelationsLoading = true
        send(["command": "artifact_versions", "artifact_id": artifactID])
        send(["command": "artifact_relations", "artifact_id": artifactID])
    }

    func closeArtifactHistory() {
        artifactHistoryArtifact = nil
        artifactVersions = []
        artifactRelations = []
        artifactHistoryError = nil
        artifactHistoryLoading = false
        artifactRelationsLoading = false
        artifactRestorePendingVersion = nil
    }

    func restoreArtifactVersion(_ version: ArtifactVersionRecord) {
        guard let artifact = artifactHistoryArtifact,
              artifact.id == version.artifactID,
              artifactRestorePendingVersion == nil else { return }
        guard artifact.kind == "markdown" || artifact.subtitle == "markdown" else {
            artifactHistoryError = "Восстановление версий доступно только для Markdown-материалов"
            return
        }
        artifactHistoryError = nil
        artifactRestorePendingVersion = version.version
        statusText = "Восстанавливаю версию \(version.version)…"
        send([
            "command": "restore_artifact",
            "artifact_id": artifact.id,
            "version": version.version,
        ])
    }

    func openArtifactVersion(_ version: ArtifactVersionRecord) {
        guard let path = version.path, !path.isEmpty else {
            artifactHistoryError = "Файл версии \(version.version) не найден"
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func performQuickAction(_ action: QuickActionRecord) {
        guard quickActionPendingID == nil, !isQuickActionCompleted(action) else { return }
        if action.command == "artifact_versions", let artifactID = action.artifactID {
            let artifact = artifacts.first { $0.id == artifactID }
                ?? EntityRecord(
                    id: artifactID,
                    title: "Материал",
                    subtitle: "markdown",
                    kind: "markdown"
                )
            loadArtifactHistory(artifact)
            return
        }
        guard action.command == "quick_action" else {
            errorMessage = "Неподдерживаемое быстрое действие: \(action.title)"
            return
        }
        guard let taskID = action.taskID ?? currentTaskID else {
            errorMessage = "Для быстрого действия не найдена текущая задача"
            return
        }
        pendingQuickAction = action
        quickActionPendingID = action.id
        statusText = "Выполняю: \(action.title.lowercased())…"
        send([
            "command": "quick_action",
            "id": action.id,
            "action": action.id,
            "task_id": taskID,
        ])
    }

    private func quickActionKey(_ action: QuickActionRecord) -> String {
        "\(action.taskID ?? currentTaskID ?? "none"):\(action.id)"
    }

    func exportPilotMetrics() {
        let panel = NSSavePanel()
        panel.title = "Экспортировать обезличенный отчёт пилота"
        panel.prompt = "Сохранить"
        panel.nameFieldStringValue = "RnD-Workbench-pilot-report.json"
        panel.allowedContentTypes = [.json]
        panel.canCreateDirectories = true
        panel.begin { [weak self] result in
            guard result == .OK, let url = panel.url else { return }
            Task { @MainActor [weak self] in
                self?.send([
                    "command": "export_pilot_metrics",
                    "path": url.path,
                ])
            }
        }
    }

    func runPilotPreflight() {
        send(["command": "pilot_preflight"])
    }

    func performPilotOnboardingAction() {
        switch pilotOnboardingActionID {
        case "review_preflight":
            runPilotPreflight()
        case "start_voice":
            UserDefaults.standard.set(CompactMode.voice.rawValue, forKey: "rnd.compact.mode")
            presentCompact()
        case "open_chat":
            UserDefaults.standard.set(CompactMode.chat.rawValue, forKey: "rnd.compact.mode")
            presentCompact()
        case "show_meeting_import":
            navigationRequest = AppNavigationRequest(section: .meetings)
            presentFull()
        case "prepare_briefing":
            composerDraft = "/briefing "
            UserDefaults.standard.set(CompactMode.chat.rawValue, forKey: "rnd.compact.mode")
            presentCompact()
        default:
            break
        }
    }

    func setPilotUsefulnessRating(_ rating: Int) {
        guard (1...5).contains(rating) else { return }
        send([
            "command": "set_pilot_feedback",
            "usefulness_rating": rating,
        ])
    }

    private func applyPilotPreflight(_ report: [String: Any]) {
        let overall = string(report, "overall")
        pilotPreflightOverall = overall.isEmpty ? "limited" : overall
        pilotPreflightChecks = rows(report, "checks").map { check in
            PilotPreflightCheckRecord(
                id: string(check, "id"),
                title: string(check, "title"),
                status: string(check, "status"),
                detail: string(check, "detail"),
                action: string(check, "action")
            )
        }
    }

    private func applyPilotOnboarding(_ onboarding: [String: Any]) {
        pilotOnboardingStatus = string(onboarding, "status")
        pilotOnboardingTitle = string(onboarding, "title")
        pilotOnboardingDetail = string(onboarding, "detail")
        pilotOnboardingActionID = string(onboarding, "action_id")
        pilotOnboardingActionLabel = string(onboarding, "action_label")
        if let progress = onboarding["progress"] as? [String: Any] {
            pilotOnboardingCompleted = max(0, int(progress["completed"]))
            pilotOnboardingTotal = max(1, int(progress["total"]))
        }
    }

    func shutdown() {
        globalPushToTalkMonitor?.stop()
        globalPushToTalkMonitor = nil
        let runningProcess = process
        send(["command": "quit"])
        inputPipe?.fileHandleForWriting.closeFile()
        let deadline = Date().addingTimeInterval(3)
        while runningProcess?.isRunning == true && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if runningProcess?.isRunning == true { runningProcess?.terminate() }
        process = nil
    }

    private func send(_ payload: [String: Any]) {
        guard let handle = inputPipe?.fileHandleForWriting,
              var data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        data.append(0x0A)
        try? handle.write(contentsOf: data)
    }

    private nonisolated func consumeOutput(_ data: Data) {
        let chunk = String(decoding: data, as: UTF8.self)
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.outputBuffer += chunk
            while let newline = self.outputBuffer.firstIndex(of: "\n") {
                let line = String(self.outputBuffer[..<newline])
                self.outputBuffer.removeSubrange(...newline)
                self.handleLine(line)
            }
        }
    }

    private func handleLine(_ line: String) {
        guard let data = line.data(using: .utf8),
              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = event["type"] as? String else { return }
        switch type {
        case "state":
            if let raw = event["state"] as? String, let newState = AssistantState(rawValue: raw) { state = newState }
            if let detail = event["detail"] as? String { statusText = detail }
        case "ready":
            isReady = true
            state = .ready
            if !llmConfigurationPending { statusText = "Готов к работе" }
        case "model_loaded": if let model = event["model"] as? String { loadedModels.insert(model) }
        case "snapshot": if let snapshot = event["data"] as? [String: Any] { applySnapshot(snapshot) }
        case "llm_configured":
            let runtime = event["runtime"] as? [String: Any] ?? [:]
            let configuredMode = (event["mode"] as? String) ?? llmMode
            let runtimeProviderType = string(runtime, "configured_provider_type")
            let configuredProviderType = runtimeProviderType.isEmpty
                ? ((event["provider_type"] as? String) ?? externalProviderType)
                : runtimeProviderType
            let configuredBaseURL = (event["base_url"] as? String) ?? externalLLMBaseURL
            let remoteModel = string(runtime, "remote_model")
            let configuredModel = configuredMode == "auto" && !remoteModel.isEmpty
                ? remoteModel
                : ((event["model"] as? String) ?? externalLLMModel)
            let configuredReady = event["ready"] as? Bool ?? true
            let remoteReady = runtime["remote_ready"] as? Bool ?? configuredReady
            let runtimeAutoPolicy = string(runtime, "auto_remote_policy")
            let configuredAutoPolicy = runtimeAutoPolicy.isEmpty ? autoRemotePolicy : runtimeAutoPolicy
            var credentialDeletionError: String?
            if configuredMode == "local", configuredReady,
               let endpoint = pendingCredentialDeletionEndpoint {
                do {
                    try ExternalLLMKeychain.delete(canonicalEndpoint: endpoint)
                    hasExternalLLMAPIKey = false
                } catch {
                    credentialDeletionError = "Локальная модель включена, но ключ API не удалось удалить: \(error.localizedDescription)"
                }
                pendingCredentialDeletionEndpoint = nil
            } else if pendingCredentialDeletionEndpoint != nil {
                pendingCredentialDeletionEndpoint = nil
            }
            settings["llm_mode"] = configuredMode
            if configuredMode == "external" || configuredMode == "auto" {
                settings["external_provider_type"] = configuredProviderType
            }
            if configuredMode == "auto" { settings["auto_remote_policy"] = configuredAutoPolicy }
            if !configuredBaseURL.isEmpty { settings["external_llm_base_url"] = configuredBaseURL }
            if !configuredModel.isEmpty { settings["external_llm_model"] = configuredModel }
            if configuredMode == "external" {
                externalLLMReady = configuredReady
                let activeModel = (event["active_model"] as? String) ?? configuredModel
                if !activeModel.isEmpty { modelName = activeModel }
                refreshExternalLLMKeyStatus(for: configuredBaseURL)
                statusText = configuredReady
                    ? (configuredProviderType == "corporate"
                        ? "Корпоративная модель настроена"
                        : "Внешняя модель настроена")
                    : "Удалённая модель требует настройки"
                actualLLMRoute = configuredProviderType == "corporate"
                    ? "corporate_api" : "external_api"
            } else if configuredMode == "auto" {
                externalLLMReady = remoteReady
                let activeModel = (event["active_model"] as? String) ?? "Auto · Локальная MLX"
                if !activeModel.isEmpty { modelName = activeModel }
                refreshExternalLLMKeyStatus(for: configuredBaseURL)
                actualLLMRoute = "local_mlx"
                statusText = configuredAutoPolicy == "eligible"
                    ? (remoteReady
                        ? "Авто настроен · удалённый маршрут доступен по политике данных"
                        : "Авто настроен · пока используется локальная модель")
                    : "Авто настроен · только локальная модель"
            } else {
                externalLLMReady = true
                let activeModel = (event["active_model"] as? String) ?? configuredModel
                modelName = activeModel.isEmpty ? "Локальная MLX" : activeModel
                statusText = "Используется локальная MLX"
                actualLLMRoute = "local_mlx"
            }
            llmConfigurationPending = false
            pendingLLMMode = nil
            llmConfigurationError = credentialDeletionError
                ?? (configuredMode == "external" && !configuredReady
                    ? "Настройки сохранены, но модель пока не готова" : nil)
            if let credentialDeletionError { errorMessage = credentialDeletionError }
        case "llm_configuration_error":
            let message = event["message"] as? String ?? "Не удалось применить конфигурацию модели"
            if pendingLLMMode == "external" || pendingLLMMode == "auto" { externalLLMReady = false }
            llmConfigurationPending = false
            pendingLLMMode = nil
            pendingCredentialDeletionEndpoint = nil
            llmConfigurationError = message
            statusText = "Не удалось настроить модель"
            errorMessage = message
        case "calibrated":
            isVoiceStartPending = false
            isSessionActive = true
        case "session_stopped":
            isVoiceStartPending = false
            isSessionActive = false
        case "dictation_started":
            if (event["destination"] as? String) == "system" {
                externalDictationStartPending = false
                externalDictationActive = true
                externalDictationTranscribing = false
                externalDictationStatus = "Говорите — отпустите правую ⌥ для вставки"
                statusText = "Глобальная диктовка: слушаю…"
            }
        case "dictation_stopped":
            if (event["destination"] as? String) == "system"
                || externalDictationStartPending || externalDictationActive {
                externalDictationStartPending = false
                externalDictationActive = false
                externalDictationTranscribing = true
                externalDictationStatus = externalDictationFocusChanged
                    ? "Фокус изменился — распознаю текст и сохраню его в черновик"
                    : "Распознаю и вставляю текст…"
                statusText = "Глобальная диктовка: распознаю…"
            }
        case "dictation_ready":
            let destination = event["destination"] as? String ?? "composer"
            let text = event["text"] as? String ?? ""
            sttSeconds = number(event["seconds"]) ?? sttSeconds
            state = .ready
            if destination == "system" {
                externalDictationStartPending = false
                externalDictationActive = false
                externalDictationTranscribing = false
                let result: SystemTextInsertionResult = externalDictationFocusChanged
                    ? .failed("Фокус изменился — текст не вставлен")
                    : systemTextInserter.insert(text, into: externalDictationTarget)
                externalDictationTarget = nil
                externalDictationFocusChanged = false
                if case .failed(let message) = result {
                    let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !cleanText.isEmpty {
                        composerDraft = cleanText
                        composerSuggestionIndex = 0
                        dictationReviewSequence += 1
                        externalDictationStatus = "\(message). Текст сохранён в черновик RnD Workbench."
                        statusText = "Текст диктовки сохранён в черновик"
                    } else {
                        externalDictationStatus = message
                        statusText = message
                    }
                } else if case .keyboardUnverified = result {
                    let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !cleanText.isEmpty {
                        composerDraft = cleanText
                        composerSuggestionIndex = 0
                        dictationReviewSequence += 1
                    }
                    externalDictationStatus = "\(result.statusText). Копия сохранена в черновик RnD Workbench."
                    statusText = "Проверьте вставку; текст сохранён в черновик"
                } else {
                    externalDictationStatus = result.statusText
                    statusText = result.statusText
                }
            } else {
                composerDraft = text
                composerSuggestionIndex = 0
                isVoiceStartPending = false
                isSessionActive = false
                dictationReviewSequence += 1
                statusText = "Проверьте распознанный текст и отправьте"
            }
        case "dictation_error":
            if (event["destination"] as? String) == "system" {
                externalDictationStartPending = false
                externalDictationActive = false
                externalDictationTranscribing = false
                externalDictationTarget = nil
                externalDictationFocusChanged = false
                let message = event["message"] as? String ?? "Не удалось распознать диктовку"
                externalDictationStatus = message
                statusText = "Глобальная диктовка: \(message)"
            }
        case "user": if let text = event["text"] as? String { messages.append(ChatMessage(role: .user, text: text)) }
        case "assistant_start":
            dismissSpeechError()
            lastAssistantSpoken = nil
            firstTokenSeconds = nil
            firstAudioSeconds = nil
            responseSeconds = nil
            routingFallbackMessage = nil
            applyLLMRoute(event["llm_route"])
            quickActions = []
            if !submittedAttachmentIDs.isEmpty {
                pendingAttachments.removeAll { submittedAttachmentIDs.contains($0.id) }
                submittedAttachmentIDs.removeAll()
            }
            let responseSources = rows(event, "sources").map(sourceRecord)
            messages.append(ChatMessage(role: .assistant, text: "", isStreaming: true, sources: responseSources))
            activeSources = responseSources
        case "assistant_delta":
            if let text = event["text"] as? String,
               let index = messages.lastIndex(where: { $0.role == .assistant && $0.isStreaming }) { messages[index].text += text }
        case "assistant_end":
            isLLMTurnPending = false
            applyLLMRoute(event["llm_route"])
            if let performance = event["performance"] as? [String: Any] {
                firstTokenSeconds = number(performance["first_token_seconds"]) ?? firstTokenSeconds
                firstAudioSeconds = number(performance["first_audio_seconds"]) ?? firstAudioSeconds
                responseSeconds = number(performance["total_seconds"]) ?? responseSeconds
            }
            if let index = messages.lastIndex(where: { $0.role == .assistant && $0.isStreaming }) {
                let interrupted = event["interrupted"] as? Bool ?? false
                let finalText = event["text"] as? String ?? ""
                if interrupted && finalText.isEmpty { messages.remove(at: index) }
                else { if !finalText.isEmpty { messages[index].text = finalText }; messages[index].isStreaming = false; messages[index].wasInterrupted = interrupted }
            }
            responseSeconds = number(event["seconds"])
            lastAssistantSpoken = event["spoken"] as? Bool
            let actionTaskID = (event["task_id"] as? String) ?? currentTaskID
            quickActions = rows(event, "quick_actions").map {
                quickActionRecord($0, fallbackTaskID: actionTaskID)
            }
            if let ttsError = event["tts_error"] as? String,
               !ttsError.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                speechErrorMessage = ttsError
                speechErrorRetryable = true
                speechErrorTaskID = event["task_id"] as? String ?? currentTaskID
            } else if lastAssistantSpoken == true {
                dismissSpeechError()
            }
        case "speech_error":
            speechErrorMessage = event["message"] as? String ?? "Не удалось озвучить ответ"
            speechErrorRetryable = event["retryable"] as? Bool ?? true
            speechErrorTaskID = event["task_id"] as? String ?? currentTaskID
        case "speech_recovered":
            let recoveredTaskID = event["task_id"] as? String
            if recoveredTaskID == nil || recoveredTaskID == speechErrorTaskID {
                dismissSpeechError()
            }
            lastAssistantSpoken = true
            statusText = "Ответ озвучен"
        case "pilot_metrics_exported":
            statusText = "Обезличенная сводка пилота сохранена"
        case "pilot_feedback_saved":
            statusText = "Оценка полезности сохранена"
        case "pilot_preflight":
            if let report = event["result"] as? [String: Any] {
                applyPilotPreflight(report)
            }
        case "task_context":
            activeSources = rows(event, "sources").map(sourceRecord)
        case "routing_fallback":
            applyLLMRoute(event["llm_route"])
            let message = event["message"] as? String
                ?? "Удалённый маршрут недоступен — продолжаю локально"
            routingFallbackMessage = message
            statusText = message
        case "routing_blocked":
            isLLMTurnPending = false
            let message = event["message"] as? String
                ?? "Передача чувствительных данных этому маршруту заблокирована"
            statusText = "Нужен локальный маршрут или другая классификация"
            errorMessage = message
        case "interrupted": statusText = "Перебили — слушаю…"
        case "metric":
            switch event["name"] as? String {
            case "stt": sttSeconds = number(event["seconds"])
            case "llm_first_token": firstTokenSeconds = number(event["seconds"])
            case "voice_first_audio": firstAudioSeconds = number(event["seconds"])
            case "response_total": responseSeconds = number(event["seconds"])
            default: break
            }
        case "search_results":
            searchResults = ((event["results"] as? [[String: Any]]) ?? []).map {
                EntityRecord(
                    id: string($0, "id"),
                    title: string($0, "title"),
                    subtitle: string($0, "kind"),
                    detail: string($0, "snippet"),
                    kind: string($0, "kind"),
                    path: $0["path"] as? String,
                    chunkID: $0["chunk_id"] as? String,
                    charStart: optionalInt($0["char_start"]),
                    charEnd: optionalInt($0["char_end"]),
                    excerpt: string($0, "excerpt")
                )
            }
        case "artifact_versions":
            applyArtifactHistoryPayload(event, restored: false)
        case "artifact_restored":
            applyArtifactHistoryPayload(event, restored: true)
        case "artifact_relations":
            let artifactID = string(event, "artifact_id")
            guard artifactHistoryArtifact?.id == artifactID else { break }
            artifactRelations = rows(event, "relations").map(artifactRelationRecord)
            artifactRelationsLoading = false
        case "quick_action_completed":
            if let action = pendingQuickAction {
                completedQuickActionKeys.insert(quickActionKey(action))
                let result = event["result"] as? [String: Any] ?? [:]
                let created = bool(result["created"])
                statusText = created ? "Быстрое действие выполнено" : "Результат уже был сохранён"
            } else {
                statusText = "Быстрое действие выполнено"
            }
            pendingQuickAction = nil
            quickActionPendingID = nil
        case "audio_import_started":
            meetingAudioImportInProgress = true
            meetingAudioImportStage = "Аудиозапись добавлена · готовлю распознавание…"
            statusText = "Аудиозапись добавлена"
        case "transcription_started":
            meetingAudioImportInProgress = true
            meetingAudioImportStage = "Распознаю речь локально…"
            statusText = "Распознаю аудиозапись…"
        case "transcription_completed":
            meetingAudioImportInProgress = true
            meetingAudioImportStage = "Транскрипт готов · создаю карточку встречи…"
            statusText = "Транскрипт готов"
        case "audio_import_completed":
            let source = event["source"] as? [String: Any] ?? [:]
            let eventMeetingID = event["meeting_id"] as? String ?? ""
            let sourceMeetingID = string(source, "meeting_id")
            let sourceID = string(source, "id")
            pendingImportedMeetingID = eventMeetingID.isEmpty
                ? (sourceMeetingID.isEmpty ? pendingImportedMeetingID : sourceMeetingID)
                : eventMeetingID
            if !sourceID.isEmpty { pendingImportedMeetingSourceID = sourceID }
            meetingAudioImportInProgress = false
            meetingAudioImportStage = "Встреча добавлена"
            statusText = "Встреча из аудиозаписи добавлена"
            meetingImportKind = "audio"
        case "audio_import_error":
            let message = event["message"] as? String ?? "Не удалось обработать аудиозапись"
            meetingAudioImportInProgress = false
            meetingAudioImportStage = nil
            meetingIDsBeforeAudioImport.removeAll()
            pendingImportedMeetingID = nil
            pendingImportedMeetingSourceID = nil
            statusText = "Не удалось обработать аудиозапись"
            errorMessage = message
        case "source_imported":
            let source = event["source"] as? [String: Any] ?? [:]
            let sourceKind = string(source, "kind")
            if meetingAudioImportInProgress && (sourceKind.isEmpty || sourceKind == "meeting") {
                let meetingID = string(source, "meeting_id")
                let sourceID = string(source, "id")
                pendingImportedMeetingID = meetingID.isEmpty ? nil : meetingID
                pendingImportedMeetingSourceID = sourceID.isEmpty ? nil : sourceID
                meetingAudioImportInProgress = false
                meetingAudioImportStage = "Встреча добавлена"
                statusText = "Встреча из аудиозаписи добавлена"
            } else {
                statusText = sourceKind == "meeting" ? "Встреча добавлена" : "Источник добавлен"
            }
        case "synapse_package_imported":
            let result = event["result"] as? [String: Any] ?? [:]
            let meetingID = string(result, "meeting_id")
            let sourceID = string(result, "source_id")
            let importStatus = string(result, "status")
            pendingImportedMeetingID = meetingID.isEmpty ? nil : meetingID
            pendingImportedMeetingSourceID = sourceID.isEmpty ? nil : sourceID
            meetingAudioImportInProgress = false
            meetingAudioImportStage = importStatus == "already_imported"
                ? "Пакет уже был импортирован · открываю встречу"
                : "Пакет eXpress (Синапс) импортирован · контекст обогащён"
            statusText = importStatus == "already_imported"
                ? "Пакет eXpress (Синапс) уже есть в рабочем контексте"
                : "Встреча из eXpress (Синапс) добавлена"
            meetingImportKind = "audio"
        case "express_sync_completed":
            let added = Int(number(event["added"]) ?? 0)
            let hasMore = bool(event["has_more"])
            expressSyncInProgress = false
            expressConnectorConnected = true
            let resultText = added > 0
                ? "Новые встречи eXpress добавлены: \(added)"
                : "Новых встреч eXpress нет"
            meetingAudioImportStage = hasMore ? "\(resultText) · есть ещё" : resultText
            statusText = meetingAudioImportStage ?? "Синхронизация eXpress завершена"
        case "express_sync_error":
            let message = event["message"] as? String
                ?? "Не удалось синхронизировать встречи eXpress"
            expressSyncInProgress = false
            meetingAudioImportStage = nil
            statusText = "Синхронизация eXpress не выполнена"
            errorMessage = message
        case "entity_deleted":
            let title = (event["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let recovery = event["recovery"] as? String ?? "database_only"
            if title.isEmpty {
                statusText = recovery == "trash" ? "Запись удалена · файлы в Корзине" : "Запись удалена"
            } else {
                statusText = recovery == "trash"
                    ? "«\(title)»: запись удалена · файлы в Корзине"
                    : "«\(title)» удалён из RnD Workbench"
            }
        case "automation_started": statusText = "Выполняется автоматизация…"
        case "automation_completed": statusText = "Автоматизация завершена"
        case "cleared": messages.removeAll()
        case "notice": if let message = event["message"] as? String { statusText = message }
        case "error":
            let message = event["message"] as? String ?? "Неизвестная ошибка"
            isLLMTurnPending = false
            submittedAttachmentIDs.removeAll()
            pendingQuickAction = nil
            quickActionPendingID = nil
            if artifactHistoryLoading || artifactRelationsLoading || artifactRestorePendingVersion != nil {
                artifactHistoryLoading = false
                artifactRelationsLoading = false
                artifactRestorePendingVersion = nil
                artifactHistoryError = message
            }
            if isVoiceStartPending {
                isVoiceStartPending = false
                isSessionActive = false
                statusText = "Не удалось запустить голосовой режим"
            }
            if meetingAudioImportInProgress {
                let failedKind = meetingImportKind
                meetingAudioImportInProgress = false
                meetingAudioImportStage = nil
                meetingIDsBeforeAudioImport.removeAll()
                pendingImportedMeetingID = nil
                pendingImportedMeetingSourceID = nil
                statusText = failedKind == "synapse"
                    ? "Не удалось импортировать пакет eXpress (Синапс)"
                    : "Не удалось обработать аудиозапись"
                meetingImportKind = "audio"
            }
            if llmConfigurationPending {
                if pendingLLMMode == "external" || pendingLLMMode == "auto" { externalLLMReady = false }
                llmConfigurationPending = false
                pendingLLMMode = nil
                pendingCredentialDeletionEndpoint = nil
                llmConfigurationError = message
                statusText = "Не удалось настроить модель"
            } else if statusText.hasPrefix("Удаляю ") {
                statusText = "Не удалось удалить объект"
            }
            errorMessage = message
        case "fatal":
            isLLMTurnPending = false
            submittedAttachmentIDs.removeAll()
            pendingQuickAction = nil
            quickActionPendingID = nil
            isVoiceStartPending = false
            meetingAudioImportInProgress = false
            meetingAudioImportStage = nil
            pendingCredentialDeletionEndpoint = nil
            fail(event["message"] as? String ?? "Неизвестная ошибка")
        default: break
        }
    }

    private func applyLLMRoute(_ value: Any?) {
        guard let route = value as? [String: Any] else { return }
        let actualRoute = string(route, "actual_route")
        if !actualRoute.isEmpty { actualLLMRoute = actualRoute }
        let routeModel = string(route, "model")
        if !routeModel.isEmpty { actualLLMModel = routeModel }
        if let configured = route["java_core_configured"] as? Bool {
            javaCorePolicyConfigured = configured
        }
        if let ready = route["java_core_ready"] as? Bool {
            javaCorePolicyReady = ready
        }
    }

    private func applyArtifactHistoryPayload(_ event: [String: Any], restored: Bool) {
        let artifactData = event["artifact"] as? [String: Any] ?? [:]
        let artifactID = string(artifactData, "id")
        guard !artifactID.isEmpty, artifactHistoryArtifact?.id == artifactID else { return }

        artifactHistoryArtifact = artifactRecord(artifactData)
        artifactVersions = rows(event, "versions").map(artifactVersionRecord)
        artifactRelations = rows(event, "relations").map(artifactRelationRecord)
        artifactHistoryLoading = false
        artifactRelationsLoading = false
        artifactHistoryError = nil
        if restored {
            artifactRestorePendingVersion = nil
            statusText = "Версия восстановлена как новая текущая"
        }
    }

    private func applySnapshot(_ data: [String: Any]) {
        workspaces = rows(data, "workspaces").map {
            WorkspaceRecord(
                id: string($0, "id"),
                name: string($0, "name"),
                description: string($0, "description"),
                status: string($0, "status"),
                classification: string($0, "classification").isEmpty
                    ? "internal" : string($0, "classification")
            )
        }
        currentWorkspaceID = string(data, "current_workspace_id")
        workspaceTimeline = rows(data, "workspace_timeline").map { row in
            let target = row["target"] as? [String: Any] ?? [:]
            let source = row["source"] as? [String: Any] ?? [:]
            return WorkspaceTimelineRecord(
                id: string(row, "id"),
                type: string(row, "type"),
                title: string(row, "title"),
                detail: string(row, "detail"),
                timestamp: string(row, "timestamp"),
                status: string(row, "status"),
                targetSection: string(target, "section"),
                targetType: string(target, "entity_type"),
                targetID: string(target, "entity_id"),
                sourceID: source["id"] as? String,
                sourceTitle: source["title"] as? String,
                sourcePath: source["path"] as? String,
                sourceStart: optionalInt(source["char_start"]),
                sourceEnd: optionalInt(source["char_end"]),
                sourceExcerpt: string(source, "excerpt"),
                decisionThreadKey: row["decision_thread_key"] as? String,
                decisionSequence: optionalInt(row["decision_sequence"]),
                decisionCount: optionalInt(row["decision_count"]),
                isCurrentDecision: bool(row["is_current_decision"]),
                currentDecisionText: string(row, "current_decision_text")
            )
        }
        currentTaskID = data["current_task_id"] as? String
        if let speechErrorTaskID, speechErrorTaskID != currentTaskID {
            dismissSpeechError()
        }
        tasks = rows(data, "tasks").map {
            TaskRecord(
                id: string($0, "id"),
                workspaceID: string($0, "workspace_id"),
                title: string($0, "title"),
                status: string($0, "status"),
                plan: $0["plan"] as? [String] ?? [],
                result: string($0, "result"),
                skillID: $0["skill_id"] as? String,
                updatedAt: string($0, "updated_at"),
                classification: string($0, "classification").isEmpty
                    ? "internal" : string($0, "classification")
            )
        }
        meetings = rows(data, "meetings").map { row in
            let rawCounts = row["item_counts"] as? [String: Any] ?? [:]
            return MeetingRecord(
                id: string(row, "id"),
                workspaceID: string(row, "workspace_id"),
                sourceID: string(row, "source_id"),
                title: string(row, "title"),
                occurredAt: string(row, "occurred_at"),
                participants: row["participants"] as? [String] ?? [],
                summary: string(row, "summary"),
                status: string(row, "status"),
                analyzedAt: string(row, "analyzed_at"),
                itemCounts: rawCounts.reduce(into: [:]) { result, pair in result[pair.key] = int(pair.value) },
                openAttention: int(row["open_attention"]),
                sourcePath: row["source_path"] as? String
            )
        }
        let snapshotMeetingID = data["current_meeting_id"] as? String
        currentMeetingID = snapshotMeetingID
        if let importedMeeting = meetingToOpenAfterAudioImport() {
            currentMeetingID = importedMeeting.id
            pendingImportedMeetingID = nil
            pendingImportedMeetingSourceID = nil
            meetingIDsBeforeAudioImport.removeAll()
            meetingAudioImportStage = nil
            if snapshotMeetingID != importedMeeting.id {
                selectMeeting(importedMeeting.id)
            }
        }
        meetingItems = rows(data, "meeting_items").map { row in
            MeetingItemRecord(
                id: string(row, "id"),
                meetingID: string(row, "meeting_id"),
                kind: string(row, "kind"),
                text: string(row, "text"),
                owner: string(row, "owner"),
                dueAt: string(row, "due_at"),
                topic: string(row, "topic"),
                status: string(row, "status"),
                sourceQuote: string(row, "source_quote"),
                sourceStart: optionalInt(row["source_start"]),
                sourceEnd: optionalInt(row["source_end"]),
                confidence: number(row["confidence"])
            )
        }
        meetingDiff = rows(data, "meeting_diff").enumerated().map { index, row in
            EntityRecord(
                id: string(row, "id").isEmpty ? "meeting-diff-\(index)" : string(row, "id"),
                title: string(row, "title"),
                subtitle: string(row, "kind"),
                detail: string(row, "detail"),
                status: string(row, "status")
            )
        }
        let briefing = data["meeting_briefing"] as? String
        meetingBriefing = briefing?.isEmpty == false ? briefing : nil
        attentionEvents = rows(data, "attention_events").map { row in
            AttentionEventRecord(
                key: string(row, "key"),
                title: string(row, "title"),
                reason: string(row, "reason"),
                score: number(row["score"]) ?? 0,
                severity: string(row, "severity"),
                kind: string(row, "kind"),
                entityID: string(row, "entity_id"),
                workspaceID: string(row, "workspace_id"),
                sourceID: string(row, "source_id"),
                sourcePath: row["source_path"] as? String,
                dueAt: string(row, "due_at"),
                actionLabel: string(row, "action_label")
            )
        }
        messages = rows(data, "messages").map { row in
            let metadata = decodeJSONObject(row["metadata"] as? String)
            let messageSources = rows(metadata, "sources").map(sourceRecord)
            return ChatMessage(id: string(row, "id"), role: string(row, "role") == "user" ? .user : .assistant, text: string(row, "content"), wasInterrupted: metadata["interrupted"] as? Bool ?? false, sources: messageSources)
        }
        activeSources = rows(data, "task_sources").map(sourceRecord)
        sources = rows(data, "sources").map {
            EntityRecord(
                id: string($0, "id"), title: string($0, "title"),
                subtitle: string($0, "kind"), detail: string($0, "created_at"),
                path: $0["path"] as? String,
                classification: string($0, "classification").isEmpty
                    ? "internal" : string($0, "classification")
            )
        }
        memory = rows(data, "memory").map { row in
            let kind = string(row, "kind")
            return EntityRecord(
                id: string(row, "id"), title: string(row, "title"),
                subtitle: MemoryKind(rawValue: kind)?.title ?? kind,
                detail: string(row, "content"), kind: kind,
                classification: string(row, "classification").isEmpty
                    ? "internal" : string(row, "classification")
            )
        }
        skills = rows(data, "skills").map {
            EntityRecord(
                id: string($0, "id"), title: string($0, "name"),
                subtitle: string($0, "command"), detail: string($0, "description"),
                content: string($0, "instruction"), status: string($0, "scope"),
                command: string($0, "command"), enabled: bool($0["enabled"]),
                version: int($0["version"]),
                classification: string($0, "classification").isEmpty
                    ? "internal" : string($0, "classification")
            )
        }
        capabilities = rows(data, "capabilities").map { EntityRecord(id: string($0, "id"), title: string($0, "name"), subtitle: string($0, "category"), detail: string($0, "description"), status: string($0, "status"), risk: string($0, "risk")) }
        artifacts = rows(data, "artifacts").map(artifactRecord)
        quickActions = rows(data, "quick_actions").map {
            quickActionRecord($0, fallbackTaskID: currentTaskID)
        }
        inbox = rows(data, "inbox").map {
            EntityRecord(
                id: string($0, "id"),
                title: string($0, "title"),
                subtitle: string($0, "kind"),
                detail: string($0, "detail"),
                status: string($0, "status"),
                kind: string($0, "kind"),
                sourceRef: $0["source_ref"] as? String
            )
        }
        approvals = rows(data, "approvals").map { row in
            let actionType = string(row, "action_type")
            let risk = string(row, "risk")
            let policy = string(row, "confirmation_policy")
            let step = int(row["step_index"])
            let revision = max(1, int(row["revision"]))
            let actor = string(row, "actor")
            let origin = string(row, "origin")
            let resolvedBy = string(row, "resolved_by")
            let resolvedAt = string(row, "resolved_at")
            let result = string(row, "result")
            let actorLabel = actor == "local-user" ? "локальный пользователь" : actor == "system" ? "система" : actor
            let originLabel = origin == "user_request" ? "запрос пользователя" : origin == "approval_center" ? "центр согласований" : origin == "assistant" ? "помощник" : origin
            let riskLabel = ["low": "низкий", "medium": "средний", "high": "высокий", "critical": "критический"][risk] ?? risk
            let policyLabel = ["none": "не требуется", "explicit": "явное", "two_step": "двухэтапное"][policy] ?? policy
            let resolverLabel = resolvedBy == "local-user" ? "локальный пользователь" : resolvedBy == "system" ? "система" : resolvedBy
            var history = [
                "Инициатор: \(actorLabel.isEmpty ? "не указан" : actorLabel)",
                "Источник: \(originLabel.isEmpty ? "не указан" : originLabel)",
                "Риск: \(riskLabel.isEmpty ? "средний" : riskLabel) · подтверждение: \(policyLabel.isEmpty ? "явное" : policyLabel)",
            ]
            if !resolvedBy.isEmpty {
                history.append(
                    "Решение: \(resolverLabel)" + (resolvedAt.isEmpty ? "" : " · \(displayArtifactDate(resolvedAt))")
                )
            }
            if !result.isEmpty { history.append("Результат: \(result)") }
            return EntityRecord(
                id: string(row, "id"),
                title: string(row, "title"),
                subtitle: step > 0 ? "\(actionType) · шаг \(step) · редакция \(revision)" : actionType,
                detail: history.joined(separator: "\n"),
                content: string(row, "payload"),
                status: string(row, "status"),
                risk: risk,
                version: revision
            )
        }
        automations = rows(data, "automations").map { EntityRecord(id: string($0, "id"), title: string($0, "name"), subtitle: string($0, "schedule"), detail: string($0, "prompt"), status: string($0, "next_run_at"), enabled: bool($0["enabled"])) }
        taskEvents = rows(data, "task_events").map { EntityRecord(id: string($0, "id"), title: string($0, "title"), subtitle: string($0, "kind"), detail: string($0, "detail"), status: string($0, "created_at")) }
        audit = rows(data, "audit").map { EntityRecord(id: string($0, "id"), title: string($0, "action"), subtitle: string($0, "status"), detail: string($0, "detail"), status: string($0, "created_at")) }
        settings = (data["settings"] as? [String: String]) ?? [:]
        pilotMetrics = (data["pilot_metrics"] as? [String: Any]) ?? [:]
        if let report = data["pilot_preflight"] as? [String: Any] {
            applyPilotPreflight(report)
        }
        if let onboarding = data["pilot_onboarding"] as? [String: Any] {
            applyPilotOnboarding(onboarding)
        }
        if let connector = data["express_connector"] as? [String: Any] {
            expressConnectorConfigured = bool(connector["configured"])
            expressConnectorConnected = bool(connector["connected"])
        } else {
            expressConnectorConfigured = false
            expressConnectorConnected = false
        }
        if let platform = data["platform"] as? [String: Any],
           let javaPolicy = platform["java_core_policy"] as? [String: Any] {
            javaCorePolicyConfigured = bool(javaPolicy["configured"])
            javaCorePolicyReady = bool(javaPolicy["ready"])
            if let actionJournal = platform["java_action_journal"] as? [String: Any] {
                javaActionJournalReady = bool(actionJournal["ready"])
                javaAutonomyPolicyReady = bool(actionJournal["autonomy_policy_ready"])
                if let recovery = actionJournal["recovery"] as? [String: Any] {
                    actionRecoveryAttention = Int(
                        number(recovery["requires_attention"]) ?? 0
                    )
                }
            }
        }
        if let llm = data["llm"] as? [String: Any] {
            let snapshotMode = string(llm, "mode")
            let snapshotBaseURL = string(llm, "base_url")
            let snapshotRemoteModel = string(llm, "remote_model")
            let snapshotModel = snapshotMode == "auto" && !snapshotRemoteModel.isEmpty
                ? snapshotRemoteModel : string(llm, "model")
            let snapshotConfiguredProvider = string(llm, "configured_provider_type")
            let snapshotAutoPolicy = string(llm, "auto_remote_policy")
            if !snapshotMode.isEmpty { settings["llm_mode"] = snapshotMode }
            if !snapshotBaseURL.isEmpty { settings["external_llm_base_url"] = snapshotBaseURL }
            if !snapshotModel.isEmpty { settings["external_llm_model"] = snapshotModel }
            if !snapshotConfiguredProvider.isEmpty {
                settings["external_provider_type"] = snapshotConfiguredProvider
            }
            if !snapshotAutoPolicy.isEmpty { settings["auto_remote_policy"] = snapshotAutoPolicy }
            if isExternalLLMActive, didHydrateExternalLLM, !llmConfigurationPending {
                externalLLMReady = bool(llm["ready"])
            } else if isAutoLLMActive, didHydrateExternalLLM, !llmConfigurationPending {
                externalLLMReady = bool(llm["remote_ready"])
            }
        }
        let snapshotModelLabel = string(data, "model")
        if !snapshotModelLabel.isEmpty { modelName = snapshotModelLabel }
        refreshExternalLLMKeyStatus(for: externalLLMBaseURL)
        let shouldHydrateRemote = isExternalLLMActive || (
            isAutoLLMActive && autoRemotePolicy == "eligible"
                && !externalLLMBaseURL.isEmpty && !externalLLMModel.isEmpty
        )
        if shouldHydrateRemote {
            if !didHydrateExternalLLM {
                externalLLMReady = false
                hydrateExternalLLMFromSnapshotIfNeeded()
            }
        } else if isAutoLLMActive {
            didHydrateExternalLLM = true
            externalLLMReady = false
            actualLLMRoute = "local_mlx"
        } else if !llmConfigurationPending {
            externalLLMReady = true
            actualLLMRoute = "local_mlx"
        }
        if let today = data["today"] as? [String: Any] {
            dashboard = DashboardStats(activeTasks: int(today["active_tasks"]), attention: int(today["attention"]), sources: int(today["sources"]), artifacts: int(today["artifacts"]))
        }
    }

    private func refreshExternalLLMKeyStatus(for baseURL: String) {
        guard let canonicalEndpoint = ExternalLLMEndpoint.canonicalized(baseURL) else {
            hasExternalLLMAPIKey = false
            return
        }
        hasExternalLLMAPIKey = (try? ExternalLLMKeychain.read(canonicalEndpoint: canonicalEndpoint))?.isEmpty == false
    }

    private func hydrateExternalLLMFromSnapshotIfNeeded() {
        guard !didHydrateExternalLLM else { return }
        didHydrateExternalLLM = true
        let restoringAuto = isAutoLLMActive
        let baseURL = externalLLMBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let model = externalLLMModel.trimmingCharacters(in: .whitespacesAndNewlines)
        if let validationError = ExternalLLMEndpoint.validationError(baseURL) {
            showRemoteHydrationError("Сохранённый адрес удалённой модели недействителен. \(validationError)", auto: restoringAuto)
            return
        }
        guard !model.isEmpty else {
            showRemoteHydrationError("Для удалённого провайдера не сохранён идентификатор модели", auto: restoringAuto)
            return
        }
        guard let canonicalEndpoint = ExternalLLMEndpoint.canonicalized(baseURL) else {
            showRemoteHydrationError("Не удалось безопасно нормализовать сохранённый адрес провайдера", auto: restoringAuto)
            return
        }
        do {
            let apiKey = try ExternalLLMKeychain.read(canonicalEndpoint: canonicalEndpoint) ?? ""
            hasExternalLLMAPIKey = !apiKey.isEmpty
            guard !apiKey.isEmpty || ExternalLLMEndpoint.isLoopback(canonicalEndpoint) else {
                externalLLMReady = false
                showRemoteHydrationError(
                    "Ключ API не найден в Связке ключей macOS. Перейдите в Настройки → Данные и модели.",
                    auto: restoringAuto
                )
                return
            }
            llmConfigurationPending = true
            pendingLLMMode = restoringAuto ? "auto" : "external"
            llmConfigurationError = nil
            statusText = restoringAuto
                ? "Восстанавливаю удалённый маршрут для Авто…"
                : "Восстанавливаю настройки удалённой модели…"
            send([
                "command": "configure_llm",
                "mode": restoringAuto ? "auto" : "external",
                "base_url": canonicalEndpoint,
                "model": model,
                "api_key": apiKey,
                "provider_type": externalProviderType,
                "auto_remote_policy": autoRemotePolicy,
            ])
        } catch {
            externalLLMReady = false
            showRemoteHydrationError("Не удалось прочитать ключ API: \(error.localizedDescription)", auto: restoringAuto)
        }
    }

    private func showRemoteHydrationError(_ message: String, auto: Bool) {
        if auto {
            llmConfigurationPending = false
            pendingLLMMode = nil
            llmConfigurationError = message
            statusText = "Авто использует локальную модель · удалённый маршрут недоступен"
        } else {
            showLLMConfigurationError(message)
        }
    }

    private func ensureLLMRouteAvailable() -> Bool {
        guard isExternalLLMActive else { return true }
        if llmConfigurationPending {
            errorMessage = "Подождите, пока конфигурация модели будет применена"
            return false
        }
        guard externalLLMReady else {
            let message = llmConfigurationError
                ?? "Внешняя модель ещё не настроена. Проверьте адрес, модель и ключ API в настройках."
            showLLMConfigurationError(message)
            return false
        }
        return true
    }

    private func showLLMConfigurationError(_ message: String) {
        llmConfigurationPending = false
        pendingLLMMode = nil
        llmConfigurationError = message
        statusText = "Настройки внешней модели не применены"
        errorMessage = message
    }

    private func meetingToOpenAfterAudioImport() -> MeetingRecord? {
        if let meetingID = pendingImportedMeetingID,
           let meeting = meetings.first(where: { $0.id == meetingID }) {
            return meeting
        }
        if let sourceID = pendingImportedMeetingSourceID,
           let meeting = meetings.first(where: { $0.sourceID == sourceID }) {
            return meeting
        }
        guard pendingImportedMeetingID != nil
                || pendingImportedMeetingSourceID != nil
                || meetingAudioImportStage == "Встреча добавлена" else { return nil }
        return meetings.first(where: { !meetingIDsBeforeAudioImport.contains($0.id) })
    }

    private func fail(_ message: String) {
        state = .error
        statusText = "Ошибка"
        errorMessage = message
        isSessionActive = false
        isVoiceStartPending = false
        externalDictationStartPending = false
        externalDictationActive = false
        externalDictationTranscribing = false
        externalDictationTarget = nil
        externalDictationFocusChanged = false
        meetingAudioImportInProgress = false
        meetingAudioImportStage = nil
        artifactHistoryLoading = false
        artifactRelationsLoading = false
        artifactRestorePendingVersion = nil
    }
}

private func rows(_ data: [String: Any], _ key: String) -> [[String: Any]] { data[key] as? [[String: Any]] ?? [] }
private func string(_ data: [String: Any], _ key: String) -> String { data[key] as? String ?? "" }
private func number(_ value: Any?) -> Double? { (value as? NSNumber)?.doubleValue }
private func int(_ value: Any?) -> Int { (value as? NSNumber)?.intValue ?? 0 }
private func optionalInt(_ value: Any?) -> Int? { (value as? NSNumber)?.intValue }
private func bool(_ value: Any?) -> Bool { (value as? NSNumber)?.boolValue ?? false }
private func artifactRecord(_ data: [String: Any]) -> EntityRecord {
    let kind = string(data, "kind")
    let version = int(data["current_version"])
    return EntityRecord(
        id: string(data, "id"),
        title: string(data, "title"),
        subtitle: kind,
        detail: "Версия \(version)",
        kind: kind,
        path: data["path"] as? String,
        version: version,
        classification: string(data, "classification").isEmpty
            ? "internal" : string(data, "classification")
    )
}

private func artifactVersionRecord(_ data: [String: Any]) -> ArtifactVersionRecord {
    let metadata = data["metadata"] as? [String: Any] ?? [:]
    let version = int(data["version"])
    let artifactID = string(data, "artifact_id")
    return ArtifactVersionRecord(
        id: string(data, "id").isEmpty ? "\(artifactID)-v\(version)" : string(data, "id"),
        artifactID: artifactID,
        version: version,
        path: data["path"] as? String,
        createdAt: string(data, "created_at"),
        isCurrent: bool(data["is_current"]),
        restoredFromVersion: optionalInt(metadata["restored_from_version"]),
        metadataSummary: artifactMetadataSummary(metadata)
    )
}

private func artifactRelationRecord(_ data: [String: Any]) -> ArtifactRelationRecord {
    let metadata = data["metadata"] as? [String: Any] ?? [:]
    return ArtifactRelationRecord(
        id: string(data, "id"),
        artifactVersion: int(data["artifact_version"]),
        relationType: string(data, "relation_type"),
        taskID: (data["task_id"] as? String).flatMap { $0.isEmpty ? nil : $0 },
        sourceID: (data["source_id"] as? String).flatMap { $0.isEmpty ? nil : $0 },
        relatedArtifactID: (data["related_artifact_id"] as? String).flatMap { $0.isEmpty ? nil : $0 },
        relatedArtifactVersion: optionalInt(data["related_artifact_version"]),
        metadataSummary: artifactMetadataSummary(metadata),
        createdAt: string(data, "created_at")
    )
}

private func quickActionRecord(
    _ data: [String: Any],
    fallbackTaskID: String?
) -> QuickActionRecord {
    QuickActionRecord(
        id: string(data, "id"),
        title: string(data, "title"),
        command: string(data, "command"),
        taskID: (data["task_id"] as? String) ?? fallbackTaskID,
        artifactID: data["artifact_id"] as? String
    )
}

private func artifactMetadataSummary(_ metadata: [String: Any]) -> String {
    let hiddenKeys: Set<String> = [
        "task_id", "source_ids", "related_artifact_id", "related_artifact_version",
    ]
    let labels = [
        "restored_from_version": "Исходная версия",
        "skill": "Скилл",
        "conversion": "Преобразование",
        "chunk_id": "Фрагмент",
        "selection": "Отбор",
        "char_start": "Начало",
        "char_end": "Конец",
        "score": "Релевантность",
    ]
    return metadata.keys.sorted().compactMap { key in
        guard !hiddenKeys.contains(key), let value = metadata[key] else { return nil }
        let rendered: String
        if let text = value as? String { rendered = text }
        else if let number = value as? NSNumber { rendered = number.stringValue }
        else if let values = value as? [String] { rendered = values.joined(separator: ", ") }
        else if JSONSerialization.isValidJSONObject(value),
                let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
                let text = String(data: data, encoding: .utf8) { rendered = text }
        else { rendered = String(describing: value) }
        return "\(labels[key] ?? key): \(rendered)"
    }.joined(separator: " · ")
}
private func sourceRecord(_ data: [String: Any]) -> EntityRecord {
    let kind = string(data, "kind")
    return EntityRecord(
        id: string(data, "id"),
        title: string(data, "title"),
        subtitle: kind,
        kind: kind,
        path: data["path"] as? String,
        chunkID: data["chunk_id"] as? String,
        charStart: optionalInt(data["char_start"]),
        charEnd: optionalInt(data["char_end"]),
        excerpt: string(data, "excerpt")
    )
}
private func decodeJSONObject(_ text: String?) -> [String: Any] {
    guard let text, let data = text.data(using: .utf8) else { return [:] }
    return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
}

struct StatusOrb: View {
    let state: AssistantState
    @State private var pulse = false
    var body: some View {
        ZStack {
            Circle().fill(state.color.opacity(0.18)).frame(width: 38, height: 38).scaleEffect(pulse ? 1.20 : 0.88).opacity(pulse ? 0.25 : 0.75)
            Circle().fill(RadialGradient(colors: [.white.opacity(0.95), state.color], center: .topLeading, startRadius: 1, endRadius: 22)).frame(width: 20, height: 20).shadow(color: state.color.opacity(0.7), radius: 9)
        }
        .onAppear { updatePulse() }.onChange(of: state) { _, _ in updatePulse() }
    }
    private func updatePulse() {
        pulse = false
        guard [.listening, .thinking, .speaking].contains(state) else { return }
        withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) { pulse = true }
    }
}

struct WorkbenchSupergraphic: View {
    var body: some View {
        GeometryReader { proxy in
            ZStack {
                Rectangle()
                    .fill(RnDTheme.blue.opacity(0.38))
                    .frame(width: proxy.size.width * 1.45, height: 34)
                    .rotationEffect(.degrees(-38))
                    .offset(x: 18, y: proxy.size.height * 0.30)
                Rectangle()
                    .fill(RnDTheme.steel.opacity(0.26))
                    .frame(width: proxy.size.width * 1.55, height: 18)
                    .rotationEffect(.degrees(-38))
                    .offset(x: -20, y: proxy.size.height * 0.47)
                Rectangle()
                    .fill(RnDTheme.red.opacity(0.82))
                    .frame(width: 58, height: 4)
                    .rotationEffect(.degrees(-38))
                    .offset(x: proxy.size.width * 0.34, y: proxy.size.height * 0.14)
            }
            .clipped()
        }
        .allowsHitTesting(false)
    }
}

struct AssistantWorkspaceView: View {
    @ObservedObject var controller: BackendController
    @State private var section = AppSection(
        rawValue: UserDefaults.standard.string(forKey: "rnd-workbench.section") ?? ""
    ) ?? .today
    var body: some View {
        HStack(spacing: 0) {
            sidebar; Divider().opacity(0.4)
            VStack(spacing: 0) { topBar; Divider().opacity(0.35); sectionContent.frame(maxWidth: .infinity, maxHeight: .infinity); Divider().opacity(0.35); UniversalComposer(controller: controller, showQuickActions: false) }
        }
        .frame(width: 1060, height: 720)
        .background(RnDTheme.canvas)
        .foregroundStyle(RnDTheme.ink)
        .tint(RnDTheme.blue)
        .preferredColorScheme(.light)
        .onChange(of: section) { _, value in
            UserDefaults.standard.set(value.rawValue, forKey: "rnd-workbench.section")
        }
        .onChange(of: controller.navigationRequest) { _, request in
            guard let request else { return }
            section = request.section
        }
        .sheet(item: $controller.sourcePreview) { source in
            SourceExcerptPreview(source: source, onOpenFile: controller.openSourceFile)
        }
        .sheet(
            item: Binding(
                get: { controller.artifactHistoryArtifact },
                set: { if $0 == nil { controller.closeArtifactHistory() } }
            )
        ) { artifact in
            ArtifactHistorySheet(controller: controller, requestedArtifact: artifact)
        }
        .alert("RnD Workbench", isPresented: Binding(get: { controller.errorMessage != nil }, set: { if !$0 { controller.errorMessage = nil } })) { Button("Закрыть", role: .cancel) { controller.errorMessage = nil } } message: { Text(controller.errorMessage ?? "") }
    }

    private var sidebar: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                StatusOrb(state: controller.state)
                VStack(alignment: .leading, spacing: 2) {
                    Text("RnD Workbench").font(.system(size: 16, weight: .bold, design: .rounded))
                    Label(
                        controller.configuredLLMModeLabel,
                        systemImage: controller.isAutoLLMActive
                            ? "arrow.triangle.branch"
                            : controller.isExternalLLMActive ? "network" : "lock.shield.fill"
                    )
                    .font(.caption2)
                    .foregroundStyle(controller.isExternalLLMActive ? RnDTheme.red : RnDTheme.steel)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(.horizontal, 13)
            .padding(.top, 12)
            List(selection: $section) {
                ForEach(sidebarGroups) { group in
                    Section(group.title.uppercased()) {
                        ForEach(group.items) { item in
                            Label(item.title, systemImage: item.icon)
                                .tag(item)
                                .foregroundStyle(.white)
                                .listRowBackground(section == item ? RnDTheme.blue : Color.clear)
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .environment(\.colorScheme, .dark)
            VStack(alignment: .leading, spacing: 4) {
                Text(controller.modelName).font(.caption2).lineLimit(2)
                if controller.isExternalLLMActive {
                    Label(
                        controller.externalLLMReady ? "Внешний маршрут настроен" : "Внешний маршрут требует настройки",
                        systemImage: "network"
                    )
                    .font(.caption2)
                    .foregroundStyle(RnDTheme.red)
                    Label(controller.actualLLMRouteStatusLabel, systemImage: controller.actualLLMRouteIcon)
                        .font(.caption2)
                        .foregroundStyle(controller.isRemoteRouteActive ? RnDTheme.red : RnDTheme.steel)
                } else if controller.isAutoLLMActive {
                    Label(controller.actualLLMRouteStatusLabel, systemImage: controller.actualLLMRouteIcon)
                        .font(.caption2)
                        .foregroundStyle(controller.isRemoteRouteActive ? RnDTheme.red : RnDTheme.steel)
                } else {
                    HStack(spacing: 5) {
                        Circle().fill(controller.isReady ? Color.white : RnDTheme.red).frame(width: 6, height: 6)
                        Text(controller.isReady ? "Локальные модели готовы" : "Загрузка \(controller.loadedModels.count)/3")
                    }
                    .font(.caption2)
                    .foregroundStyle(RnDTheme.steel)
                }
            }
            .padding(13)
        }
        .foregroundStyle(.white)
        .frame(width: 250)
        .background(ZStack { RnDTheme.navy; WorkbenchSupergraphic().opacity(0.72) })
    }

    private var topBar: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) { Text(section.title).font(.system(size: 19, weight: .bold, design: .rounded)); Text(controller.statusText).font(.caption).foregroundStyle(controller.state.color).lineLimit(1) }
                .layoutPriority(1)
            Spacer()
            Menu { ForEach(controller.workspaces) { workspace in Button(workspace.name) { controller.selectWorkspace(workspace.id) } } } label: { Label(controller.currentWorkspace?.name ?? "Рабочее пространство", systemImage: "square.stack.3d.up").lineLimit(1) }
                .menuStyle(.borderlessButton)
                .frame(maxWidth: 185)
            Button {
                controller.presentCompact()
            } label: {
                Label("Виджет", systemImage: "macwindow.on.rectangle")
            }
            .buttonStyle(.bordered)
            .fixedSize(horizontal: true, vertical: false)
            .help("Перейти в компактный виджет")
            .accessibilityLabel("Перейти в компактный виджет")
            Button { controller.newTask(); section = .tasks } label: { Label("Новая задача", systemImage: "plus") }.buttonStyle(.bordered).fixedSize(horizontal: true, vertical: false)
        }.padding(.horizontal, 18).padding(.vertical, 11).background(RnDTheme.panel)
    }

    @ViewBuilder private var sectionContent: some View {
        switch section {
        case .today: TodayView(controller: controller, section: $section)
        case .workspaces: WorkspacesView(controller: controller)
        case .tasks: TasksView(controller: controller)
        case .meetings: MeetingsView(controller: controller)
        case .search: SearchView(controller: controller)
        case .inbox: InboxView(controller: controller)
        case .skills: SkillsView(controller: controller)
        case .capabilities: CapabilitiesView(controller: controller)
        case .artifacts: ArtifactsView(controller: controller)
        case .automations: AutomationsView(controller: controller)
        case .approvals: ApprovalsView(controller: controller)
        case .settings: SettingsView(controller: controller)
        }
    }
}

struct TodayView: View {
    @ObservedObject var controller: BackendController
    @Binding var section: AppSection
    let columns = [GridItem(.adaptive(minimum: 150), spacing: 12)]
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 16) {
            FocusHero(controller: controller) {
                if let task = controller.currentTask { controller.selectTask(task.id) }
                else { controller.newTask() }
                section = .tasks
            }
            LazyVGrid(columns: columns, spacing: 12) {
                StatCard(title: "Активные задачи", value: controller.dashboard.activeTasks, icon: "checklist", color: RnDTheme.navy) { section = .tasks }
                StatCard(title: "Требует внимания", value: controller.dashboard.attention, icon: "bell.badge.fill", color: RnDTheme.red) { section = .inbox }
                StatCard(title: "Источники", value: controller.dashboard.sources, icon: "doc.text.magnifyingglass", color: RnDTheme.blue) { section = .workspaces }
                StatCard(title: "Материалы", value: controller.dashboard.artifacts, icon: "doc.richtext.fill", color: RnDTheme.steel) { section = .artifacts }
            }
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    SectionHeader("Продолжить работу", action: "Все задачи") { section = .tasks }
                    if controller.tasks.isEmpty {
                        EmptyState(icon: "checklist", title: "Задач пока нет", detail: "Поставьте первую задачу текстом или голосом.")
                    } else {
                        ForEach(controller.tasks.prefix(3)) { task in
                            TaskRow(task: task) { controller.selectTask(task.id); section = .tasks }
                                .padding(9)
                                .background(RoundedRectangle(cornerRadius: 10).fill(RnDTheme.canvas))
                        }
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 230, alignment: .topLeading)
                .padding(16)
                .background(RoundedRectangle(cornerRadius: 16).fill(RnDTheme.panel))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(RnDTheme.line))

                AttentionPanel(
                    events: controller.attentionEvents,
                    limit: 4,
                    compact: true,
                    onExplain: controller.explainAttention,
                    onShowAll: { section = .inbox },
                    onOpenSource: controller.openSource
                )
                .frame(maxWidth: .infinity, minHeight: 230, alignment: .topLeading)
            }
        }.padding(22) }
    }
}

struct AttentionPanel: View {
    let events: [AttentionEventRecord]
    let limit: Int
    let compact: Bool
    let onExplain: () -> Void
    let onShowAll: (() -> Void)?
    let onOpenSource: (EntityRecord) -> Void

    private var visibleEvents: ArraySlice<AttentionEventRecord> { events.prefix(limit) }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 9 : 12) {
            header
            if events.isEmpty {
                calmState
            } else {
                ForEach(Array(visibleEvents.enumerated()), id: \.element.id) { index, event in
                    AttentionPriorityRow(
                        rank: index + 1,
                        event: event,
                        compact: compact,
                        onOpenSource: onOpenSource
                    )
                }
                if events.count > limit {
                    Text("Ещё приоритетов: \(events.count - limit)")
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(RnDTheme.blue)
                        .accessibilityLabel("Ещё \(events.count - limit) приоритетов")
                }
            }
        }
        .padding(compact ? 14 : 18)
        .background(
            ZStack(alignment: .topTrailing) {
                RoundedRectangle(cornerRadius: 16).fill(RnDTheme.panel)
                WorkbenchSupergraphic().frame(width: 150, height: 72).opacity(0.08).clipShape(RoundedRectangle(cornerRadius: 16))
            }
        )
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(RnDTheme.line))
        .shadow(color: RnDTheme.navy.opacity(0.05), radius: 10, y: 4)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Что требует внимания")
    }

    private var header: some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().fill(events.isEmpty ? RnDTheme.blue.opacity(0.12) : RnDTheme.red.opacity(0.10))
                Image(systemName: events.isEmpty ? "checkmark" : "scope")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(events.isEmpty ? RnDTheme.blue : RnDTheme.red)
            }
            .frame(width: 32, height: 32)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Что требует внимания").font(.headline)
                Text(events.isEmpty ? "Приоритетных сигналов нет" : "Ранжировано по срочности и влиянию")
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            if !events.isEmpty {
                Text("\(events.count)")
                    .font(.caption.bold().monospacedDigit())
                    .foregroundStyle(RnDTheme.red)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Capsule().fill(RnDTheme.red.opacity(0.08)))
                    .accessibilityLabel("Всего приоритетов: \(events.count)")
            }
            Button(action: onExplain) {
                HStack(spacing: 4) {
                    Image(systemName: "text.bubble")
                    if !compact { Text("Объяснить приоритеты") }
                }
            }
            .buttonStyle(.borderless)
            .font(.caption.weight(.medium))
            .help("Объяснить, почему события получили такой приоритет")
            .accessibilityLabel("Объяснить приоритеты")
            if let onShowAll {
                Button(action: onShowAll) { Image(systemName: "tray.full") }
                    .buttonStyle(.borderless)
                    .help("Открыть все уведомления")
                    .accessibilityLabel("Открыть все уведомления")
            }
        }
    }

    private var calmState: some View {
        HStack(spacing: 10) {
            Image(systemName: "shield.checkered").font(.title2).foregroundStyle(RnDTheme.blue)
            VStack(alignment: .leading, spacing: 2) {
                Text("Всё спокойно").font(.callout.weight(.semibold))
                Text("Срочных решений, просрочек и ошибок сейчас нет.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: compact ? 82 : 96, alignment: .leading)
        .padding(.horizontal, 8)
        .accessibilityElement(children: .combine)
    }
}

struct AttentionPriorityRow: View {
    let rank: Int
    let event: AttentionEventRecord
    let compact: Bool
    let onOpenSource: (EntityRecord) -> Void

    private var severityColor: Color {
        switch event.severity.lowercased() {
        case "critical", "high", "error": return RnDTheme.red
        case "medium", "warning": return Color(red: 0.89, green: 0.42, blue: 0.08)
        default: return RnDTheme.blue
        }
    }

    private var symbol: String {
        switch event.kind.lowercased() {
        case "overdue", "deadline", "due", "meeting_action", "meeting_commitment": return "calendar.badge.exclamationmark"
        case "decision", "decision_changed", "meeting_decision": return "arrow.triangle.branch"
        case "risk", "meeting_risk", "error", "inbox_error", "automation", "automation_error": return "exclamationmark.triangle.fill"
        case "question", "meeting_question", "needs_user", "approval": return "person.crop.circle.badge.questionmark"
        case "task_done", "result": return "checkmark.seal.fill"
        case "task": return "checklist"
        default: return "sparkles"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: compact ? 8 : 11) {
            ZStack {
                Circle().fill(severityColor.opacity(0.11))
                Text("\(rank)").font(.caption.bold().monospacedDigit()).foregroundStyle(severityColor)
            }
            .frame(width: compact ? 26 : 30, height: compact ? 26 : 30)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: symbol).font(.caption).foregroundStyle(severityColor).accessibilityHidden(true)
                    Text(event.title).font(.callout.weight(.semibold)).lineLimit(2).layoutPriority(1)
                    Spacer(minLength: 2)
                    if !event.dueAt.isEmpty {
                        Label(shortDueDate(event.dueAt), systemImage: "clock")
                            .font(.caption2.weight(.medium)).foregroundStyle(severityColor).labelStyle(.titleAndIcon)
                            .accessibilityLabel("Срок: \(event.dueAt)")
                    }
                }
                Text(event.reason.isEmpty ? "Сигнал требует проверки" : event.reason)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(compact ? 2 : 3)
                if !compact || event.sourceRecord != nil || !event.actionLabel.isEmpty {
                    HStack(spacing: 8) {
                        PriorityMeter(score: event.score, color: severityColor)
                        Spacer(minLength: 0)
                        if let source = event.sourceRecord {
                            Button {
                                onOpenSource(source)
                            } label: {
                                Label(event.actionLabel.isEmpty ? "Источник" : event.actionLabel, systemImage: "arrow.up.forward.square")
                            }
                            .buttonStyle(.borderless)
                            .font(.caption2.weight(.medium))
                            .fixedSize(horizontal: true, vertical: false)
                            .help("Открыть локальный источник")
                            .accessibilityLabel("Открыть источник для приоритета «\(event.title)»")
                        } else if !event.actionLabel.isEmpty {
                            Text(event.actionLabel).font(.caption2.weight(.medium)).foregroundStyle(RnDTheme.blue)
                        }
                    }
                }
            }
            .layoutPriority(1)
        }
        .padding(compact ? 9 : 11)
        .background(
            RoundedRectangle(cornerRadius: 11)
                .fill(LinearGradient(colors: [severityColor.opacity(0.055), RnDTheme.canvas.opacity(0.52)], startPoint: .leading, endPoint: .trailing))
        )
        .overlay(alignment: .leading) {
            Capsule().fill(severityColor).frame(width: 3).padding(.vertical, 8)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Приоритет \(rank): \(event.title). \(event.reason)")
        .accessibilityValue("Оценка приоритета \(Int(event.score.rounded()))")
    }

    private func shortDueDate(_ value: String) -> String {
        let datePart = value.prefix(10)
        guard datePart.count == 10 else { return value }
        let pieces = datePart.split(separator: "-")
        guard pieces.count == 3 else { return value }
        return "\(pieces[2]).\(pieces[1])"
    }
}

struct PriorityMeter: View {
    let score: Double
    let color: Color

    private var normalizedScore: Double { min(max(score / 100, 0.08), 1) }

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(RnDTheme.line)
                Capsule().fill(color.opacity(0.82)).frame(width: proxy.size.width * normalizedScore)
            }
        }
        .frame(width: 54, height: 3)
        .accessibilityHidden(true)
    }
}

struct FocusHero: View {
    @ObservedObject var controller: BackendController
    let action: () -> Void
    var body: some View {
        ZStack {
            LinearGradient(colors: [RnDTheme.navy, RnDTheme.blue], startPoint: .topLeading, endPoint: .bottomTrailing)
            WorkbenchSupergraphic().opacity(0.46)
            HStack(spacing: 22) {
                VStack(alignment: .leading, spacing: 8) {
                    Label(
                        controller.configuredLLMModeLabel,
                        systemImage: controller.isAutoLLMActive
                            ? "arrow.triangle.branch"
                            : controller.isExternalLLMActive ? "network" : "lock.shield.fill"
                    )
                        .font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.76))
                    Text(controller.currentTask?.title ?? "Чем займёмся сегодня?")
                        .font(.system(size: 23, weight: .bold, design: .rounded))
                        .foregroundStyle(.white).lineLimit(2)
                    Text(controller.currentTask == nil ? "Поставьте задачу голосом или текстом — контекст подберётся автоматически." : "Продолжите текущую задачу с сохранённой историей и источниками.")
                        .font(.callout).foregroundStyle(.white.opacity(0.78)).lineLimit(2)
                    Button(controller.currentTask == nil ? "Начать задачу" : "Продолжить") { action() }
                        .buttonStyle(.borderedProminent).tint(.white).foregroundStyle(RnDTheme.navy)
                }
                Spacer(minLength: 12)
                VStack(spacing: 10) {
                    HStack(spacing: 8) {
                        PipelineNode(icon: "mic.fill", label: "Whisper", ready: controller.loadedModels.contains("Whisper"))
                        Image(systemName: "chevron.right").foregroundStyle(.white.opacity(0.5))
                        PipelineNode(
                            icon: controller.actualLLMRouteIcon,
                            label: llmLabel,
                            ready: llmReady,
                            accent: controller.isRemoteRouteActive ? RnDTheme.red : .white
                        )
                        Image(systemName: "chevron.right").foregroundStyle(.white.opacity(0.5))
                        PipelineNode(icon: "speaker.wave.2.fill", label: "OmniVoice", ready: controller.loadedModels.contains("TTS"))
                    }
                    Text(pipelineStatus)
                        .font(.caption2).foregroundStyle(.white.opacity(0.7))
                }
            }.padding(22)
        }
        .frame(minHeight: 176)
        .clipShape(RoundedRectangle(cornerRadius: 20))
        .overlay(RoundedRectangle(cornerRadius: 20).stroke(.white.opacity(0.12)))
        .overlay(alignment: .leading) {
            if controller.isRemoteRouteActive {
                Capsule().fill(RnDTheme.red).frame(width: 4).padding(.vertical, 18)
            }
        }
        .shadow(color: RnDTheme.navy.opacity(0.18), radius: 16, y: 8)
    }

    private var llmLabel: String {
        controller.actualLLMModel.isEmpty
            ? controller.actualLLMRouteLabel
            : controller.actualLLMModel
    }

    private var llmReady: Bool {
        if controller.isExternalLLMActive {
            return controller.externalLLMReady && !controller.llmConfigurationPending
        }
        return controller.loadedModels.contains { $0.hasPrefix("LLM") }
    }

    private var pipelineStatus: String {
        if controller.isAutoLLMActive {
            return controller.routingFallbackMessage
                ?? "Авто · фактически: \(controller.actualLLMRouteLabel) · STT и TTS локально"
        }
        if controller.isExternalLLMActive {
            if controller.llmConfigurationPending { return "Настраиваю внешнюю LLM · STT и TTS локально" }
            if controller.externalLLMReady { return "STT и TTS локально · контекст у провайдера" }
            return "Внешняя LLM требует настройки · STT и TTS локально"
        }
        return controller.isReady ? "Все локальные модели готовы" : "Загрузка локальных моделей"
    }
}

struct PipelineNode: View {
    let icon: String
    let label: String
    let ready: Bool
    var accent: Color = .white
    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: icon).font(.system(size: 19, weight: .semibold))
                .frame(width: 42, height: 42)
                .background(Circle().fill(.white.opacity(ready ? 0.20 : 0.09)))
                .overlay(Circle().stroke(accent.opacity(0.78), lineWidth: 1.5))
                .overlay(alignment: .topTrailing) { Circle().fill(ready ? Color.white : RnDTheme.red).frame(width: 7, height: 7) }
            Text(label)
                .font(.caption2.weight(.medium))
                .lineLimit(2)
                .minimumScaleFactor(0.65)
                .multilineTextAlignment(.center)
                .frame(width: 68)
        }.foregroundStyle(.white)
    }
}

private enum WorkspaceTimelineFilter: String, CaseIterable, Identifiable {
    case all, tasks, meetings, decisions, sources, artifacts, approvals

    var id: String { rawValue }
    var title: String {
        switch self {
        case .all: return "Все"
        case .tasks: return "Задачи"
        case .meetings: return "Встречи"
        case .decisions: return "Решения"
        case .sources: return "Источники"
        case .artifacts: return "Материалы"
        case .approvals: return "Согласования"
        }
    }

    func includes(_ item: WorkspaceTimelineRecord) -> Bool {
        switch self {
        case .all: return true
        case .tasks: return item.type == "task" || item.type == "task_event"
        case .meetings: return item.type == "meeting"
        case .decisions: return item.type == "decision"
        case .sources: return item.type == "source"
        case .artifacts: return item.type == "artifact"
        case .approvals: return item.type == "approval"
        }
    }
}

struct WorkspaceTimelineSection: View {
    @ObservedObject var controller: BackendController
    @State private var filter: WorkspaceTimelineFilter = .all

    private var filtered: [WorkspaceTimelineRecord] {
        controller.workspaceTimeline.filter(filter.includes)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Хронология").font(.headline)
                    Text("Задачи, встречи, решения, источники и результаты в одном порядке")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text("\(filtered.count)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(WorkspaceTimelineFilter.allCases) { candidate in
                        Button(candidate.title) { filter = candidate }
                            .buttonStyle(.plain)
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .foregroundStyle(filter == candidate ? Color.white : RnDTheme.ink)
                            .background(Capsule().fill(filter == candidate ? RnDTheme.blue : RnDTheme.canvas))
                            .overlay(Capsule().stroke(filter == candidate ? RnDTheme.blue : RnDTheme.line))
                    }
                }
            }

            if filtered.isEmpty {
                EmptyState(
                    icon: "clock.arrow.circlepath",
                    title: "Хронология пока пуста",
                    detail: filter == .all
                        ? "События появятся после создания задач, встреч или материалов."
                        : "Для выбранного типа событий записей пока нет."
                )
            } else {
                LazyVStack(spacing: 0) {
                    ForEach(Array(filtered.prefix(80).enumerated()), id: \.element.id) { index, item in
                        WorkspaceTimelineRow(
                            item: item,
                            isLast: index == min(filtered.count, 80) - 1,
                            onOpen: { controller.openWorkspaceTimelineItem(item) },
                            onOpenSource: { controller.openWorkspaceTimelineSource(item) }
                        )
                    }
                }
                if filtered.count > 80 {
                    Text("Показаны последние 80 из \(filtered.count) событий")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 16).fill(RnDTheme.panel))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(RnDTheme.line))
    }
}

struct WorkspaceTimelineRow: View {
    let item: WorkspaceTimelineRecord
    let isLast: Bool
    let onOpen: () -> Void
    let onOpenSource: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            VStack(spacing: 0) {
                ZStack {
                    Circle().fill(color.opacity(0.12))
                    Image(systemName: icon).font(.caption.weight(.semibold)).foregroundStyle(color)
                }
                .frame(width: 30, height: 30)
                if !isLast {
                    Rectangle()
                        .fill(RnDTheme.line)
                        .frame(width: 1)
                        .frame(minHeight: 54)
                }
            }

            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Text(item.title).font(.callout.weight(.semibold)).lineLimit(2)
                    Spacer(minLength: 4)
                    Text(displayMeetingDate(item.timestamp))
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if !item.detail.isEmpty {
                    Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                }
                if item.type == "decision" {
                    decisionHistory
                }
                HStack(spacing: 8) {
                    Text(typeTitle).font(.caption2.weight(.medium)).foregroundStyle(color)
                    if !item.status.isEmpty {
                        Text(item.status).font(.caption2).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    if item.sourceRecord != nil && item.type != "source" {
                        Button(action: onOpenSource) {
                            Label(
                                item.sourceStart != nil ? "Фрагмент" : "Источник",
                                systemImage: item.sourceStart != nil ? "selection.pin.in.out" : "doc.text"
                            )
                        }
                        .buttonStyle(.borderless)
                        .font(.caption2)
                    }
                    Button(action: onOpen) { Image(systemName: "chevron.right") }
                        .buttonStyle(.borderless)
                        .help("Открыть связанный объект")
                        .accessibilityLabel("Открыть: \(item.title)")
                }
            }
            .padding(.bottom, isLast ? 0 : 12)
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var decisionHistory: some View {
        if let sequence = item.decisionSequence, let count = item.decisionCount {
            HStack(spacing: 6) {
                Text("Версия решения \(sequence) из \(count)")
                    .font(.caption2.weight(.medium))
                if item.isCurrentDecision {
                    Text("Текущее")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(RnDTheme.blue)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(RnDTheme.blue.opacity(0.10)))
                }
            }
            if !item.isCurrentDecision && !item.currentDecisionText.isEmpty {
                Text("Текущее решение: \(item.currentDecisionText)")
                    .font(.caption2)
                    .foregroundStyle(RnDTheme.blue)
                    .lineLimit(2)
            }
        }
    }

    private var typeTitle: String {
        switch item.type {
        case "task", "task_event": return "Задача"
        case "meeting": return "Встреча"
        case "decision": return "Решение"
        case "source": return "Источник"
        case "artifact": return "Материал"
        case "approval": return "Согласование"
        default: return item.type
        }
    }

    private var icon: String {
        switch item.type {
        case "task": return "checklist"
        case "task_event": return "clock.badge.checkmark"
        case "meeting": return "person.2.fill"
        case "decision": return "arrow.triangle.branch"
        case "source": return "doc.text.fill"
        case "artifact": return "doc.richtext.fill"
        case "approval": return "checkmark.shield.fill"
        default: return "circle.fill"
        }
    }

    private var color: Color {
        switch item.type {
        case "decision": return RnDTheme.red
        case "meeting": return RnDTheme.navy
        case "artifact": return RnDTheme.steel
        case "approval": return Color(red: 0.89, green: 0.42, blue: 0.08)
        default: return RnDTheme.blue
        }
    }
}

struct WorkspacesView: View {
    @ObservedObject var controller: BackendController
    @State private var showCreate = false
    @State private var name = ""
    @State private var description = ""
    @State private var showWorkspaceEdit = false
    @State private var showMemoryEdit = false
    @State private var editingMemoryID: String?
    @State private var memoryTitle = ""
    @State private var memoryContent = ""
    @State private var memoryKind = "note"
    @State private var sourcePendingDeletion: EntityRecord?
    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                HStack { Text("Пространства").font(.headline); Spacer(); Button { showCreate = true } label: { Image(systemName: "plus") } }.padding(.horizontal, 12).padding(.top, 12)
                List(controller.workspaces, selection: Binding(get: { controller.currentWorkspaceID }, set: { controller.selectWorkspace($0) })) { workspace in
                    VStack(alignment: .leading, spacing: 4) { Text(workspace.name).font(.body.weight(.medium)); Text(workspace.description.isEmpty ? "Без описания" : workspace.description).font(.caption).foregroundStyle(.secondary).lineLimit(2); ClassificationBadge(value: workspace.classification) }.tag(workspace.id)
                }.listStyle(.sidebar)
            }.frame(minWidth: 230, idealWidth: 250)
            ScrollView { VStack(alignment: .leading, spacing: 18) {
                HStack { VStack(alignment: .leading) { Text(controller.currentWorkspace?.name ?? "Рабочее пространство").font(.title2.bold()); Text(controller.currentWorkspace?.description ?? "").foregroundStyle(.secondary) }; Spacer(); Button { if let workspace = controller.currentWorkspace { name = workspace.name; description = workspace.description; showWorkspaceEdit = true } } label: { Label("Изменить", systemImage: "pencil") }; Button { controller.chooseFile() } label: { Label("Документ", systemImage: "paperclip") }; Button { controller.chooseFile(kind: "meeting") } label: { Label("Транскрипт", systemImage: "person.2.wave.2") } }
                WorkspaceTimelineSection(controller: controller)
                HStack { Text("Источники").font(.headline); Spacer(); Text("\(controller.sources.count)").foregroundStyle(.secondary) }
                if controller.sources.isEmpty { EmptyState(icon: "doc.badge.plus", title: "Добавьте контекст", detail: "Поддерживаются Markdown, TXT, CSV, JSON, DOCX и PDF при наличии pypdf.") }
                else {
                    ForEach(controller.sources) { item in
                        EntityRow(
                            item: item,
                            icon: item.subtitle == "meeting" ? "person.2.fill" : "doc.text.fill",
                            trailing: AnyView(HStack(spacing: 8) {
                                ClassificationMenu(value: item.classification) {
                                    controller.setClassification(entityType: "source", id: item.id, value: $0)
                                }
                                Button { controller.openSource(item) } label: { Image(systemName: "arrow.up.forward.square") }
                                    .buttonStyle(.borderless)
                                    .help("Открыть источник «\(item.title)»")
                                    .accessibilityLabel("Открыть источник «\(item.title)»")
                                Button(role: .destructive) { sourcePendingDeletion = item } label: { Image(systemName: "trash") }
                                    .buttonStyle(.borderless)
                                    .foregroundStyle(RnDTheme.red)
                                    .disabled(!controller.canDeleteEntities)
                                    .help("Удалить источник «\(item.title)»")
                                    .accessibilityLabel("Удалить источник «\(item.title)»")
                            }))
                            .contextMenu {
                                Button("Открыть") { controller.openSource(item) }
                                Menu("Классификация") {
                                    ForEach(DataClassification.allCases) { classification in
                                        Button(classification.title) {
                                            controller.setClassification(entityType: "source", id: item.id, value: classification.rawValue)
                                        }
                                    }
                                }
                                Divider()
                                Button("Удалить источник…", role: .destructive) { sourcePendingDeletion = item }
                                    .disabled(!controller.canDeleteEntities)
                            }
                    }
                }
                HStack { Text("Рабочая память").font(.headline); Spacer(); Button { editingMemoryID = nil; memoryTitle = ""; memoryContent = ""; memoryKind = "note"; showMemoryEdit = true } label: { Label("Добавить", systemImage: "plus") } }
                ForEach(controller.memory) { item in EntityRow(item: item, icon: "brain.head.profile", trailing: AnyView(HStack { ClassificationMenu(value: item.classification) { controller.setClassification(entityType: "memory", id: item.id, value: $0) }; Button { editingMemoryID = item.id; memoryTitle = item.title; memoryContent = item.detail; memoryKind = item.kind.isEmpty ? "note" : item.kind; showMemoryEdit = true } label: { Image(systemName: "pencil") }.buttonStyle(.borderless); Button(role: .destructive) { controller.deleteMemory(item.id) } label: { Image(systemName: "trash") }.buttonStyle(.borderless) })) }
                if controller.currentWorkspaceID != "personal" { Divider(); Button("Архивировать рабочее пространство", role: .destructive) { controller.archiveCurrentWorkspace() } }
            }.padding(22) }.frame(minWidth: 520)
        }
        .sheet(isPresented: $showCreate) {
            FormSheet(title: "Новое рабочее пространство", primary: "Создать", onCancel: { showCreate = false }, onPrimary: { controller.createWorkspace(name: name, description: description); name = ""; description = ""; showCreate = false }) { TextField("Название", text: $name); TextField("Описание", text: $description) }
        }
        .sheet(isPresented: $showWorkspaceEdit) {
            FormSheet(title: "Изменить рабочее пространство", primary: "Сохранить", onCancel: { showWorkspaceEdit = false }, onPrimary: { controller.updateWorkspace(name: name, description: description); showWorkspaceEdit = false }) { TextField("Название", text: $name); TextField("Описание", text: $description) }
        }
        .sheet(isPresented: $showMemoryEdit) {
            FormSheet(title: editingMemoryID == nil ? "Новая запись памяти" : "Изменить память", primary: "Сохранить", onCancel: { showMemoryEdit = false }, onPrimary: { if let id = editingMemoryID { controller.updateMemory(id: id, title: memoryTitle, content: memoryContent, kind: memoryKind) } else { controller.saveMemory(title: memoryTitle, content: memoryContent, kind: memoryKind) }; showMemoryEdit = false }) {
                TextField("Название", text: $memoryTitle)
                Picker("Тип памяти", selection: $memoryKind) {
                    Text("Рабочая заметка").tag("note")
                    Text("Предпочтение").tag("preference")
                    Text("Факт").tag("fact")
                    Text("Обязательство").tag("commitment")
                    Text("Явно сохранённое").tag("explicit")
                    Text("Результат задачи").tag("task_result")
                }
                TextEditor(text: $memoryContent).frame(height: 150).overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            }
        }
        .confirmationDialog(
            "Удалить источник «\(sourcePendingDeletion?.title ?? "")»?",
            isPresented: Binding(
                get: { sourcePendingDeletion != nil },
                set: { if !$0 { sourcePendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Удалить источник", role: .destructive) {
                if let source = sourcePendingDeletion { controller.deleteSource(source.id) }
                sourcePendingDeletion = nil
            }
            .disabled(!controller.canDeleteEntities)
            Button("Отмена", role: .cancel) { sourcePendingDeletion = nil }
        } message: {
            Text("Источник исчезнет из контекста и поиска. Управляемая копия будет перемещена в Корзину; исходный импортированный файл не изменится.")
        }
    }
}

struct TasksView: View {
    @ObservedObject var controller: BackendController
    @State private var showPlanEdit = false
    @State private var planText = ""
    @State private var taskPendingDeletion: TaskRecord?
    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                HStack { Text("Задачи").font(.headline); Spacer(); Button { controller.newTask() } label: { Image(systemName: "plus") } }.padding(.horizontal, 12).padding(.top, 12)
                List(controller.tasks, selection: Binding(get: { controller.currentTaskID }, set: { if let id = $0 { controller.selectTask(id) } })) { task in
                    TaskRow(task: task) { controller.selectTask(task.id) }
                        .tag(task.id)
                        .contextMenu {
                            Button("Открыть") { controller.selectTask(task.id) }
                            Divider()
                            Button("Удалить задачу…", role: .destructive) { taskPendingDeletion = task }
                                .disabled(!controller.canDeleteEntities)
                        }
                }
                .listStyle(.sidebar)
            }.frame(minWidth: 250, idealWidth: 280)
            VStack(spacing: 0) {
                if let task = controller.currentTask {
                    VStack(spacing: 12) {
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 5) { Text(task.title).font(.headline).lineLimit(2); HStack { StatusBadge(status: task.status); ClassificationBadge(value: task.classification); if let skill = task.skillID { Label(skill, systemImage: "wand.and.stars").font(.caption).foregroundStyle(.secondary) } } }
                            Spacer()
                            ClassificationMenu(value: task.classification) {
                                controller.setClassification(entityType: "task", id: task.id, value: $0)
                            }
                            Button { planText = task.plan.joined(separator: "\n"); showPlanEdit = true } label: { Label("Изменить план", systemImage: "pencil") }.buttonStyle(.borderless)
                            Button(role: .destructive) { taskPendingDeletion = task } label: { Image(systemName: "trash") }
                                .buttonStyle(.borderless)
                                .foregroundStyle(RnDTheme.red)
                                .fixedSize(horizontal: true, vertical: false)
                                .disabled(!controller.canDeleteEntities)
                                .help("Удалить задачу «\(task.title)»")
                                .accessibilityLabel("Удалить задачу «\(task.title)»")
                        }
                        TaskPlanStrip(task: task)
                    }.padding(15)
                    Divider().opacity(0.35); ConversationView(
                        messages: controller.messages,
                        sources: controller.activeSources,
                        quickActions: controller.quickActions,
                        quickActionPendingID: controller.quickActionPendingID,
                        isQuickActionCompleted: controller.isQuickActionCompleted,
                        onQuickAction: controller.performQuickAction,
                        onOpenSource: controller.openSource
                    )
                    if !controller.taskEvents.isEmpty { DisclosureGroup("Журнал действий · \(controller.taskEvents.count)") { ForEach(controller.taskEvents.prefix(8)) { item in EntityRow(item: item, icon: "clock") } }.padding(12).background(RnDTheme.panel) }
                } else { EmptyState(icon: "checklist", title: "Выберите задачу", detail: "История и контекст каждой задачи хранятся отдельно.") }
            }.frame(minWidth: 500)
        }
        .sheet(isPresented: $showPlanEdit) {
            FormSheet(title: "План задачи", primary: "Сохранить", onCancel: { showPlanEdit = false }, onPrimary: { if let task = controller.currentTask { let steps = planText.split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }; controller.updateTaskPlan(task.id, plan: steps) }; showPlanEdit = false }) { Text("Один этап на строку").font(.caption).foregroundStyle(.secondary); TextEditor(text: $planText).frame(height: 180).overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary)) }
        }
        .confirmationDialog(
            "Удалить задачу «\(taskPendingDeletion?.title ?? "")»?",
            isPresented: Binding(
                get: { taskPendingDeletion != nil },
                set: { if !$0 { taskPendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Удалить задачу", role: .destructive) {
                if let task = taskPendingDeletion { controller.deleteTask(task.id) }
                taskPendingDeletion = nil
            }
            .disabled(!controller.canDeleteEntities)
            Button("Отмена", role: .cancel) { taskPendingDeletion = nil }
        } message: {
            Text("История, план, подтверждения и прикреплённые только к этой задаче источники будут удалены. Созданные материалы сохранятся.")
        }
    }
}

struct TaskPlanStrip: View {
    let task: TaskRecord
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(Array(task.plan.enumerated()), id: \.offset) { index, step in
                    HStack(spacing: 6) {
                        VStack(alignment: .leading, spacing: 5) {
                            ZStack {
                                Circle().fill(task.status == "done" ? RnDTheme.blue : RnDTheme.canvas).frame(width: 26, height: 26)
                                if task.status == "done" { Image(systemName: "checkmark").font(.caption.bold()).foregroundStyle(.white) }
                                else { Text("\(index + 1)").font(.caption2.bold()).foregroundStyle(RnDTheme.navy) }
                            }
                            Text(step).font(.caption).foregroundStyle(RnDTheme.ink).lineLimit(2).frame(width: 108, alignment: .leading)
                        }
                        if index < task.plan.count - 1 { Rectangle().fill(RnDTheme.line).frame(width: 20, height: 1).offset(y: -10) }
                    }
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(RnDTheme.canvas))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(RnDTheme.line))
    }
}

private enum MeetingPeriod: String, CaseIterable, Identifiable {
    case all, week, month, quarter
    var id: String { rawValue }
    var title: String {
        switch self {
        case .all: return "За всё время"
        case .week: return "7 дней"
        case .month: return "30 дней"
        case .quarter: return "90 дней"
        }
    }
    var days: Int? {
        switch self {
        case .all: return nil
        case .week: return 7
        case .month: return 30
        case .quarter: return 90
        }
    }
}

private enum MeetingKindFilter: String, CaseIterable, Identifiable {
    case all, decision, action, commitment, risk, question
    var id: String { rawValue }
    var title: String {
        switch self {
        case .all: return "Все"
        case .decision: return "Решения"
        case .action: return "Поручения"
        case .commitment: return "Обещания"
        case .risk: return "Риски"
        case .question: return "Вопросы"
        }
    }

    func matches(_ rawKind: String) -> Bool {
        guard self != .all else { return true }
        let value = rawKind.lowercased()
        switch self {
        case .all: return true
        case .decision: return ["decision", "decisions", "решение", "решения"].contains(value)
        case .action: return ["action", "actions", "task", "поручение", "поручения"].contains(value)
        case .commitment: return ["commitment", "commitments", "promise", "обещание", "обещания"].contains(value)
        case .risk: return ["risk", "risks", "риск", "риски"].contains(value)
        case .question: return ["question", "questions", "вопрос", "вопросы"].contains(value)
        }
    }
}

struct MeetingsView: View {
    @ObservedObject var controller: BackendController
    @State private var selectedKind: MeetingKindFilter = .all
    @State private var selectedPerson = ""
    @State private var period: MeetingPeriod = .all
    @State private var comparisonMeetingID = ""
    @State private var meetingPendingDeletion: MeetingRecord?

    private var people: [String] {
        Array(Set(controller.meetings.flatMap(\.participants))).sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }

    private var visibleMeetings: [MeetingRecord] {
        controller.meetings.filter { meeting in
            let personMatches = selectedPerson.isEmpty || meeting.participants.contains { $0.localizedCaseInsensitiveCompare(selectedPerson) == .orderedSame }
            let periodMatches: Bool
            if let days = period.days, let date = meetingDate(meeting.occurredAt) {
                periodMatches = date >= Calendar.current.date(byAdding: .day, value: -days, to: Date()) ?? .distantPast
            } else {
                periodMatches = period.days == nil || meetingDate(meeting.occurredAt) == nil
            }
            return personMatches && periodMatches
        }
    }

    private var visibleItems: [MeetingItemRecord] {
        controller.meetingItems.filter { item in
            selectedKind.matches(item.kind) && (selectedPerson.isEmpty || item.owner.localizedCaseInsensitiveCompare(selectedPerson) == .orderedSame)
        }
    }

    private var comparisonMeetings: [MeetingRecord] {
        controller.meetings.filter { $0.id != controller.currentMeetingID }
    }

    var body: some View {
        HSplitView {
            VStack(spacing: 0) {
                meetingFilters
                Divider().opacity(0.35)
                if visibleMeetings.isEmpty {
                    EmptyState(icon: "person.2.slash", title: "Встречи не найдены", detail: controller.meetings.isEmpty ? "Добавьте аудиозапись, готовый транскрипт или папку/ZIP встречи из eXpress (Синапс) — распознавание и анализ выполняются локально." : "Измените фильтр по человеку или периоду.")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(visibleMeetings, selection: Binding(get: { controller.currentMeetingID }, set: { if let id = $0 { controller.selectMeeting(id) } })) { meeting in
                        MeetingListRow(meeting: meeting)
                            .tag(meeting.id)
                            .contextMenu {
                                Button("Открыть") { controller.selectMeeting(meeting.id) }
                                Divider()
                                Button("Удалить встречу…", role: .destructive) { meetingPendingDeletion = meeting }
                                    .disabled(meeting.sourceID.isEmpty || !controller.canDeleteEntities)
                            }
                    }
                    .listStyle(.sidebar)
                }
            }
            .frame(minWidth: 285, idealWidth: 315, maxWidth: 365)

            Group {
                if let meeting = controller.currentMeeting {
                    meetingDetail(meeting)
                } else {
                    EmptyState(icon: "person.2.wave.2", title: "Выберите встречу", detail: "Откройте структурированную карточку с решениями, поручениями и точными цитатами.")
                }
            }
            .frame(minWidth: 530, maxWidth: .infinity, maxHeight: .infinity)
        }
        .onChange(of: controller.currentMeetingID) { _, _ in comparisonMeetingID = "" }
        .onChange(of: controller.meetings.map(\.id)) { _, ids in
            if !ids.contains(comparisonMeetingID) { comparisonMeetingID = "" }
        }
        .confirmationDialog(
            "Удалить встречу «\(meetingPendingDeletion?.title ?? "")»?",
            isPresented: Binding(
                get: { meetingPendingDeletion != nil },
                set: { if !$0 { meetingPendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Удалить встречу", role: .destructive) {
                if let meeting = meetingPendingDeletion, !meeting.sourceID.isEmpty {
                    controller.deleteSource(meeting.sourceID)
                }
                meetingPendingDeletion = nil
            }
            .disabled(!controller.canDeleteEntities)
            Button("Отмена", role: .cancel) { meetingPendingDeletion = nil }
        } message: {
            Text("Карточка встречи, транскрипт и результаты анализа будут удалены. Управляемые копии аудио и транскрипта, если они есть, будут перемещены в Корзину; исходный файл не изменится.")
        }
    }

    private var meetingFilters: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Встречи").font(.headline)
                Spacer()
            }
            HStack(spacing: 8) {
                Menu {
                    Button(action: controller.chooseMeetingAudio) {
                        Label("Аудиозапись", systemImage: "waveform.badge.plus")
                    }
                    Button { controller.chooseFile(kind: "meeting") } label: {
                        Label("Готовый транскрипт", systemImage: "doc.badge.plus")
                    }
                    Divider()
                    Button(action: controller.chooseSynapseMeetingPackage) {
                        Label("Папка или ZIP eXpress (Синапс)", systemImage: "shippingbox.and.arrow.backward")
                    }
                    if controller.expressConnectorConfigured {
                        Divider()
                        Button(action: controller.syncExpressMeetings) {
                            Label(
                                controller.expressSyncInProgress
                                    ? "Синхронизирую eXpress…"
                                    : "Получить новые встречи eXpress",
                                systemImage: "arrow.triangle.2.circlepath"
                            )
                        }
                        .disabled(controller.expressSyncInProgress)
                    }
                } label: {
                    Label("Добавить встречу", systemImage: "plus.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .tint(RnDTheme.navy)
                .fixedSize(horizontal: true, vertical: false)
                .disabled(!controller.canImportMeetingAudio || controller.expressSyncInProgress)
                .help("Добавить аудио, транскрипт или локальную папку/ZIP встречи из eXpress (Синапс)")
            }
            .controlSize(.small)
            if let stage = controller.meetingAudioImportStage {
                HStack(spacing: 7) {
                    if controller.meetingAudioImportInProgress {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(RnDTheme.blue)
                    }
                    Text(stage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            HStack(spacing: 8) {
                Picker("Человек", selection: $selectedPerson) {
                    Text("Все участники").tag("")
                    ForEach(people, id: \.self) { Text($0).tag($0) }
                }
                .labelsHidden()
                .accessibilityLabel("Фильтр встреч по человеку")
                Picker("Период", selection: $period) {
                    ForEach(MeetingPeriod.allCases) { Text($0.title).tag($0) }
                }
                .labelsHidden()
                .accessibilityLabel("Фильтр встреч по периоду")
            }
        }
        .padding(12)
        .background(RnDTheme.panel)
    }

    private func meetingDetail(_ meeting: MeetingRecord) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 15) {
                meetingHeader(meeting)

                if !meeting.summary.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        Label("Кратко", systemImage: "sparkles").font(.headline)
                        Text(meeting.summary).font(.body).textSelection(.enabled)
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 14).fill(RnDTheme.panel))
                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(RnDTheme.line))
                }

                if let briefing = controller.meetingBriefing {
                    MeetingBriefingCard(text: briefing)
                }

                if !controller.meetingDiff.isEmpty {
                    MeetingDiffView(items: controller.meetingDiff)
                }

                Picker("Тип результата", selection: $selectedKind) {
                    ForEach(MeetingKindFilter.allCases) { Text($0.title).tag($0) }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .accessibilityLabel("Фильтр результатов встречи")

                if visibleItems.isEmpty {
                    EmptyState(icon: "line.3.horizontal.decrease.circle", title: "Нет результатов по фильтру", detail: "Выберите другой тип или запустите повторный анализ встречи.")
                } else {
                    ForEach(visibleItems) { item in
                        MeetingItemCard(item: item, sourcePath: meeting.sourcePath, onStatus: controller.setMeetingItemStatus, onOpenSource: controller.openSource)
                    }
                }
            }
            .padding(18)
        }
    }

    private func meetingHeader(_ meeting: MeetingRecord) -> some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack(alignment: .top, spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 13).fill(RnDTheme.navy)
                    Image(systemName: "person.2.wave.2.fill").font(.title2).foregroundStyle(.white)
                }.frame(width: 48, height: 48).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(meeting.title).font(.title2.bold()).lineLimit(2)
                    HStack(spacing: 9) {
                        Label(displayMeetingDate(meeting.occurredAt), systemImage: "calendar")
                        if !meeting.participants.isEmpty { Label(meeting.participants.joined(separator: ", "), systemImage: "person.2") }
                    }.font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                StatusBadge(status: meeting.status)
                Button(role: .destructive) { meetingPendingDeletion = meeting } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless)
                    .foregroundStyle(RnDTheme.red)
                    .fixedSize(horizontal: true, vertical: false)
                    .disabled(meeting.sourceID.isEmpty || !controller.canDeleteEntities)
                    .help(meeting.sourceID.isEmpty ? "Источник встречи недоступен" : "Удалить встречу «\(meeting.title)»")
                    .accessibilityLabel("Удалить встречу «\(meeting.title)»")
            }
            HStack(spacing: 8) {
                Button { controller.reanalyzeMeeting(meeting.id) } label: { Label("Проанализировать снова", systemImage: "arrow.clockwise") }
                    .help("Повторно извлечь решения, поручения, обещания, риски и вопросы")
                Button { controller.prepareBriefing(meeting.id) } label: { Label("Подготовить брифинг", systemImage: "doc.text.magnifyingglass") }
                    .buttonStyle(.borderedProminent)
                    .help("Подготовить материалы к следующей встрече")
                Spacer()
                Picker("Вторая встреча", selection: $comparisonMeetingID) {
                    Text("Выберите встречу").tag("")
                    ForEach(comparisonMeetings) { Text($0.title).tag($0.id) }
                }
                .frame(maxWidth: 190)
                .accessibilityLabel("Вторая встреча для сравнения")
                Button { controller.compareMeetings(meeting.id, with: comparisonMeetingID) } label: { Label("Сравнить", systemImage: "arrow.left.arrow.right") }
                    .disabled(comparisonMeetingID.isEmpty)
            }
            MeetingCountStrip(meeting: meeting)
        }
    }
}

struct MeetingListRow: View {
    let meeting: MeetingRecord
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text(meeting.title).font(.body.weight(.semibold)).lineLimit(2)
                Spacer(minLength: 8)
                if meeting.openAttention > 0 {
                    Label("\(meeting.openAttention)", systemImage: "exclamationmark.circle.fill")
                        .font(.caption.bold()).foregroundStyle(RnDTheme.red)
                        .accessibilityLabel("Требуют внимания: \(meeting.openAttention)")
                }
            }
            Text(displayMeetingDate(meeting.occurredAt)).font(.caption).foregroundStyle(.secondary)
            if !meeting.participants.isEmpty {
                Text(meeting.participants.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            HStack(spacing: 7) {
                MeetingMiniCount(icon: "checkmark.seal.fill", count: meeting.itemCounts["decision"] ?? meeting.itemCounts["decisions"] ?? 0, label: "решений")
                MeetingMiniCount(icon: "person.crop.circle.badge.checkmark", count: meeting.itemCounts["action"] ?? meeting.itemCounts["actions"] ?? 0, label: "поручений")
                MeetingMiniCount(icon: "exclamationmark.triangle.fill", count: meeting.itemCounts["risk"] ?? meeting.itemCounts["risks"] ?? 0, label: "рисков")
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }
}

struct MeetingMiniCount: View {
    let icon: String
    let count: Int
    let label: String
    var body: some View {
        Label("\(count)", systemImage: icon).font(.caption2).foregroundStyle(count > 0 ? RnDTheme.blue : .secondary)
            .accessibilityLabel("\(count) \(label)")
    }
}

struct MeetingCountStrip: View {
    let meeting: MeetingRecord
    private let metrics: [(String, String, [String])] = [
        ("checkmark.seal.fill", "Решения", ["decision", "decisions"]),
        ("person.crop.circle.badge.checkmark", "Поручения", ["action", "actions"]),
        ("hand.raised.fill", "Обещания", ["commitment", "commitments"]),
        ("exclamationmark.triangle.fill", "Риски", ["risk", "risks"]),
        ("questionmark.circle.fill", "Вопросы", ["question", "questions"]),
    ]
    var body: some View {
        HStack(spacing: 7) {
            ForEach(Array(metrics.enumerated()), id: \.offset) { _, metric in
                let count = metric.2.compactMap { meeting.itemCounts[$0] }.first ?? 0
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) { Image(systemName: metric.0); Text("\(count)").fontWeight(.bold) }
                    Text(metric.1).font(.caption2)
                }
                .foregroundStyle(count > 0 ? RnDTheme.navy : .secondary)
                .padding(.horizontal, 9).padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 9).fill(RnDTheme.canvas))
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(metric.1): \(count)")
            }
        }
    }
}

struct MeetingItemCard: View {
    let item: MeetingItemRecord
    let sourcePath: String?
    let onStatus: (String, String) -> Void
    let onOpenSource: (EntityRecord) -> Void

    private var kind: MeetingKindFilter {
        MeetingKindFilter.allCases.first { $0 != .all && $0.matches(item.kind) } ?? .all
    }
    private var icon: String {
        switch kind {
        case .decision: return "checkmark.seal.fill"
        case .action: return "person.crop.circle.badge.checkmark"
        case .commitment: return "hand.raised.fill"
        case .risk: return "exclamationmark.triangle.fill"
        case .question: return "questionmark.circle.fill"
        case .all: return "circle.grid.cross.fill"
        }
    }
    private var sourceLocation: String {
        switch (item.sourceStart, item.sourceEnd) {
        case let (start?, end?): return "Фрагмент \(start)–\(end)"
        case let (start?, nil): return "Фрагмент с позиции \(start)"
        default: return "Первоисточник"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Label(kind == .all ? item.kind : kind.title, systemImage: icon).font(.caption.bold()).foregroundStyle(RnDTheme.blue)
                if !item.topic.isEmpty { Text(item.topic).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                Spacer()
                Menu {
                    Button("Открыто") { onStatus(item.id, "open") }
                    Button("Выполнено") { onStatus(item.id, "done") }
                    Button("Неактуально") { onStatus(item.id, "superseded") }
                } label: { StatusBadge(status: item.status) }
                .menuStyle(.borderlessButton)
                .accessibilityLabel("Статус: \(meetingStatusTitle(item.status)). Изменить статус")
            }
            Text(item.text).font(.body.weight(.medium)).textSelection(.enabled)
            if !item.owner.isEmpty || !item.dueAt.isEmpty {
                HStack(spacing: 13) {
                    if !item.owner.isEmpty { Label(item.owner, systemImage: "person.fill") }
                    if !item.dueAt.isEmpty { Label(displayMeetingDate(item.dueAt), systemImage: "calendar.badge.clock") }
                }.font(.caption).foregroundStyle(.secondary)
            }
            if !item.sourceQuote.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("«\(item.sourceQuote)»").font(.callout).italic().foregroundStyle(.secondary).textSelection(.enabled)
                    HStack {
                        Text(sourceLocation).font(.caption2).foregroundStyle(.secondary)
                        if let confidence = item.confidence { Text("· уверенность \(Int(confidence * 100))%").font(.caption2).foregroundStyle(.secondary) }
                        Spacer()
                        Button { onOpenSource(EntityRecord(id: item.id, title: "Транскрипт встречи", subtitle: sourceLocation, kind: "meeting", path: sourcePath)) } label: {
                            Label("Открыть источник", systemImage: "arrow.up.forward.square")
                        }
                        .buttonStyle(.borderless)
                        .disabled(sourcePath?.isEmpty != false)
                        .help(sourcePath?.isEmpty == false ? "Открыть локальный транскрипт" : "Путь к транскрипту недоступен")
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 9).fill(RnDTheme.canvas))
            }
        }
        .padding(13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 13).fill(RnDTheme.panel))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(RnDTheme.line))
        .accessibilityElement(children: .contain)
    }
}

struct MeetingBriefingCard: View {
    let text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Briefing к следующей встрече", systemImage: "doc.text.magnifyingglass").font(.headline).foregroundStyle(.white)
            Text(text).foregroundStyle(.white).textSelection(.enabled)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).fill(LinearGradient(colors: [RnDTheme.navy, RnDTheme.blue], startPoint: .topLeading, endPoint: .bottomTrailing)))
    }
}

struct MeetingDiffView: View {
    let items: [EntityRecord]
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label("Что изменилось", systemImage: "arrow.left.arrow.right.circle.fill").font(.headline)
            ForEach(items) { item in
                HStack(alignment: .top, spacing: 9) {
                    Image(systemName: item.status == "removed" ? "minus.circle.fill" : item.status == "added" ? "plus.circle.fill" : "arrow.triangle.2.circlepath.circle.fill")
                        .foregroundStyle(item.status == "removed" ? RnDTheme.red : RnDTheme.blue)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.title).font(.callout.weight(.semibold))
                        Text(item.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).fill(RnDTheme.panel))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(RnDTheme.line))
    }
}

private func meetingDate(_ raw: String) -> Date? {
    guard !raw.isEmpty else { return nil }
    let iso = ISO8601DateFormatter()
    if let date = iso.date(from: raw) { return date }
    for format in ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd"] {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = format
        if let date = formatter.date(from: raw) { return date }
    }
    return nil
}

private func displayMeetingDate(_ raw: String) -> String {
    guard let date = meetingDate(raw) else { return raw.isEmpty ? "Дата не указана" : raw }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "ru_RU")
    formatter.dateStyle = .medium
    formatter.timeStyle = raw.count > 10 ? .short : .none
    return formatter.string(from: date)
}

private func displayArtifactDate(_ raw: String) -> String {
    guard !raw.isEmpty else { return "Дата не указана" }
    return displayMeetingDate(raw)
}

private func meetingStatusTitle(_ status: String) -> String {
    switch status {
    case "open", "new": return "Открыто"
    case "in_progress", "running": return "В работе"
    case "done", "completed": return "Выполнено"
    case "dismissed", "superseded": return "Неактуально"
    case "error": return "Ошибка"
    default: return status.isEmpty ? "Без статуса" : status
    }
}

struct SearchView: View {
    @ObservedObject var controller: BackendController
    @State private var query = ""
    @State private var globally = false
    var body: some View {
        VStack(spacing: 14) {
            HStack { TextField("Искать по документам и встречам…", text: $query).textFieldStyle(.roundedBorder).onSubmit { controller.search(query, globally: globally) }; Toggle("Все пространства", isOn: $globally).toggleStyle(.switch); Button("Найти") { controller.search(query, globally: globally) }.buttonStyle(.borderedProminent).disabled(query.isEmpty) }.padding(18)
            Divider().opacity(0.35)
            ScrollView { LazyVStack(alignment: .leading, spacing: 10) { if controller.searchResults.isEmpty { EmptyState(icon: "magnifyingglass", title: "Локальный ИИ-поиск", detail: "Поиск работает по импортированным источникам. Для аналитического отчёта используйте /research.") }; ForEach(controller.searchResults) { item in Button { controller.openSource(item) } label: { EntityRow(item: item, icon: "doc.text.magnifyingglass") }.buttonStyle(.plain) } }.padding(18) }
        }
    }
}

struct InboxView: View {
    @ObservedObject var controller: BackendController
    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                AttentionPanel(
                    events: controller.attentionEvents,
                    limit: 5,
                    compact: false,
                    onExplain: controller.explainAttention,
                    onShowAll: nil,
                    onOpenSource: controller.openSource
                )
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Label("Лента уведомлений", systemImage: "tray.full")
                            .font(.headline)
                        Spacer()
                        Text("\(controller.inbox.count)")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            .accessibilityLabel("Уведомлений: \(controller.inbox.count)")
                    }
                    if controller.inbox.isEmpty {
                        EmptyState(icon: "tray", title: "Уведомлений нет", detail: "Здесь появятся готовые результаты, новые источники и ошибки автоматизаций.")
                    }
                    ForEach(controller.inbox) { item in
                        Button { controller.openInboxItem(item) } label: {
                            EntityRow(
                                item: item,
                                icon: item.status == "new" ? "bell.badge.fill" : "bell",
                                trailing: item.sourceRef?.isEmpty == false
                                    ? AnyView(Image(systemName: "chevron.right").foregroundStyle(.secondary))
                                    : nil
                            )
                        }
                        .buttonStyle(.plain)
                            .contextMenu {
                                if item.sourceRef?.isEmpty == false {
                                    Button("Открыть связанный объект") { controller.openInboxItem(item) }
                                }
                                if item.status == "new" {
                                    Button("Отметить прочитанным") { controller.markInboxRead(item.id) }
                                }
                            }
                            .accessibilityLabel("Событие: \(item.title)")
                            .accessibilityHint(item.sourceRef?.isEmpty == false ? "Открывает связанный объект" : "Отмечает уведомление прочитанным")
                    }
                }
                .padding(16)
                .background(RoundedRectangle(cornerRadius: 16).fill(RnDTheme.panel))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(RnDTheme.line))
            }
            .padding(20)
        }
    }
}

struct SkillsView: View {
    @ObservedObject var controller: BackendController
    @State private var showCreate = false
    @State private var name = ""
    @State private var command = "/"
    @State private var description = ""
    @State private var instruction = ""
    @State private var editingID: String?
    let columns = [GridItem(.adaptive(minimum: 220), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Скиллы выполнения задач").foregroundStyle(.secondary)
                        Text("«Применить» запускает скилл в текущей задаче; «В запрос» позволяет сначала дополнить команду.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Button { beginCreating() } label: { Label("Создать скилл", systemImage: "plus") }
                }
                if controller.skills.isEmpty {
                    EmptyState(icon: "wand.and.stars", title: "Скиллов пока нет", detail: "Создайте скилл с инструкцией и командой через /.")
                }
                LazyVGrid(columns: columns, spacing: 12) {
                    ForEach(controller.skills) { item in
                        SkillCatalogCard(
                            item: item,
                            canApply: controller.canSendText,
                            onApply: { controller.runSkillCommand(item) },
                            onInsert: { controller.insertSkillCommand(item) },
                            onEdit: { beginEditing(item) }
                        )
                    }
                }
            }
            .padding(20)
        }
        .sheet(isPresented: $showCreate) {
            FormSheet(
                title: editingID == nil ? "Новый скилл" : "Изменить скилл",
                primary: "Сохранить",
                onCancel: { showCreate = false },
                onPrimary: {
                    controller.saveSkill(
                        id: editingID,
                        name: name,
                        command: command,
                        description: description,
                        instruction: instruction
                    )
                    showCreate = false
                }
            ) {
                TextField("Название", text: $name)
                TextField("Команда через /", text: $command)
                TextField("Описание", text: $description)
                TextEditor(text: $instruction)
                    .frame(height: 130)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            }
        }
    }

    private func beginCreating() {
        editingID = nil
        name = ""
        command = "/"
        description = ""
        instruction = ""
        showCreate = true
    }

    private func beginEditing(_ item: EntityRecord) {
        editingID = item.id
        name = item.title
        command = item.command
        description = item.detail
        instruction = item.content
        showCreate = true
    }
}

struct SkillCatalogCard: View {
    let item: EntityRecord
    let canApply: Bool
    let onApply: () -> Void
    let onInsert: () -> Void
    let onEdit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "wand.and.stars").foregroundStyle(RnDTheme.blue)
                Text(item.command).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if item.version > 0 { Text("v\(item.version)").font(.caption2).foregroundStyle(.tertiary) }
            }
            Text(item.title).font(.headline).lineLimit(2)
            Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            Spacer(minLength: 0)
            HStack(spacing: 7) {
                Button("Применить", action: onApply)
                    .buttonStyle(.borderedProminent)
                    .disabled(!canApply || !item.enabled)
                    .help("Запустить \(item.command) в текущей задаче")
                Button("В запрос", action: onInsert)
                    .buttonStyle(.bordered)
                    .disabled(!item.enabled)
                    .help("Вставить команду в поле ввода")
                Spacer()
                Button(action: onEdit) { Image(systemName: "pencil") }
                    .buttonStyle(.borderless)
                    .help("Изменить скилл")
                    .accessibilityLabel("Изменить скилл «\(item.title)»")
            }
        }
        .frame(maxWidth: .infinity, minHeight: 150, alignment: .topLeading)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14).fill(RnDTheme.panel))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(RnDTheme.line))
        .shadow(color: RnDTheme.navy.opacity(0.04), radius: 8, y: 3)
    }
}

struct CapabilitiesView: View {
    @ObservedObject var controller: BackendController
    let columns = [GridItem(.adaptive(minimum: 240), spacing: 12)]
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 14) { Text("Навыки и подключения помощника").foregroundStyle(.secondary); if controller.capabilities.isEmpty { EmptyState(icon: "puzzlepiece.extension", title: "Навыков пока нет", detail: "Подключённые навыки и доступные интеграции появятся здесь.") }; LazyVGrid(columns: columns, spacing: 12) { ForEach(controller.capabilities) { item in CatalogCard(item: item, icon: item.status == "connected" ? "checkmark.circle.fill" : "link.badge.plus") } } }.padding(20) } }
}

struct ArtifactsView: View {
    @ObservedObject var controller: BackendController
    @State private var editingArtifact: EntityRecord?
    @State private var artifactContent = ""
    @State private var showEditor = false
    @State private var artifactPendingDeletion: EntityRecord?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                if controller.artifacts.isEmpty {
                    EmptyState(icon: "doc.richtext", title: "Артефактов пока нет", detail: "Команды /document, /research, /meeting, /briefing и /digest сохраняют отдельные результаты.")
                }
                ForEach(controller.artifacts) { item in
                    EntityRow(
                        item: item,
                        icon: "doc.richtext.fill",
                        trailing: AnyView(HStack(spacing: 8) {
                            ClassificationMenu(value: item.classification) {
                                controller.setClassification(entityType: "artifact", id: item.id, value: $0)
                            }
                            Button { showHistory(item) } label: { Image(systemName: "clock.arrow.circlepath") }
                                .buttonStyle(.borderless)
                                .help("История версий и происхождение")
                                .accessibilityLabel("История версий материала «\(item.title)»")
                            Button { beginEditing(item) } label: { Image(systemName: "pencil") }
                                .buttonStyle(.borderless)
                                .help("Создать новую версию")
                                .accessibilityLabel("Изменить материал «\(item.title)»")
                            Button { controller.openArtifact(item) } label: { Image(systemName: "arrow.up.forward.app") }
                                .buttonStyle(.borderless)
                                .help("Открыть текущую версию")
                                .accessibilityLabel("Открыть материал «\(item.title)»")
                            Button(role: .destructive) { artifactPendingDeletion = item } label: { Image(systemName: "trash") }
                                .buttonStyle(.borderless)
                                .foregroundStyle(RnDTheme.red)
                                .fixedSize(horizontal: true, vertical: false)
                                .disabled(!controller.canDeleteEntities)
                                .help("Удалить материал «\(item.title)»")
                                .accessibilityLabel("Удалить материал «\(item.title)»")
                        })
                    )
                    .contextMenu {
                        Menu("Классификация") {
                            ForEach(DataClassification.allCases) { classification in
                                Button(classification.title) {
                                    controller.setClassification(entityType: "artifact", id: item.id, value: classification.rawValue)
                                }
                            }
                        }
                        Button("История версий…") { showHistory(item) }
                        Button("Изменить") { beginEditing(item) }
                        Button("Открыть") { controller.openArtifact(item) }
                        Divider()
                        Button("Удалить материал…", role: .destructive) { artifactPendingDeletion = item }
                            .disabled(!controller.canDeleteEntities)
                    }
                }
            }
            .padding(20)
        }
        .sheet(isPresented: $showEditor) {
            FormSheet(title: editingArtifact?.title ?? "Артефакт", primary: "Новая версия", onCancel: { showEditor = false }, onPrimary: { if let item = editingArtifact { controller.updateArtifact(item.id, content: artifactContent) }; showEditor = false }) {
                Text("Сохранение создаст новую версию; предыдущая останется на диске.").font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $artifactContent).font(.system(.body, design: .monospaced)).frame(height: 360).overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            }
        }
        .confirmationDialog(
            "Удалить материал «\(artifactPendingDeletion?.title ?? "")»?",
            isPresented: Binding(
                get: { artifactPendingDeletion != nil },
                set: { if !$0 { artifactPendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Удалить материал", role: .destructive) {
                if let artifact = artifactPendingDeletion { controller.deleteArtifact(artifact.id) }
                artifactPendingDeletion = nil
            }
            .disabled(!controller.canDeleteEntities)
            Button("Отмена", role: .cancel) { artifactPendingDeletion = nil }
        } message: {
            Text("Все версии материала будут удалены из RnD Workbench. Управляемые файлы будут перемещены в Корзину.")
        }
    }

    private func beginEditing(_ item: EntityRecord) {
        editingArtifact = item
        artifactContent = item.path.flatMap { try? String(contentsOfFile: $0, encoding: .utf8) } ?? ""
        showEditor = true
    }

    private func showHistory(_ item: EntityRecord) {
        controller.loadArtifactHistory(item)
    }
}

struct ArtifactHistorySheet: View {
    @ObservedObject var controller: BackendController
    let requestedArtifact: EntityRecord
    @Environment(\.dismiss) private var dismiss
    @State private var selectedVersion: Int?
    @State private var restoreConfirmationVersion: Int?

    private var artifact: EntityRecord {
        guard controller.artifactHistoryArtifact?.id == requestedArtifact.id else {
            return requestedArtifact
        }
        return controller.artifactHistoryArtifact ?? requestedArtifact
    }

    private var versions: [ArtifactVersionRecord] {
        controller.artifactVersions.sorted { $0.version > $1.version }
    }

    private var selectedRecord: ArtifactVersionRecord? {
        let preferred = selectedVersion ?? artifact.version
        return versions.first { $0.version == preferred } ?? versions.first
    }

    private var selectedRelations: [ArtifactRelationRecord] {
        guard let version = selectedRecord?.version else { return [] }
        return controller.artifactRelations.filter { $0.artifactVersion == version }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(artifact.title).font(.title2.bold()).lineLimit(2)
                    Text("История версий и происхождение материала")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button { controller.refreshArtifactHistory() } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .disabled(controller.artifactHistoryLoading || controller.artifactRestorePendingVersion != nil)
                .help("Обновить историю")
                .accessibilityLabel("Обновить историю версий")
                Button("Готово") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(20)

            Divider()

            if let error = controller.artifactHistoryError {
                HStack(alignment: .top, spacing: 9) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(RnDTheme.red)
                    Text(error).font(.callout).textSelection(.enabled)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 10)
                .background(RnDTheme.red.opacity(0.07))
            }

            if controller.artifactHistoryLoading && versions.isEmpty {
                VStack(spacing: 12) {
                    ProgressView()
                    Text("Загружаю историю материала…").foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                HSplitView {
                    versionList
                        .frame(minWidth: 220, idealWidth: 250, maxWidth: 300)
                    versionDetails
                        .frame(minWidth: 420, maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .frame(minWidth: 720, idealWidth: 780, minHeight: 520, idealHeight: 590)
        .onAppear {
            selectedVersion = artifact.version > 0 ? artifact.version : nil
            if controller.artifactHistoryArtifact?.id != requestedArtifact.id {
                controller.loadArtifactHistory(requestedArtifact)
            }
        }
        .onChange(of: controller.artifactHistoryArtifact?.version) { _, newVersion in
            if let newVersion, newVersion > 0 { selectedVersion = newVersion }
        }
        .confirmationDialog(
            "Восстановить версию \(restoreConfirmationVersion ?? 0)?",
            isPresented: Binding(
                get: { restoreConfirmationVersion != nil },
                set: { if !$0 { restoreConfirmationVersion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Восстановить как новую версию") {
                if let version = restoreConfirmationVersion,
                   let record = versions.first(where: { $0.version == version }) {
                    controller.restoreArtifactVersion(record)
                }
                restoreConfirmationVersion = nil
            }
            Button("Отмена", role: .cancel) { restoreConfirmationVersion = nil }
        } message: {
            Text("Текущая версия останется в истории. Содержимое выбранной версии будет сохранено как новая версия материала.")
        }
    }

    private var versionList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Версии").font(.headline)
                    Spacer()
                    Text("\(versions.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                .padding(.bottom, 3)

                ForEach(versions) { version in
                    Button { selectedVersion = version.version } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text("Версия \(version.version)").font(.callout.weight(.semibold))
                                Spacer()
                                if version.isCurrent {
                                    Text("Текущая")
                                        .font(.caption2.weight(.semibold))
                                        .padding(.horizontal, 7)
                                        .padding(.vertical, 2)
                                        .foregroundStyle(RnDTheme.blue)
                                        .background(Capsule().fill(RnDTheme.blue.opacity(0.12)))
                                }
                            }
                            Text(displayArtifactDate(version.createdAt))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if let restored = version.restoredFromVersion {
                                Label("Из версии \(restored)", systemImage: "arrow.uturn.backward")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 10)
                                .fill(selectedRecord?.id == version.id ? RnDTheme.blue.opacity(0.11) : RnDTheme.panel)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 10)
                                .stroke(selectedRecord?.id == version.id ? RnDTheme.blue : RnDTheme.line)
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Версия \(version.version)\(version.isCurrent ? ", текущая" : "")")
                }
            }
            .padding(16)
        }
        .background(RnDTheme.canvas.opacity(0.55))
    }

    private var versionDetails: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let version = selectedRecord {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Версия \(version.version)").font(.title3.bold())
                        if version.isCurrent { StatusBadge(status: "done") }
                        Spacer()
                        if controller.artifactRestorePendingVersion == version.version {
                            ProgressView().controlSize(.small)
                            Text("Восстанавливаю…").font(.caption).foregroundStyle(.secondary)
                        } else {
                            Button("Открыть файл") { controller.openArtifactVersion(version) }
                                .disabled(version.path?.isEmpty != false)
                            Button("Восстановить…") { restoreConfirmationVersion = version.version }
                                .buttonStyle(.borderedProminent)
                                .disabled(
                                    version.isCurrent
                                        || controller.artifactRestorePendingVersion != nil
                                        || !(artifact.kind == "markdown" || artifact.subtitle == "markdown")
                                )
                        }
                    }

                    if !version.metadataSummary.isEmpty {
                        provenanceBlock(
                            icon: "info.circle.fill",
                            title: "Метаданные версии",
                            detail: version.metadataSummary
                        )
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Label("Происхождение", systemImage: "point.3.connected.trianglepath.dotted")
                                .font(.headline)
                            Spacer()
                            if controller.artifactRelationsLoading {
                                ProgressView().controlSize(.small)
                            }
                        }
                        Text("Связи показывают, из какой задачи, источника или версии создан результат.")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        if selectedRelations.isEmpty && !controller.artifactRelationsLoading {
                            Text("Для этой версии связанных объектов не записано.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 8)
                        } else {
                            ForEach(selectedRelations) { relation in
                                provenanceRow(relation)
                            }
                        }
                    }
                } else {
                    EmptyState(
                        icon: "clock.arrow.circlepath",
                        title: "История пуста",
                        detail: "Для этого материала пока нет сохранённых версий."
                    )
                }
            }
            .padding(20)
        }
    }

    @ViewBuilder
    private func provenanceRow(_ relation: ArtifactRelationRecord) -> some View {
        let presentation = relationPresentation(relation)
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: presentation.icon)
                .foregroundStyle(RnDTheme.blue)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text(presentation.title).font(.callout.weight(.semibold))
                Text(presentation.target).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                if !relation.metadataSummary.isEmpty {
                    Text(relation.metadataSummary).font(.caption2).foregroundStyle(.tertiary).textSelection(.enabled)
                }
            }
            Spacer()
        }
        .padding(11)
        .background(RoundedRectangle(cornerRadius: 10).fill(RnDTheme.canvas.opacity(0.7)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(RnDTheme.line))
        .accessibilityElement(children: .combine)
    }

    private func provenanceBlock(icon: String, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).foregroundStyle(RnDTheme.steel)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.callout.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(RnDTheme.canvas.opacity(0.55)))
    }

    private func relationPresentation(_ relation: ArtifactRelationRecord) -> (title: String, target: String, icon: String) {
        let shortID: (String?) -> String = { value in
            guard let value, !value.isEmpty else { return "Связанный объект не указан" }
            return String(value.prefix(12))
        }
        switch relation.relationType {
        case "produced_by_task":
            let title = controller.tasks.first { $0.id == relation.taskID }?.title ?? shortID(relation.taskID)
            return ("Создано задачей", title, "checklist")
        case "derived_from_source":
            let title = controller.sources.first { $0.id == relation.sourceID }?.title ?? shortID(relation.sourceID)
            return ("На основе источника", title, "doc.text.magnifyingglass")
        case "derived_from_artifact":
            let title = controller.artifacts.first { $0.id == relation.relatedArtifactID }?.title ?? shortID(relation.relatedArtifactID)
            let suffix = relation.relatedArtifactVersion.map { " · версия \($0)" } ?? ""
            return ("На основе материала", title + suffix, "doc.on.doc")
        case "revision_of":
            let version = relation.relatedArtifactVersion.map(String.init) ?? "?"
            return ("Редакция", "Предыдущая версия \(version)", "pencil.and.outline")
        case "restored_from":
            let version = relation.relatedArtifactVersion.map(String.init) ?? "?"
            return ("Восстановлено", "Версия \(version)", "arrow.uturn.backward.circle")
        default:
            return (relation.relationType.isEmpty ? "Связь" : relation.relationType, shortID(relation.relatedArtifactID ?? relation.sourceID ?? relation.taskID), "link")
        }
    }
}

struct AutomationsView: View {
    @ObservedObject var controller: BackendController
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = "ежедневно 09:00"
    @State private var editingID: String?
    var body: some View {
        HSplitView {
            ScrollView { VStack(alignment: .leading, spacing: 12) { Text(editingID == nil ? "Новая автоматизация" : "Изменить автоматизацию").font(.headline); TextField("Название", text: $name); TextEditor(text: $prompt).frame(height: 110).overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary)); TextField("ежедневно 09:00", text: $schedule); Text("Поддерживается: ежедневно HH:MM, каждую пятницу HH:MM, once YYYY-MM-DD HH:MM или «при новом источнике»").font(.caption).foregroundStyle(.secondary); Button(editingID == nil ? "Создать" : "Сохранить") { if let id = editingID { controller.updateAutomation(id: id, name: name, prompt: prompt, schedule: schedule) } else { controller.createAutomation(name: name, prompt: prompt, schedule: schedule) }; editingID = nil; name = ""; prompt = ""; schedule = "ежедневно 09:00" }.buttonStyle(.borderedProminent).disabled(name.isEmpty || prompt.isEmpty); if editingID != nil { Button("Отменить изменение") { editingID = nil; name = ""; prompt = ""; schedule = "ежедневно 09:00" } } }.padding(20) }.frame(minWidth: 300, idealWidth: 340)
            ScrollView { LazyVStack(alignment: .leading, spacing: 10) { ForEach(controller.automations) { item in EntityRow(item: item, icon: "clock.arrow.2.circlepath", trailing: AnyView(Toggle("", isOn: Binding(get: { item.enabled }, set: { _ in controller.toggleAutomation(item) })).labelsHidden())).contextMenu { Button("Изменить") { editingID = item.id; name = item.title; prompt = item.detail; schedule = item.subtitle }; Button("Удалить", role: .destructive) { controller.deleteAutomation(item.id) } } }; if controller.automations.isEmpty { EmptyState(icon: "clock", title: "Нет автоматизаций", detail: "Локальный планировщик работает, пока приложение запущено.") } }.padding(20) }.frame(minWidth: 400)
        }
    }
}

struct ApprovalsView: View {
    @ObservedObject var controller: BackendController
    @State private var editingApproval: EntityRecord?
    @State private var payload = ""
    @State private var showEditor = false
    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                Text("Внешние действия требуют подтверждения. Неподключённые навыки не будут имитировать успешное выполнение. Отклонение окончательно для текущего шага; ошибочный шаг можно изменить и вернуть в очередь.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                ForEach(controller.approvals) { item in
                    VStack(alignment: .leading, spacing: 9) {
                        EntityRow(
                            item: item,
                            icon: approvalIcon(item.status),
                            trailing: AnyView(
                                Text(approvalStatusTitle(item.status))
                                    .font(.caption2.weight(.medium))
                                    .foregroundStyle(approvalStatusColor(item.status))
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(
                                        Capsule().fill(approvalStatusColor(item.status).opacity(0.12))
                                    )
                            )
                        )
                        if item.status == "pending" {
                            HStack {
                                Spacer()
                                Button("Изменить") { beginEditing(item) }
                                Button("Отклонить") {
                                    controller.resolveApproval(item.id, status: "rejected")
                                }
                                Button("Подтвердить") {
                                    controller.resolveApproval(item.id, status: "approved")
                                }
                                .buttonStyle(.borderedProminent)
                            }
                        } else if item.status == "error" {
                            HStack {
                                Label("Действие не выполнено", systemImage: "exclamationmark.triangle.fill")
                                    .font(.caption)
                                    .foregroundStyle(RnDTheme.red)
                                Spacer()
                                Button("Изменить и повторить") { beginEditing(item) }
                            }
                        }
                    }
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 12).fill(RnDTheme.panel))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(RnDTheme.line))
                }
                if controller.approvals.isEmpty {
                    EmptyState(
                        icon: "checkmark.shield",
                        title: "История согласований пуста",
                        detail: "Черновики писем, встреч и сообщений появятся здесь."
                    )
                }
            }
            .padding(20)
        }
        .sheet(isPresented: $showEditor) {
            FormSheet(
                title: editingApproval?.status == "error" ? "Изменить и повторить действие" : "Параметры действия",
                primary: editingApproval?.status == "error" ? "Вернуть в очередь" : "Сохранить",
                onCancel: { showEditor = false },
                onPrimary: {
                    if let item = editingApproval {
                        controller.updateApproval(item.id, payload: payload)
                    }
                    showEditor = false
                }
            ) {
                Text("JSON-предпросмотр действия. Сохранение ошибочного шага создаёт новую редакцию и снова требует подтверждения.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextEditor(text: $payload)
                    .font(.system(.body, design: .monospaced))
                    .frame(height: 220)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            }
        }
    }

    private func beginEditing(_ item: EntityRecord) {
        editingApproval = item
        payload = item.content
        showEditor = true
    }

    private func approvalIcon(_ status: String) -> String {
        switch status {
        case "succeeded": return "checkmark.shield.fill"
        case "rejected", "cancelled": return "xmark.shield.fill"
        case "error": return "exclamationmark.shield.fill"
        case "executing": return "arrow.triangle.2.circlepath"
        default: return "checkmark.shield"
        }
    }

    private func approvalStatusTitle(_ status: String) -> String {
        switch status {
        case "pending": return "Ожидает"
        case "approved": return "Подтверждено"
        case "executing": return "Выполняется"
        case "succeeded": return "Выполнено"
        case "rejected": return "Отклонено"
        case "cancelled": return "Отменено"
        case "error": return "Ошибка"
        default: return status
        }
    }

    private func approvalStatusColor(_ status: String) -> Color {
        switch status {
        case "succeeded": return RnDTheme.blue
        case "error", "rejected", "cancelled": return RnDTheme.red
        case "approved", "executing": return RnDTheme.navy
        default: return RnDTheme.steel
        }
    }
}

struct SettingsView: View {
    @ObservedObject var controller: BackendController
    @AppStorage("rnd.speakReplies") private var speakReplies = true
    @State private var llmModeDraft = "local"
    @State private var externalBaseURLDraft = ""
    @State private var externalModelDraft = ""
    @State private var apiKeyDraft = ""
    @State private var autoProviderTypeDraft = "corporate"
    @State private var autoRemotePolicyDraft = "local_only"
    @State private var draftEndpointHasStoredKey = false
    @State private var didLoadLLMDrafts = false
    @State private var preflightDetailsExpanded = false
    @State private var pilotUsageExpanded = false
    @FocusState private var focusedLLMField: LLMSettingsField?

    private enum LLMSettingsField: Hashable { case baseURL, model, apiKey }

    var body: some View {
        Form {
            Section("Персонализация") {
                Picker("Стиль ответа", selection: Binding(get: { controller.settings["response_style"] ?? "brief" }, set: { controller.setSetting("response_style", $0) })) { Text("Краткий").tag("brief"); Text("Подробный").tag("detailed") }
                Picker("Проактивность", selection: Binding(get: { controller.settings["proactivity"] ?? "balanced" }, set: { controller.setSetting("proactivity", $0) })) { Text("Тихая").tag("quiet"); Text("Сбалансированная").tag("balanced"); Text("Проактивная").tag("proactive") }
                Toggle("Рабочая память", isOn: Binding(get: { controller.settings["memory_enabled"] == "true" }, set: { controller.setSetting("memory_enabled", $0 ? "true" : "false") }))
                VStack(alignment: .leading, spacing: 7) {
                    Text("Что можно запоминать").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Toggle("Предпочтения", isOn: Binding(get: { controller.settings["memory_preferences_enabled"] == "true" }, set: { controller.setSetting("memory_preferences_enabled", $0 ? "true" : "false") }))
                    Toggle("Факты", isOn: Binding(get: { controller.settings["memory_facts_enabled"] == "true" }, set: { controller.setSetting("memory_facts_enabled", $0 ? "true" : "false") }))
                    Toggle("Обязательства", isOn: Binding(get: { controller.settings["memory_commitments_enabled"] == "true" }, set: { controller.setSetting("memory_commitments_enabled", $0 ? "true" : "false") }))
                    Toggle("Рабочие заметки и результаты", isOn: Binding(get: { controller.settings["memory_work_enabled"] == "true" }, set: { controller.setSetting("memory_work_enabled", $0 ? "true" : "false") }))
                }
                .padding(.leading, 18)
                .disabled(controller.settings["memory_enabled"] != "true")
            }
            Section("Голос") {
                Toggle("Озвучивать текстовые ответы", isOn: $speakReplies)
                Toggle("Проверять распознанный текст перед отправкой", isOn: Binding(get: { controller.settings["voice_review_before_send"] == "true" }, set: { controller.setSetting("voice_review_before_send", $0 ? "true" : "false") }))
                Text("Если включено, голосовой запрос после распознавания появится в поле ввода: его можно исправить и отправить вручную.").font(.caption).foregroundStyle(.secondary)
                Text("Настройка общая для основного и компактного интерфейсов.").font(.caption).foregroundStyle(.secondary)
                Divider()
                LabeledContent("Глобальная диктовка", value: "Правая ⌥ · удерживать")
                Label(
                    controller.globalPushToTalkStatusLabel,
                    systemImage: controller.globalPushToTalkReady
                        ? "checkmark.circle.fill" : "lock.trianglebadge.exclamationmark"
                )
                .foregroundStyle(controller.globalPushToTalkReady ? RnDTheme.blue : RnDTheme.red)
                .accessibilityLabel("Статус глобальной диктовки: \(controller.globalPushToTalkStatusLabel)")
                Text(controller.globalPushToTalkStatusDetail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let status = controller.externalDictationStatus {
                    Text(status)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(RnDTheme.ink)
                }
                if !controller.globalPushToTalkReady || !controller.eventPostingPermissionGranted {
                    Button("Настроить доступ macOS") {
                        controller.requestGlobalPushToTalkPermissions()
                    }
                    .accessibilityHint("Запрашивает Универсальный доступ, Мониторинг ввода и разрешение резервной вставки")
                }
            }
            Section("Качество и готовность пилота") {
                GroupBox {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(controller.pilotOnboardingTitle)
                                .font(.callout.weight(.semibold))
                            Spacer()
                            Text(controller.pilotOnboardingProgressLabel)
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                        }
                        ProgressView(
                            value: Double(controller.pilotOnboardingCompleted),
                            total: Double(controller.pilotOnboardingTotal)
                        )
                        Text(controller.pilotOnboardingDetail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if !controller.pilotOnboardingActionLabel.isEmpty {
                            Button(controller.pilotOnboardingActionLabel) {
                                controller.performPilotOnboardingAction()
                            }
                            .buttonStyle(.borderedProminent)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } label: {
                    Label("Быстрый старт", systemImage: "figure.walk.motion")
                }
                LabeledContent(
                    "Измерений за 14 дней",
                    value: "\(controller.pilotMetricSampleCount)"
                )
                Text(controller.pilotMetricsSummaryLabel)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(RnDTheme.ink)
                DisclosureGroup("Использование пилота", isExpanded: $pilotUsageExpanded) {
                    LabeledContent("Активных дней · 28 дней", value: "\(controller.pilotActiveDays)")
                    LabeledContent("Завершённых запросов", value: "\(controller.pilotCompletedTurns)")
                    Text(controller.pilotUsageSummaryLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker(
                        "Оценка полезности",
                        selection: Binding(
                            get: { controller.pilotUsefulnessRating },
                            set: { controller.setPilotUsefulnessRating($0) }
                        )
                    ) {
                        Text("Не указана").tag(0).disabled(true)
                        ForEach(1...5, id: \.self) { value in
                            Text("\(value) из 5").tag(value)
                        }
                    }
                    .pickerStyle(.menu)
                }
                Button("Экспортировать отчёт JSON") {
                    controller.exportPilotMetrics()
                }
                Text("Экспорт содержит только агрегаты качества, активных дней и завершённых сценариев — без запросов, транскриптов, документов и идентификаторов сессий.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Divider()
                LabeledContent("Итог", value: controller.pilotPreflightOverallLabel)
                Text(controller.pilotPreflightSummaryLabel)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(RnDTheme.ink)
                Button("Проверить снова") {
                    controller.runPilotPreflight()
                }
                DisclosureGroup("Подробности проверки", isExpanded: $preflightDetailsExpanded) {
                    ForEach(controller.pilotPreflightChecks) { check in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: preflightIcon(check.status))
                                .foregroundStyle(preflightColor(check.status))
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(check.title).font(.callout.weight(.semibold))
                                Text(check.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                if !check.action.isEmpty {
                                    Text(check.action)
                                        .font(.caption)
                                        .foregroundStyle(preflightColor(check.status))
                                }
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
                Text("Проверка передаёт только технические статусы и агрегаты — без запросов, транскриптов и ответов.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Данные и модели") {
                LabeledContent(
                    "Активная маршрутизация",
                    value: controller.configuredLLMModeLabel
                )
                LabeledContent("Последний фактический маршрут", value: controller.actualLLMRouteLabel)
                LabeledContent(
                    "Общая политика маршрутизации",
                    value: controller.javaCorePolicyStatusLabel
                )
                LabeledContent(
                    "Защита внешних действий",
                    value: controller.javaActionJournalStatusLabel
                )
                LabeledContent("Активная модель", value: controller.modelName)

                Picker("Модель для ответов", selection: $llmModeDraft) {
                    Text("Локально").tag("local")
                    Text("Авто").tag("auto")
                    Text("Корпоративная").tag("corporate")
                    Text("Внешняя").tag("external")
                }
                .pickerStyle(.segmented)

                if llmModeDraft == "auto" {
                    VStack(alignment: .leading, spacing: 10) {
                        Picker("Провайдер для удалённого маршрута", selection: $autoProviderTypeDraft) {
                            Text("Корпоративный").tag("corporate")
                            Text("Внешний").tag("external")
                        }
                        Picker("Политика Авто", selection: $autoRemotePolicyDraft) {
                            Text("Только локально").tag("local_only")
                            Text("Удалённо для допустимых данных").tag("eligible")
                        }
                        Label(
                            autoRemotePolicyDraft == "eligible"
                                ? "Каждый запрос проверяется по классификации. Если удалённый маршрут недоступен до первого токена, ответ продолжится локально без изменения выбранного режима."
                                : "Все запросы выполняются локально. Настройки удалённого провайдера не используются.",
                            systemImage: "arrow.triangle.branch"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                }

                if shouldShowRemoteFields {
                    VStack(alignment: .leading, spacing: 10) {
                        TextField("Адрес OpenAI-совместимой API", text: $externalBaseURLDraft)
                            .focused($focusedLLMField, equals: .baseURL)
                        TextField("Идентификатор модели", text: $externalModelDraft)
                            .focused($focusedLLMField, equals: .model)
                        SecureField(
                            draftEndpointHasStoredKey
                                ? "Ключ для этого адреса сохранён — оставьте пустым, чтобы не менять"
                                : "Ключ API",
                            text: $apiKeyDraft
                        )
                        .focused($focusedLLMField, equals: .apiKey)

                        HStack(spacing: 7) {
                            Image(systemName: draftEndpointHasStoredKey ? "key.fill" : "key")
                            Text(
                                draftEndpointHasStoredKey
                                    ? "У этого адреса собственный ключ в Связке ключей macOS; ключи других провайдеров не используются."
                                    : "Для localhost ключ можно не указывать. Для каждого удалённого адреса нужен собственный ключ."
                            )
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)

                        Picker("Контекст для удалённой модели", selection: externalContextScopeBinding) {
                            Text("Только текущая задача").tag("task")
                            Text("Рабочее пространство и память").tag("workspace")
                        }

                        Label {
                            if externalContextScope == "workspace" {
                                Text("Расширенный доступ: провайдеру передаются текущая задача, её история, разрешённые скиллы, источники автопоиска и рабочая память. Политика классификации применяется после выбора контекста: недопустимые автоматические данные исключаются, явные — блокируют отправку. Исходное аудио, STT и TTS остаются на устройстве.")
                            } else {
                                Text("Провайдеру передаются текущая задача, её история и только вручную прикреплённые или явно @упомянутые источники. Рабочая память и автопоиск не передаются. Исходное аудио, STT и TTS остаются на устройстве.")
                            }
                        } icon: {
                            Image(systemName: "exclamationmark.shield.fill")
                                .foregroundStyle(RnDTheme.red)
                        }
                        .font(.caption)
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 9).fill(RnDTheme.red.opacity(externalContextScope == "workspace" ? 0.10 : 0.055)))
                        .overlay(RoundedRectangle(cornerRadius: 9).stroke(RnDTheme.red.opacity(externalContextScope == "workspace" ? 0.34 : 0.18)))
                    }
                } else if llmModeDraft == "local" {
                    Label("Запросы и выбранный контекст обрабатываются локально через MLX.", systemImage: "lock.shield.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if controller.llmConfigurationPending {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Применяю конфигурацию модели…")
                    }
                    .font(.caption)
                } else if let message = controller.llmConfigurationError {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(RnDTheme.red)
                } else if (controller.isExternalLLMActive || controller.isAutoLLMActive)
                            && (controller.externalLLMReady || controller.isAutoLLMActive) {
                    Label(
                        controller.isAutoLLMActive
                            ? (controller.externalLLMReady
                                ? "Авто готов · удалённый маршрут доступен"
                                : "Авто готов · используется локальная модель")
                            : controller.isCorporateLLMActive
                            ? "Корпоративная модель настроена"
                            : "Внешняя модель настроена",
                        systemImage: "checkmark.circle.fill"
                    )
                        .font(.caption)
                        .foregroundStyle(RnDTheme.blue)
                }

                HStack {
                    if llmModeDraft != "local" || controller.llmMode != "local" {
                        Button(llmModeDraft != "local" ? "Сохранить и применить" : "Применить локальную MLX") {
                            applyLLMConfiguration()
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!controller.canChangeLLMConfiguration)
                    } else {
                        Label("Локальная модель активна", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(RnDTheme.blue)
                    }

                    if controller.isExternalLLMActive || controller.isAutoLLMActive || llmModeDraft != "local" {
                        Button("Вернуться к локальной MLX") {
                            llmModeDraft = "local"
                            apiKeyDraft = ""
                            controller.useLocalLLM()
                        }
                        .disabled(!controller.canChangeLLMConfiguration)
                    }

                    Spacer()

                    if shouldShowRemoteFields && draftEndpointHasStoredKey {
                        Button("Удалить ключ этого адреса", role: .destructive) {
                            apiKeyDraft = ""
                            controller.deleteExternalLLMAPIKey(baseURL: externalBaseURLDraft)
                            DispatchQueue.main.async { refreshDraftEndpointKeyStatus() }
                        }
                        .disabled(!controller.canChangeLLMConfiguration)
                    }
                }

                Label {
                    Text(
                        llmModeDraft == "local"
                            ? "Локальный маршрут допускает все четыре уровня данных."
                            : llmModeDraft == "auto"
                                ? "Авто всегда оставляет строго ограниченные данные локально и использует удалённую модель только когда выбранная политика и классификация это допускают."
                            : llmModeDraft == "corporate"
                                ? "Корпоративный маршрут допускает данные до уровня «Конфиденциальные»; строго ограниченные данные остаются локально."
                                : "Внешний маршрут допускает данные до уровня «Внутренние»; конфиденциальные и строго ограниченные данные не отправляются."
                    )
                } icon: {
                    Image(systemName: "lock.shield.fill")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Section("Классификация данных") {
                if let workspace = controller.currentWorkspace {
                    ClassificationPicker(
                        title: "Рабочее пространство",
                        value: workspace.classification
                    ) { value in
                        controller.setClassification(
                            entityType: "workspace", id: workspace.id, value: value
                        )
                    }
                }
                if let task = controller.currentTask {
                    ClassificationPicker(title: "Текущая задача", value: task.classification) {
                        controller.setClassification(
                            entityType: "task", id: task.id, value: $0
                        )
                    }
                }
                Text("Новые задачи и источники наследуют уровень родителя. Понижение уровня не изменяет старые сообщения и версии материалов автоматически.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Аудит") { ForEach(controller.audit.prefix(12)) { item in EntityRow(item: item, icon: item.subtitle == "success" ? "checkmark.circle" : "exclamationmark.triangle") } }
        }
        .formStyle(.grouped)
        .padding(12)
        .onAppear {
            loadLLMDrafts(force: true)
            controller.refreshGlobalPushToTalkPermissions()
        }
        .onChange(of: controller.settings) { _, _ in
            if focusedLLMField == nil { loadLLMDrafts(force: false) }
        }
        .onChange(of: llmModeDraft) { _, newMode in
            if newMode == "local" { apiKeyDraft = "" }
        }
        .onChange(of: externalBaseURLDraft) { _, _ in
            refreshDraftEndpointKeyStatus()
        }
    }

    private func applyLLMConfiguration() {
        focusedLLMField = nil
        if llmModeDraft != "local" {
            let targetMode = llmModeDraft == "auto" ? "auto" : "external"
            let providerType = llmModeDraft == "auto" ? autoProviderTypeDraft : llmModeDraft
            let autoRemotePolicy = llmModeDraft == "auto" ? autoRemotePolicyDraft : "eligible"
            controller.configureExternalLLM(
                baseURL: externalBaseURLDraft,
                model: externalModelDraft,
                apiKey: apiKeyDraft,
                providerType: providerType,
                mode: targetMode,
                autoRemotePolicy: autoRemotePolicy
            )
            apiKeyDraft = ""
            refreshDraftEndpointKeyStatus()
        } else {
            controller.useLocalLLM()
        }
    }

    private func loadLLMDrafts(force: Bool) {
        guard force || !didLoadLLMDrafts || focusedLLMField == nil else { return }
        llmModeDraft = controller.isAutoLLMActive
            ? "auto"
            : controller.llmMode == "local" ? "local" : controller.externalProviderType
        autoProviderTypeDraft = controller.externalProviderType == "corporate" ? "corporate" : "external"
        autoRemotePolicyDraft = controller.settings["auto_remote_policy"] ?? "local_only"
        externalBaseURLDraft = controller.externalLLMBaseURL
        externalModelDraft = controller.externalLLMModel
        didLoadLLMDrafts = true
        refreshDraftEndpointKeyStatus()
    }

    private var externalContextScope: String {
        controller.settings["external_context_scope"] ?? "task"
    }

    private var shouldShowRemoteFields: Bool {
        llmModeDraft == "corporate" || llmModeDraft == "external"
            || (llmModeDraft == "auto" && autoRemotePolicyDraft == "eligible")
    }

    private var externalContextScopeBinding: Binding<String> {
        Binding(
            get: { externalContextScope },
            set: { controller.setSetting("external_context_scope", $0) }
        )
    }

    private func refreshDraftEndpointKeyStatus() {
        draftEndpointHasStoredKey = controller.hasStoredExternalLLMKey(baseURL: externalBaseURLDraft)
    }

    private func preflightIcon(_ status: String) -> String {
        switch status {
        case "pass": return "checkmark.circle.fill"
        case "warn": return "exclamationmark.triangle.fill"
        case "block": return "xmark.octagon.fill"
        default: return "questionmark.circle.fill"
        }
    }

    private func preflightColor(_ status: String) -> Color {
        switch status {
        case "pass": return RnDTheme.blue
        case "warn": return .orange
        case "block": return RnDTheme.red
        default: return RnDTheme.steel
        }
    }
}

struct SpeechFailureBanner: View {
    let message: String
    let retryable: Bool
    let retryUnavailableReason: String?
    var compact = false
    let onRetry: () -> Void
    let onTextOnly: () -> Void

    @ViewBuilder var body: some View {
        if compact {
            VStack(alignment: .leading, spacing: 4) {
                Label("Ответ готов — озвучивание недоступно", systemImage: "speaker.slash.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(RnDTheme.ink)
                Text(
                    retryUnavailableReason.map { "\(message) · \($0)" }
                        ?? message
                )
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                HStack(spacing: 12) {
                    Button("Повторить озвучивание", action: onRetry)
                        .disabled(!retryable)
                        .accessibilityHint(
                            retryable
                                ? "Повторно воспроизводит последний ответ"
                                : (retryUnavailableReason ?? "Повтор недоступен")
                        )
                    Button("Только текст", action: onTextOnly)
                        .accessibilityHint("Отключает озвучивание следующих ответов")
                    Spacer(minLength: 0)
                }
                .font(.caption2)
                .buttonStyle(.link)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RnDTheme.red.opacity(0.055))
            .help(message)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Ошибка озвучивания. \(message)")
        } else {
            HStack(spacing: 10) {
                Image(systemName: "speaker.slash.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(RnDTheme.red)
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(RnDTheme.red.opacity(0.09)))
                VStack(alignment: .leading, spacing: 2) {
                    Text("Текстовый ответ готов, озвучивание недоступно")
                        .font(.caption.weight(.semibold))
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    if let retryUnavailableReason {
                        Text(retryUnavailableReason)
                            .font(.caption2)
                            .foregroundStyle(RnDTheme.red)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                Button("Повторить озвучивание", action: onRetry)
                    .buttonStyle(.bordered)
                    .disabled(!retryable)
                    .accessibilityHint(
                        retryable
                            ? "Повторно воспроизводит последний ответ"
                            : (retryUnavailableReason ?? "Повтор недоступен")
                    )
                Button("Только текст", action: onTextOnly)
                    .buttonStyle(.borderless)
                    .accessibilityHint("Отключает озвучивание следующих ответов")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RnDTheme.red.opacity(0.045))
            .overlay(alignment: .bottom) { Rectangle().fill(RnDTheme.red.opacity(0.16)).frame(height: 1) }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Ошибка озвучивания. \(message)")
        }
    }
}

struct ComposerSuggestionsView: View {
    @ObservedObject var controller: BackendController
    var compact = false

    private var suggestions: [ComposerSuggestion] { controller.composerSuggestions }

    var body: some View {
        if !suggestions.isEmpty {
            VStack(alignment: .leading, spacing: compact ? 3 : 5) {
                if !compact {
                    Text(suggestions.first?.kind == .skill ? "Скиллы · ↑↓ выбрать · Tab вставить" : "Источники · ↑↓ выбрать · Tab вставить")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(Array(suggestions.enumerated()), id: \.element.id) { index, suggestion in
                            Button { controller.applyComposerSuggestion(suggestion) } label: {
                                HStack(spacing: 5) {
                                    Image(systemName: suggestion.kind == .skill ? "wand.and.stars" : suggestionIcon(suggestion))
                                    VStack(alignment: .leading, spacing: 0) {
                                        Text(suggestion.title).lineLimit(1)
                                        if !compact { Text(suggestion.subtitle).font(.caption2).foregroundStyle(.secondary).lineLimit(1) }
                                    }
                                }
                                .font(compact ? .caption2.weight(.medium) : .caption.weight(.medium))
                                .padding(.horizontal, compact ? 8 : 10)
                                .padding(.vertical, compact ? 4 : 6)
                                .background(Capsule().fill(index == selectedIndex ? RnDTheme.blue.opacity(0.13) : RnDTheme.canvas))
                                .overlay(Capsule().stroke(index == selectedIndex ? RnDTheme.blue : RnDTheme.line))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            .padding(.horizontal, compact ? 0 : 16)
            .padding(.top, compact ? 0 : 8)
        }
    }

    private var selectedIndex: Int {
        guard !suggestions.isEmpty else { return 0 }
        return min(max(controller.composerSuggestionIndex, 0), suggestions.count - 1)
    }

    private func suggestionIcon(_ suggestion: ComposerSuggestion) -> String {
        suggestion.subtitle == "Встреча" ? "person.2.fill" : "doc.text.fill"
    }
}

struct PendingAttachmentsBar: View {
    @ObservedObject var controller: BackendController
    var compact = false

    var body: some View {
        if !controller.pendingAttachments.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(controller.pendingAttachments) { attachment in
                        HStack(spacing: 5) {
                            Image(systemName: "doc.fill").foregroundStyle(RnDTheme.blue)
                            Text(attachment.name).lineLimit(1)
                            Button { controller.removePendingAttachment(attachment.id) } label: {
                                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                            .disabled(controller.isLLMTurnPending)
                            .accessibilityLabel("Убрать вложение «\(attachment.name)»")
                        }
                        .font(compact ? .caption2 : .caption)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Capsule().fill(RnDTheme.blue.opacity(0.08)))
                        .overlay(Capsule().stroke(RnDTheme.blue.opacity(0.24)))
                    }
                }
            }
            .padding(.horizontal, compact ? 0 : 16)
            .padding(.top, compact ? 0 : 7)
        }
    }
}

struct ComposerTextField: View {
    @ObservedObject var controller: BackendController
    let placeholder: String
    var compact = false
    let onSend: () -> Void

    var body: some View {
        TextField(placeholder, text: $controller.composerDraft)
            .textFieldStyle(.plain)
            .font(compact ? .caption : .system(size: 14, design: .rounded))
            .onSubmit {
                if !controller.applySelectedComposerSuggestion() { onSend() }
            }
            .onKeyPress(.tab) {
                controller.applySelectedComposerSuggestion() ? .handled : .ignored
            }
            .onKeyPress(.upArrow) {
                guard !controller.composerSuggestions.isEmpty else { return .ignored }
                controller.moveComposerSuggestion(-1)
                return .handled
            }
            .onKeyPress(.downArrow) {
                guard !controller.composerSuggestions.isEmpty else { return .ignored }
                controller.moveComposerSuggestion(1)
                return .handled
            }
            .onChange(of: controller.composerDraft) { _, _ in
                if controller.composerSuggestionIndex >= controller.composerSuggestions.count {
                    controller.composerSuggestionIndex = 0
                }
            }
            .disabled(!controller.canSendText)
    }
}

struct UniversalComposer: View {
    @ObservedObject var controller: BackendController
    var showQuickActions = true
    @AppStorage("rnd.speakReplies") private var speakReplies = true
    var body: some View {
        VStack(spacing: 0) {
            if let message = controller.speechErrorMessage {
                SpeechFailureBanner(
                    message: message,
                    retryable: controller.canRetrySpeech,
                    retryUnavailableReason: controller.speechRetryUnavailableReason,
                    onRetry: controller.retrySpeech,
                    onTextOnly: {
                        speakReplies = false
                        controller.dismissSpeechError()
                    }
                )
            }
            if showQuickActions && !controller.quickActions.isEmpty {
                QuickActionsBar(
                    actions: controller.quickActions,
                    pendingID: controller.quickActionPendingID,
                    compact: true,
                    isCompleted: controller.isQuickActionCompleted,
                    onAction: controller.performQuickAction
                )
                .padding(.horizontal, 16)
                .padding(.top, 7)
            }
            PendingAttachmentsBar(controller: controller)
            ComposerSuggestionsView(controller: controller)
            HStack(spacing: 10) {
                Button { controller.chooseComposerAttachments() } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless)
                    .disabled(!controller.canSendText)
                    .help("Добавить файлы к следующему запросу")
                    .accessibilityLabel("Добавить файлы")
                    .accessibilityHint("Открывает выбор файлов для следующего сообщения")
                ComposerTextField(
                    controller: controller,
                    placeholder: "Поставьте задачу, используйте /skill или @источник…",
                    onSend: send
                )
                if controller.sttSeconds != nil || controller.firstTokenSeconds != nil || controller.firstAudioSeconds != nil {
                    VStack(alignment: .trailing, spacing: 1) {
                        if let value = controller.firstTokenSeconds { Text("Ответ \(value, specifier: "%.2f")с") }
                        if let value = controller.firstAudioSeconds { Text("Звук \(value, specifier: "%.2f")с") }
                        if controller.firstTokenSeconds == nil, let value = controller.sttSeconds { Text("STT \(value, specifier: "%.2f")с") }
                    }
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .help("Задержка от конца реплики до первого токена и первого звука")
                }
                Toggle(isOn: $speakReplies) {
                    Image(systemName: speakReplies ? "speaker.wave.2.fill" : "speaker.slash.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .frame(width: 27, height: 27)
                        .foregroundStyle(speakReplies ? Color.white : RnDTheme.steel)
                        .background(Circle().fill(speakReplies ? RnDTheme.navy : RnDTheme.canvas))
                }
                .toggleStyle(.button)
                .buttonStyle(.plain)
                .help(speakReplies ? "Озвучивать ответы: включено" : "Озвучивать ответы: выключено")
                .accessibilityLabel("Озвучивать ответы")
                .accessibilityValue(speakReplies ? "Включено" : "Выключено")
                Button { send() } label: { Image(systemName: "arrow.up").font(.system(size: 12, weight: .bold)).frame(width: 27, height: 27).background(Circle().fill(RnDTheme.blue)).foregroundStyle(.white) }
                    .buttonStyle(.plain)
                    .disabled(controller.composerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !controller.canSendText)
                    .accessibilityLabel("Отправить сообщение")
                    .accessibilityHint("Отправляет текст и выбранные файлы ассистенту")
                Button(action: controller.toggleSession) { Image(systemName: controller.isSessionActive ? "stop.fill" : controller.isVoiceStartPending ? "hourglass" : "mic.fill").font(.system(size: 16, weight: .semibold)).frame(width: 38, height: 38).background(Circle().fill(controller.isSessionActive ? RnDTheme.red : RnDTheme.navy)).foregroundStyle(.white) }
                    .buttonStyle(.plain)
                    .disabled(!controller.canToggleVoiceSession)
                    .keyboardShortcut(.space, modifiers: [.command])
                    .help(controller.voiceSessionActionLabel)
                    .accessibilityLabel(controller.voiceSessionActionLabel)
                    .accessibilityHint(controller.voiceSessionActionHint)
                    .accessibilityValue(controller.voiceSessionAccessibilityValue)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .background(RnDTheme.panel)
        .overlay(alignment: .top) { Rectangle().fill(RnDTheme.line).frame(height: 1) }
    }
    private func send() {
        let text = controller.composerDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, controller.canSendText else { return }
        controller.composerDraft = ""
        controller.submit(text: text, speak: speakReplies)
    }
}

struct SourceExcerptPreview: View {
    @Environment(\.dismiss) private var dismiss
    let source: EntityRecord
    let onOpenFile: (EntityRecord) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: source.kind == "meeting" ? "person.2.fill" : "doc.text.magnifyingglass")
                    .font(.title2)
                    .foregroundStyle(RnDTheme.blue)
                VStack(alignment: .leading, spacing: 4) {
                    Text(source.title).font(.title3.bold()).lineLimit(2)
                    Text("Точный фрагмент, использованный помощником")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
            }

            HStack(spacing: 8) {
                if let start = source.charStart, let end = source.charEnd {
                    Label("Символы \(start)–\(end)", systemImage: "selection.pin.in.out")
                }
                if let chunkID = source.chunkID, !chunkID.isEmpty {
                    Label(chunkID, systemImage: "number")
                }
            }
            .font(.caption.monospaced())
            .foregroundStyle(.secondary)

            ScrollView {
                Text(source.excerpt)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
            }
            .background(RoundedRectangle(cornerRadius: 12).fill(RnDTheme.canvas))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(RnDTheme.line))

            HStack {
                Spacer()
                Button("Закрыть", role: .cancel) { dismiss() }
                if source.path?.isEmpty == false {
                    Button {
                        dismiss()
                        onOpenFile(source)
                    } label: {
                        Label("Открыть весь файл", systemImage: "arrow.up.forward.app")
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
        }
        .padding(22)
        .frame(minWidth: 620, minHeight: 420)
        .background(RnDTheme.panel)
        .foregroundStyle(RnDTheme.ink)
        .preferredColorScheme(.light)
    }
}

struct ConversationView: View {
    let messages: [ChatMessage]
    let sources: [EntityRecord]
    let quickActions: [QuickActionRecord]
    let quickActionPendingID: String?
    let isQuickActionCompleted: (QuickActionRecord) -> Bool
    let onQuickAction: (QuickActionRecord) -> Void
    let onOpenSource: (EntityRecord) -> Void
    var body: some View { ScrollViewReader { proxy in ScrollView { LazyVStack(spacing: 10) {
        if messages.isEmpty { EmptyState(icon: "bubble.left.and.bubble.right", title: "Новая задача", detail: "Контекст этой задачи не смешивается с другими.") }
        ForEach(messages) { message in MessageBubble(message: message, onOpenSource: onOpenSource).id(message.id) }
        if !quickActions.isEmpty {
            QuickActionsBar(
                actions: quickActions,
                pendingID: quickActionPendingID,
                compact: false,
                isCompleted: isQuickActionCompleted,
                onAction: onQuickAction
            )
            .frame(maxWidth: .infinity, alignment: .leading)
            .id("quick-actions")
        }
        if !sources.isEmpty { VStack(alignment: .leading, spacing: 6) { Label("Контекст задачи", systemImage: "paperclip").font(.caption).foregroundStyle(.secondary); SourceButtons(sources: sources, onOpen: onOpenSource) }.frame(maxWidth: .infinity, alignment: .leading) }
    }.padding(16) }
        .onChange(of: messages.count) { _, _ in
            if let id = messages.last?.id {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(id, anchor: .bottom) }
            }
        }
        .onChange(of: quickActions.count) { _, count in
            if count > 0 {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("quick-actions", anchor: .bottom) }
            }
        }
    } }
}

struct QuickActionsBar: View {
    let actions: [QuickActionRecord]
    let pendingID: String?
    let compact: Bool
    let isCompleted: (QuickActionRecord) -> Bool
    let onAction: (QuickActionRecord) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 4 : 7) {
            if !compact {
                Label("Что дальше", systemImage: "sparkles")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(actions) { action in
                        let completed = isCompleted(action)
                        Button { onAction(action) } label: {
                            HStack(spacing: 5) {
                                if pendingID == action.id {
                                    ProgressView().controlSize(.mini)
                                } else {
                                    Image(systemName: completed ? "checkmark.circle.fill" : quickActionIcon(action.id))
                                }
                                Text(completed ? "Готово" : action.title).lineLimit(1)
                            }
                            .font(compact ? .caption2.weight(.medium) : .caption.weight(.medium))
                            .padding(.horizontal, compact ? 8 : 10)
                            .padding(.vertical, compact ? 4 : 6)
                            .foregroundStyle(completed ? RnDTheme.blue : RnDTheme.ink)
                            .background(Capsule().fill(completed ? RnDTheme.blue.opacity(0.10) : RnDTheme.canvas))
                            .overlay(Capsule().stroke(completed ? RnDTheme.blue.opacity(0.35) : RnDTheme.line))
                        }
                        .buttonStyle(.plain)
                        .disabled(pendingID != nil || completed)
                        .help(action.title)
                    }
                }
            }
        }
        .padding(compact ? 0 : 10)
        .background(
            compact ? AnyShapeStyle(Color.clear) : AnyShapeStyle(RnDTheme.panel)
        )
        .overlay {
            if !compact { RoundedRectangle(cornerRadius: 12).stroke(RnDTheme.line) }
        }
    }

    private func quickActionIcon(_ id: String) -> String {
        switch id {
        case "save_as_artifact": return "doc.badge.plus"
        case "save_to_memory": return "brain.head.profile"
        case "view_artifact_versions": return "clock.arrow.circlepath"
        default: return "bolt.fill"
        }
    }
}

struct MessageBubble: View {
    let message: ChatMessage
    let onOpenSource: (EntityRecord) -> Void
    var body: some View { HStack {
        if message.role == .user { Spacer(minLength: 70) }
        VStack(alignment: .leading, spacing: 8) { HStack(spacing: 6) { Text(message.role == .user ? "Вы" : "RnD Workbench"); if message.wasInterrupted { Label("перебито", systemImage: "waveform.slash") } }.font(.caption2.weight(.semibold)).foregroundStyle(message.role == .user ? Color.white.opacity(0.76) : RnDTheme.steel); Text(message.text.isEmpty ? "…" : message.text).font(.system(size: 14, design: .rounded)).foregroundStyle(message.role == .user ? Color.white : RnDTheme.ink).textSelection(.enabled); if !message.sources.isEmpty { SourceButtons(sources: message.sources, onOpen: onOpenSource) } }.padding(.horizontal, 13).padding(.vertical, 10).background(RoundedRectangle(cornerRadius: 15).fill(message.role == .user ? RnDTheme.blue : RnDTheme.panel)).overlay(RoundedRectangle(cornerRadius: 15).stroke(message.role == .user ? RnDTheme.blue : RnDTheme.line))
        if message.role == .assistant { Spacer(minLength: 48) }
    } }
}

struct SourceButtons: View {
    let sources: [EntityRecord]
    let onOpen: (EntityRecord) -> Void
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(sources) { source in
                    Button { onOpen(source) } label: {
                        Label(source.title, systemImage: source.kind == "meeting" ? "person.2.fill" : "doc.text.fill")
                            .font(.caption2)
                            .lineLimit(1)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(Capsule().fill(RnDTheme.canvas))
                            .overlay(Capsule().stroke(RnDTheme.line))
                    }
                    .buttonStyle(.plain)
                    .help("Открыть источник «\(source.title)»")
                }
            }
        }
    }
}

struct TaskRow: View {
    let task: TaskRecord
    let action: () -> Void
    var body: some View { Button(action: action) { HStack { VStack(alignment: .leading, spacing: 4) { Text(task.title).lineLimit(2); HStack(spacing: 5) { StatusBadge(status: task.status); ClassificationBadge(value: task.classification) } }; Spacer() }.contentShape(Rectangle()) }.buttonStyle(.plain).padding(.vertical, 3) }
}

struct EntityRow: View {
    let item: EntityRecord
    let icon: String
    var trailing: AnyView? = nil
    var body: some View { HStack(alignment: .top, spacing: 11) {
        Image(systemName: icon).foregroundStyle(.tint).frame(width: 22)
        VStack(alignment: .leading, spacing: 3) { Text(item.title).font(.body.weight(.medium)).lineLimit(2); if !item.subtitle.isEmpty { Text(item.subtitle).font(.caption).foregroundStyle(.secondary) }; if !item.detail.isEmpty { Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(4) } }
            .layoutPriority(1)
        Spacer(); if let trailing { trailing } else if !item.status.isEmpty { Text(item.status).font(.caption2).foregroundStyle(.secondary).lineLimit(1) }
    }.padding(10).background(RoundedRectangle(cornerRadius: 10).fill(RnDTheme.panel)).overlay(RoundedRectangle(cornerRadius: 10).stroke(RnDTheme.line)) }
}

struct CatalogCard: View {
    let item: EntityRecord
    let icon: String
    var body: some View { VStack(alignment: .leading, spacing: 9) {
        HStack { Image(systemName: icon).foregroundStyle(item.status == "not_connected" ? Color.secondary : Color.accentColor); Spacer(); if !item.status.isEmpty { Text(item.status == "connected" ? "Подключено" : item.status == "not_connected" ? "Не подключено" : item.status).font(.caption2).foregroundStyle(.secondary) } }
        Text(item.title).font(.headline); if !item.subtitle.isEmpty { Text(item.subtitle).font(.caption.monospaced()).foregroundStyle(.secondary) }; Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(4); if item.version > 0 { Text("v\(item.version)").font(.caption2).foregroundStyle(.tertiary) }
    }.frame(maxWidth: .infinity, minHeight: 120, alignment: .topLeading).padding(14).background(RoundedRectangle(cornerRadius: 14).fill(RnDTheme.panel)).overlay(RoundedRectangle(cornerRadius: 14).stroke(RnDTheme.line)).shadow(color: RnDTheme.navy.opacity(0.04), radius: 8, y: 3) }
}

struct StatCard: View {
    let title: String; let value: Int; let icon: String; let color: Color; let action: () -> Void
    var body: some View { Button(action: action) { HStack { Image(systemName: icon).font(.title2).foregroundStyle(color); VStack(alignment: .leading) { Text("\(value)").font(.title2.bold().monospacedDigit()); Text(title).font(.caption).foregroundStyle(.secondary) }; Spacer() }.padding(14).background(RoundedRectangle(cornerRadius: 14).fill(RnDTheme.panel)).overlay(RoundedRectangle(cornerRadius: 14).stroke(RnDTheme.line)).shadow(color: RnDTheme.navy.opacity(0.04), radius: 8, y: 3) }.buttonStyle(.plain) }
}

struct StatusBadge: View {
    let status: String
    var body: some View { let label = ["new": "Новая", "running": "Выполняется", "needs_user": "Нужен пользователь", "done": "Готова", "analyzed": "Разобрана", "open": "Открыто", "superseded": "Неактуально", "error": "Ошибка"][status] ?? status; Text(label).font(.caption2.weight(.medium)).padding(.horizontal, 7).padding(.vertical, 2).background(Capsule().fill(color.opacity(0.15))).foregroundStyle(color) }
    private var color: Color { ["done", "analyzed"].contains(status) ? RnDTheme.blue : status == "error" ? RnDTheme.red : status == "running" ? RnDTheme.navy : RnDTheme.steel }
}

struct ClassificationBadge: View {
    let value: String
    private var classification: DataClassification {
        DataClassification(rawValue: value) ?? .internal
    }
    var body: some View {
        Label(classification.shortTitle, systemImage: classification.icon)
            .font(.caption2.weight(.medium))
            .foregroundStyle(classification.color)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(Capsule().fill(classification.color.opacity(0.11)))
    }
}

struct ClassificationPicker: View {
    let title: String
    let value: String
    let onChange: (String) -> Void
    var body: some View {
        Picker(title, selection: Binding(get: { value }, set: onChange)) {
            ForEach(DataClassification.allCases) { classification in
                Label(classification.title, systemImage: classification.icon)
                    .tag(classification.rawValue)
            }
        }
    }
}

struct ClassificationMenu: View {
    let value: String
    let onChange: (String) -> Void
    private var current: DataClassification {
        DataClassification(rawValue: value) ?? .internal
    }
    var body: some View {
        Menu {
            ForEach(DataClassification.allCases) { classification in
                Button {
                    onChange(classification.rawValue)
                } label: {
                    Label(classification.title, systemImage: classification.icon)
                }
            }
        } label: {
            Image(systemName: current.icon)
                .foregroundStyle(current.color)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Классификация: \(current.title)")
    }
}

struct SectionHeader: View {
    let title: String; let actionTitle: String; let action: () -> Void
    init(_ title: String, action: String, perform: @escaping () -> Void) { self.title = title; actionTitle = action; self.action = perform }
    var body: some View { HStack { Text(title).font(.headline); Spacer(); Button(actionTitle, action: action).buttonStyle(.link) } }
}

struct EmptyState: View {
    let icon: String; let title: String; let detail: String
    var body: some View { VStack(spacing: 10) { Image(systemName: icon).font(.system(size: 30)).foregroundStyle(.tertiary); Text(title).font(.headline); Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 430) }.frame(maxWidth: .infinity, minHeight: 150).padding(20) }
}

struct FormSheet<Content: View>: View {
    let title: String; let primary: String; let onCancel: () -> Void; let onPrimary: () -> Void; let content: Content
    init(title: String, primary: String, onCancel: @escaping () -> Void, onPrimary: @escaping () -> Void, @ViewBuilder content: () -> Content) { self.title = title; self.primary = primary; self.onCancel = onCancel; self.onPrimary = onPrimary; self.content = content() }
    var body: some View { VStack(alignment: .leading, spacing: 14) { Text(title).font(.title2.bold()); content; HStack { Spacer(); Button("Отмена", action: onCancel); Button(primary, action: onPrimary).buttonStyle(.borderedProminent) } }.padding(22).frame(width: 440) }
}

@MainActor
private enum AssistantWindowBridge {
    static let identifier = NSUserInterfaceItemIdentifier("rnd-workbench.assistant")
    static var fullFrame: NSRect?

    static var window: NSWindow? {
        NSApp.windows.first { $0.identifier == identifier }
    }

    static func register(_ window: NSWindow) {
        window.identifier = identifier
        window.isReleasedWhenClosed = false
    }

    static func captureFullFrame() {
        guard let window, window.frame.width > 700 else { return }
        fullFrame = window.frame
    }

    static func reveal() {
        DispatchQueue.main.async {
            NSApp.activate(ignoringOtherApps: true)
            window?.makeKeyAndOrderFront(nil)
        }
    }

    static func hide() {
        window?.orderOut(nil)
    }

    static func configure(_ window: NSWindow, for mode: AssistantPresentationMode) {
        register(window)
        window.title = "RnD Workbench"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.hasShadow = true

        switch mode {
        case .compact:
            window.level = .floating
            window.collectionBehavior.formUnion([.canJoinAllSpaces, .fullScreenAuxiliary])
            window.styleMask = [.borderless, .fullSizeContentView]
            window.isMovableByWindowBackground = true
            window.backgroundColor = .clear
            window.isOpaque = false
            window.setContentSize(NSSize(width: 400, height: 238))
            if let screen = window.screen ?? NSScreen.main {
                let frame = screen.visibleFrame
                window.setFrameOrigin(NSPoint(
                    x: frame.maxX - window.frame.width - 22,
                    y: frame.minY + 22
                ))
            }
        case .full:
            window.level = .normal
            window.collectionBehavior.subtract([.canJoinAllSpaces, .fullScreenAuxiliary])
            window.styleMask = [.titled, .closable, .miniaturizable, .fullSizeContentView]
            window.isMovableByWindowBackground = false
            window.backgroundColor = .windowBackgroundColor
            window.isOpaque = true
            if let fullFrame {
                window.setFrame(fullFrame, display: true, animate: true)
            } else {
                window.setContentSize(NSSize(width: 1060, height: 720))
                window.center()
                self.fullFrame = window.frame
            }
        }
        window.makeKeyAndOrderFront(nil)
    }
}

struct AssistantWindowConfigurator: NSViewRepresentable {
    let mode: AssistantPresentationMode

    final class Coordinator {
        var configuredWindow: ObjectIdentifier?
        var configuredMode: AssistantPresentationMode?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> NSView { NSView() }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            let identifier = ObjectIdentifier(window)
            guard context.coordinator.configuredWindow != identifier
                    || context.coordinator.configuredMode != mode else { return }
            context.coordinator.configuredWindow = identifier
            context.coordinator.configuredMode = mode
            AssistantWindowBridge.configure(window, for: mode)
        }
    }
}

struct CompactAssistantView: View {
    @ObservedObject var controller: BackendController
    @AppStorage("rnd.compact.mode") private var modeRaw = CompactMode.voice.rawValue
    @AppStorage("rnd.speakReplies") private var speakReplies = true

    private var mode: CompactMode {
        if controller.isSessionActive || controller.isVoiceStartPending { return .voice }
        if controller.externalDictationActive || controller.externalDictationStartPending
            || controller.externalDictationTranscribing { return .voice }
        return CompactMode(rawValue: modeRaw) ?? .voice
    }
    private var modeBinding: Binding<CompactMode> {
        Binding(
            get: { mode },
            set: { newMode in
                if newMode == .chat { controller.stopVoiceSession() }
                modeRaw = newMode.rawValue
            }
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Rectangle().fill(RnDTheme.line).frame(height: 1)
            if let message = controller.speechErrorMessage {
                SpeechFailureBanner(
                    message: message,
                    retryable: controller.canRetrySpeech,
                    retryUnavailableReason: controller.speechRetryUnavailableReason,
                    compact: true,
                    onRetry: controller.retrySpeech,
                    onTextOnly: {
                        speakReplies = false
                        controller.dismissSpeechError()
                    }
                )
                Rectangle().fill(RnDTheme.line).frame(height: 1)
            }
            Group {
                if mode == .voice { voicePanel } else { chatPanel }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onChange(of: controller.dictationReviewSequence) { _, sequence in
            if sequence > 0 { modeRaw = CompactMode.chat.rawValue }
        }
        .frame(width: 400, height: 238)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(RnDTheme.panel))
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(controller.isRemoteRouteActive ? RnDTheme.red.opacity(0.38) : RnDTheme.line)
        )
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .shadow(color: RnDTheme.navy.opacity(0.18), radius: 22, y: 8)
        .foregroundStyle(RnDTheme.ink)
        .tint(RnDTheme.blue)
        .preferredColorScheme(.light)
        .sheet(
            item: Binding(
                get: { controller.artifactHistoryArtifact },
                set: { if $0 == nil { controller.closeArtifactHistory() } }
            )
        ) { artifact in
            ArtifactHistorySheet(controller: controller, requestedArtifact: artifact)
        }
        .alert("RnD Workbench", isPresented: Binding(get: { controller.errorMessage != nil }, set: { if !$0 { controller.errorMessage = nil } })) { Button("Закрыть", role: .cancel) { controller.errorMessage = nil } } message: { Text(controller.errorMessage ?? "") }
    }

    private var header: some View {
        HStack(spacing: 10) {
            StatusOrb(state: controller.state)
                .scaleEffect(0.78)
                .frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 0) {
                Text("RnD Workbench").font(.system(size: 13, weight: .bold, design: .rounded)).lineLimit(1).minimumScaleFactor(0.78)
                Text(controller.compactActivityLabel).font(.caption2).foregroundStyle(controller.state.color).lineLimit(1)
                Label(
                    controller.compactLLMRouteStatusLabel,
                    systemImage: controller.actualLLMRouteIcon
                )
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(controller.isRemoteRouteActive ? RnDTheme.red : RnDTheme.blue)
                .lineLimit(1)
                .help(controller.routeStatusHelp)
                .accessibilityLabel("Фактический маршрут: \(controller.actualLLMRouteStatusLabel)")
            }
            .frame(width: 124, alignment: .leading)
            .layoutPriority(1)
            Spacer(minLength: 0)
            Picker("", selection: modeBinding) {
                ForEach(CompactMode.allCases) { item in Text(item.title).tag(item) }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .frame(width: 112)
            Button { controller.presentFull() } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                .buttonStyle(.borderless)
                .help("Развернуть")
                .accessibilityLabel("Развернуть в полное окно")
                .accessibilityHint("Переключает это же окно на полный интерфейс")
            Button { controller.hideAssistantWindow() } label: { Image(systemName: "xmark") }
                .buttonStyle(.borderless)
                .help("Скрыть")
                .accessibilityLabel("Скрыть окно RnD Workbench")
                .accessibilityHint(
                    controller.isSessionActive || controller.isVoiceStartPending
                        ? "Останавливает голосовой режим и скрывает окно"
                        : "Скрывает компактный интерфейс; открыть его снова можно из строки меню"
                )
        }
        .padding(.horizontal, 14)
        .frame(height: 52)
        .background(
            ZStack(alignment: .trailing) {
                RnDTheme.panel
                if controller.isRemoteRouteActive { RnDTheme.red.opacity(0.028) }
                WorkbenchSupergraphic().frame(width: 118).opacity(0.13)
            }
        )
    }

    private var voicePanel: some View {
        HStack(spacing: 18) {
            Button(action: controller.toggleSession) {
                ZStack {
                    Circle().fill(controller.isSessionActive ? RnDTheme.red.opacity(0.12) : RnDTheme.blue.opacity(0.10)).frame(width: 104, height: 104)
                    Circle().fill(controller.isSessionActive ? RnDTheme.red : RnDTheme.navy).frame(width: 68, height: 68).shadow(color: controller.state.color.opacity(0.28), radius: 12)
                    Image(systemName: controller.isSessionActive ? "stop.fill" : controller.isVoiceStartPending ? "hourglass" : "mic.fill").font(.system(size: 25, weight: .semibold)).foregroundStyle(.white)
                }
            }
            .buttonStyle(.plain)
            .disabled(!controller.canToggleVoiceSession)
            .help(controller.voiceSessionActionLabel)
            .accessibilityLabel(controller.voiceSessionActionLabel)
            .accessibilityHint(controller.voiceSessionActionHint)
            .accessibilityValue(controller.voiceSessionAccessibilityValue)
            VStack(alignment: .leading, spacing: 8) {
                Text(
                    controller.externalDictationActive
                        ? "Диктовка в активное поле"
                        : controller.externalDictationStartPending
                            ? "Запускаю глобальную диктовку…"
                            : controller.externalDictationTranscribing
                                ? "Распознаю диктовку…"
                            : controller.isSessionActive
                                ? "Голосовой режим включён"
                                : controller.isVoiceStartPending ? "Запускаю микрофон…" : "Начать разговор"
                )
                .font(.headline)
                Text(
                    controller.externalDictationActive || controller.externalDictationStartPending
                        || controller.externalDictationTranscribing
                        ? (controller.externalDictationStatus ?? "Отпустите правую ⌥, чтобы распознать и вставить текст.")
                        : controller.isSessionActive
                        ? "Говорите естественно — помощника можно перебить."
                        : controller.isVoiceStartPending
                            ? "Подготавливаю локальное распознавание речи."
                            : controller.globalPushToTalkReady
                                ? "Микрофон — разговор. Правая ⌥ — диктовка в активное поле."
                                : "Нажмите микрофон. Аудио и распознавание остаются на устройстве."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                Label(controller.isReady ? "Модели готовы" : "Загрузка \(controller.loadedModels.count)/3", systemImage: controller.isReady ? "checkmark.circle.fill" : "clock").font(.caption2.weight(.medium)).foregroundStyle(controller.isReady ? RnDTheme.blue : RnDTheme.steel)
                if controller.firstTokenSeconds != nil || controller.firstAudioSeconds != nil {
                    HStack(spacing: 8) {
                        if let value = controller.firstTokenSeconds { Text("Ответ \(value, specifier: "%.2f")с") }
                        if let value = controller.firstAudioSeconds { Text("Звук \(value, specifier: "%.2f")с") }
                    }
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .help("Задержка до первого токена и первого звука")
                    .accessibilityElement(children: .combine)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 22)
    }

    private var chatPanel: some View {
        VStack(spacing: 7) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        if controller.messages.isEmpty {
                            Text("Задайте вопрос — ответ появится здесь.").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        ForEach(controller.messages.suffix(4)) { message in
                            HStack(alignment: .top, spacing: 7) {
                                Image(systemName: message.role == .user ? "person.crop.circle.fill" : "sparkles").foregroundStyle(message.role == .user ? RnDTheme.steel : RnDTheme.blue).frame(width: 16)
                                Text(message.text.isEmpty ? "…" : message.text).font(.caption).lineLimit(3).textSelection(.enabled)
                            }
                            .id(message.id)
                        }
                    }
                }
                .onChange(of: controller.messages.count) { _, _ in
                    if let id = controller.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                }
                .onChange(of: controller.messages.last?.text) { _, _ in
                    if let id = controller.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                }
            }
            .frame(height: compactConversationHeight)
            PendingAttachmentsBar(controller: controller, compact: true)
            ComposerSuggestionsView(controller: controller, compact: true)
            HStack(spacing: 8) {
                Button { controller.chooseComposerAttachments() } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless)
                    .disabled(!controller.canSendText)
                    .help("Добавить файлы к следующему запросу")
                    .accessibilityLabel("Добавить файлы")
                    .accessibilityHint("Открывает выбор файлов для следующего сообщения")
                ComposerTextField(
                    controller: controller,
                    placeholder: "Сообщение, /скилл или @источник…",
                    compact: true,
                    onSend: send
                )
                Toggle(isOn: $speakReplies) {
                    Image(systemName: speakReplies ? "speaker.wave.2.fill" : "speaker.slash.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .frame(width: 26, height: 26)
                        .foregroundStyle(speakReplies ? Color.white : RnDTheme.steel)
                        .background(Circle().fill(speakReplies ? RnDTheme.navy : RnDTheme.panel))
                }
                .toggleStyle(.button)
                .buttonStyle(.plain)
                .help(speakReplies ? "Озвучивать ответы: включено" : "Озвучивать ответы: выключено")
                .accessibilityLabel("Озвучивать ответы")
                .accessibilityValue(speakReplies ? "Включено" : "Выключено")
                Button { send() } label: { Image(systemName: "arrow.up").font(.system(size: 11, weight: .bold)).frame(width: 28, height: 28).background(Circle().fill(RnDTheme.blue)).foregroundStyle(.white) }
                    .buttonStyle(.plain)
                    .disabled(controller.composerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !controller.canSendText)
                    .accessibilityLabel("Отправить сообщение")
                    .accessibilityHint("Отправляет текст и выбранные файлы ассистенту")
            }
            .padding(.leading, 12).padding(.trailing, 6).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 12).fill(RnDTheme.canvas))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(RnDTheme.line))
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private var compactConversationHeight: CGFloat {
        if controller.speechErrorMessage != nil { return 34 }
        if !controller.composerSuggestions.isEmpty || !controller.pendingAttachments.isEmpty { return 48 }
        return 104
    }

    private func send() {
        let text = controller.composerDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, controller.canSendText else { return }
        controller.composerDraft = ""
        controller.submit(text: text, speak: speakReplies)
    }
}

struct AssistantRootView: View {
    @ObservedObject var controller: BackendController

    var body: some View {
        Group {
            if controller.presentationMode == .compact {
                CompactAssistantView(controller: controller)
            } else {
                AssistantWorkspaceView(controller: controller)
            }
        }
        .background(AssistantWindowConfigurator(mode: controller.presentationMode))
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            controller.refreshGlobalPushToTalkPermissions()
        }
    }
}

struct MenuBarContent: View {
    @ObservedObject var controller: BackendController
    var body: some View {
        Button("Компактный режим") {
            controller.presentCompact()
        }
        .keyboardShortcut("m", modifiers: [.command, .shift])
        Button("Открыть RnD Workbench") {
            controller.presentFull()
        }
        Divider()
        Button(
            controller.isSessionActive || controller.isVoiceStartPending
                ? "Остановить"
                : "Начать слушать"
        ) { controller.toggleSession() }
            .disabled(!controller.canToggleVoiceSession)
        Button("Новая задача") {
            controller.newTask()
            controller.presentFull()
        }
        Divider()
        Button("Завершить") { controller.shutdown(); NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) { NSApp.setActivationPolicy(.accessory); NSApp.activate(ignoringOtherApps: true) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

#if ENDPOINT_SELF_TEST
@main
struct EndpointCanonicalizationSelfTest {
    static func main() {
        let equivalentInputs: [(String, String)] = [
            ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
            ("http://127.0.0.1:8080//v1///", "http://127.0.0.1:8080/v1"),
            ("http://127.1.2.3:80/v1/chat/completions", "http://127.1.2.3/v1"),
            ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
            ("http://[::ffff:127.0.0.1]:80/v1", "http://[::ffff:127.0.0.1]/v1"),
            ("https://API.Example.com./v1//chat/completions/", "https://api.example.com/v1"),
            ("https://api.example.com//V1//CHAT/COMPLETIONS/", "https://api.example.com/V1"),
            ("https://api.example.com:443/chat/completions", "https://api.example.com"),
            ("https://faß.de/v1", "https://fass.de/v1"),
            ("https://MÜNICH.example./v1", "https://xn--mnich-kva.example/v1"),
            ("https://xn--fa-hia.de/v1", "https://xn--fa-hia.de/v1"),
            ("https://api.example.com", "https://api.example.com"),
        ]
        for (raw, expected) in equivalentInputs {
            precondition(
                ExternalLLMEndpoint.canonicalized(raw) == expected,
                "Canonicalization mismatch for \(raw)"
            )
        }
        let rejectedInputs = [
            "https://./v1",
            "https://../v1",
            "https://.../v1",
            "http://api.example.com/v1",
            "https://user:password@api.example.com/v1",
            "https://api.example.com/v1?key=secret",
        ]
        for raw in rejectedInputs {
            precondition(ExternalLLMEndpoint.canonicalized(raw) == nil, "Unsafe URL accepted: \(raw)")
        }
        print("Endpoint canonicalization self-test: \(equivalentInputs.count + rejectedInputs.count) passed")
    }
}
#else
@main
struct LocalVoiceAssistantApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var controller = BackendController()
    var body: some Scene {
        Window("RnD Workbench", id: "assistant") {
            AssistantRootView(controller: controller)
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
        .defaultPosition(.center)
        MenuBarExtra { MenuBarContent(controller: controller) } label: { Image(systemName: controller.state.symbol) }.menuBarExtraStyle(.menu)
    }
}
#endif

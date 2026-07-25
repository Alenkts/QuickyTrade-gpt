import SwiftUI

@main
struct QuickyTradeMacApp: App {
    @StateObject private var model = MacDeskModel()
    var body: some Scene {
        WindowGroup { MacDeskView().environmentObject(model).frame(minWidth: 1080, minHeight: 680) }
        Settings { MacSettingsView().environmentObject(model).frame(width: 440, height: 260) }
    }
}

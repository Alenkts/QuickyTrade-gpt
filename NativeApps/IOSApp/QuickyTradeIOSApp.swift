import SwiftUI

@main
struct QuickyTradeIOSApp: App {
    @StateObject private var model = PhoneDeskModel()
    var body: some Scene { WindowGroup { PhoneRootView().environmentObject(model) } }
}

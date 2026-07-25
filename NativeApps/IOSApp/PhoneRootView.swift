import SwiftUI

struct PhoneRootView: View {
    @EnvironmentObject var model: PhoneDeskModel
    var body: some View {
        NavigationStack {
            Group {
                if model.connection.hasPrefix("Encrypted") { PhoneDashboard() }
                else { pairing }
            }
            .navigationTitle("QuickyTrade")
        }
    }

    private var pairing: some View {
        ContentUnavailableView {
            Label("Connect to your Mac", systemImage: "macbook.and.iphone")
        } description: {
            Text("Enter the pairing code shown in QuickyTrade settings on your Mac. Both devices must be nearby with Wi-Fi and Bluetooth enabled.")
        } actions: {
            TextField("6-digit code", text: $model.pairingCode).keyboardType(.numberPad).textContentType(.oneTimeCode).multilineTextAlignment(.center).font(.title2.monospaced()).frame(width: 180)
            Button("Pair securely") { model.pair() }.buttonStyle(.borderedProminent)
            Text(model.connection).font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct PhoneDashboard: View {
    @EnvironmentObject var model: PhoneDeskModel
    var body: some View {
        List {
            Section("IB Gateway") {
                LabeledContent("Status", value: model.snapshot.connected ? "Live" : "Disconnected")
                LabeledContent("Buying power") { Text(model.snapshot.buyingPower, format: .currency(code: "USD")) }
                LabeledContent("Net liquidation") { Text(model.snapshot.netLiquidation, format: .currency(code: "USD")) }
                LabeledContent("Updated") { Text(model.snapshot.updatedAt, style: .time) }
            }
            Section("Positions") {
                if model.snapshot.positions.isEmpty { Text("No broker positions").foregroundStyle(.secondary) }
                ForEach(model.snapshot.positions) { p in
                    VStack(alignment: .leading) {
                        Text(p.description).font(.headline.monospaced())
                        HStack { Text("Qty \(p.quantity, format: .number)"); Spacer(); Text(p.unrealizedPnL, format: .currency(code: "USD")).foregroundStyle(p.unrealizedPnL >= 0 ? .green : .red) }
                    }
                }
            }
            Section("Working orders") {
                if model.snapshot.orders.isEmpty { Text("No working orders").foregroundStyle(.secondary) }
                ForEach(model.snapshot.orders) { o in
                    VStack(alignment: .leading) { Text(o.description); Text("\(o.status) · \(o.filled, format: .number)/\(o.quantity, format: .number)").font(.caption).foregroundStyle(.secondary) }
                }
            }
            Section { Text("Order entry from iPhone is sent as a proposal. Final transmission requires review and confirmation on the paired Mac.").font(.caption).foregroundStyle(.secondary) }
        }.refreshable { }
    }
}

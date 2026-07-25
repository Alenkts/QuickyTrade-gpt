import SwiftUI

struct MacDeskView: View {
    @EnvironmentObject var model: MacDeskModel
    var body: some View {
        NavigationSplitView {
            List(Underlying.allCases, selection: $model.selected) { symbol in Text(symbol.rawValue).font(.system(.headline, design: .monospaced)) }
                .navigationTitle("0DTE Desk")
        } detail: {
            VStack(spacing: 0) {
                HStack { Circle().fill(.orange).frame(width: 9); Text(model.bridgeStatus); Spacer(); Text("BROKER VALUES UNAVAILABLE").monospaced() }.padding().background(.bar)
                HSplitView {
                    Form {
                        Picker("Right", selection: $model.right) { Text("CALL").tag(OptionRight.call); Text("PUT").tag(OptionRight.put) }.pickerStyle(.segmented)
                        Picker("Order", selection: $model.orderType) { Text("LIMIT").tag(DeskOrderType.limit) }.pickerStyle(.segmented)
                        Stepper("Quantity: \(model.quantity)", value: $model.quantity, in: 1...1)
                        if model.orderType == .limit { TextField("Limit price", value: $model.limitPrice, format: .number.precision(.fractionLength(2))) }
                        Text("Native order entry is retired. Use the IBKR-only web control center and durable TradingView pipeline.").font(.caption).foregroundStyle(.secondary)
                        Button("Review order") {}.buttonStyle(.borderedProminent).disabled(true)
                    }.padding().frame(minWidth: 340)
                    VStack(alignment: .leading) {
                        Text("POSITIONS").font(.caption).foregroundStyle(.secondary)
                        Table(model.snapshot.positions) {
                            TableColumn("Contract", value: \.description)
                            TableColumn("Qty") { Text($0.quantity, format: .number) }
                            TableColumn("Average") { Text($0.averageCost, format: .currency(code: "USD")) }
                            TableColumn("P&L") { Text($0.unrealizedPnL, format: .currency(code: "USD")) }
                        }
                        Text("ORDERS").font(.caption).foregroundStyle(.secondary)
                        Table(model.snapshot.orders) {
                            TableColumn("Order", value: \.description)
                            TableColumn("Status", value: \.status)
                            TableColumn("Filled") { Text($0.filled, format: .number) }
                        }
                    }.padding()
                }
            }
        }.task { model.start() }
    }
}

struct MacSettingsView: View {
    @EnvironmentObject var model: MacDeskModel
    var body: some View {
        Form {
            LabeledContent("Broker execution") { Text(model.bridgeStatus) }
            LabeledContent("Supported client") { Text("Web control center").monospaced() }
            LabeledContent("iPhone pairing code") { Text(model.pairingCode).font(.title2.monospaced().bold()).privacySensitive() }
            LabeledContent("Companion") { Text(model.companion.status) }
            Text("Pairing is encrypted and requires both devices on the local network.").font(.caption).foregroundStyle(.secondary)
        }.padding()
    }
}

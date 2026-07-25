import Foundation
import SwiftUI

@MainActor
final class MacDeskModel: ObservableObject {
    @Published var snapshot = AccountSnapshot()
    @Published var selected = Underlying.SPY
    @Published var right = OptionRight.call
    @Published var orderType = DeskOrderType.limit
    @Published var quantity = 1
    @Published var limitPrice = 0.0
    @Published var pairingCode = String(format: "%06d", Int.random(in: 0...999_999))
    @Published var pendingPhoneOrder: OrderIntent?
    @Published var bridgeStatus = "Legacy native broker bridge retired"

    lazy var companion = CompanionTransport(role: .macHost, pairingCode: pairingCode)
    private var broadcastTask: Task<Void, Never>?

    func start() {
        guard broadcastTask == nil else { return }
        bridgeStatus = "Use the IBKR-only web control center"
        companion.start()
        broadcastTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                try? self.companion.send(.snapshot(self.snapshot))
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    func stop() {
        broadcastTask?.cancel()
        broadcastTask = nil
        bridgeStatus = "Legacy native broker bridge retired"
    }

    func place(_ intent: OrderIntent) {
        pendingPhoneOrder = intent
        bridgeStatus = "Order proposal blocked: use the durable TradingView pipeline"
    }

    func cancel(orderID: Int) {
        bridgeStatus = "Cancel proposal blocked: manage the broker order in TWS"
    }
}

import Foundation
import SwiftUI
import Combine

@MainActor
final class PhoneDeskModel: ObservableObject {
    @Published var pairingCode = ""
    @Published var snapshot = AccountSnapshot()
    @Published var connection = "Not paired"
    @Published var proposedOrder: OrderIntent?
    private var transport: CompanionTransport?
    private var cancellables: Set<AnyCancellable> = []

    func pair() {
        guard pairingCode.count == 6 else { connection = "Enter the 6-digit Mac code"; return }
        let t = CompanionTransport(role: .phoneClient, pairingCode: pairingCode)
        transport = t
        t.$status.receive(on: RunLoop.main).sink { [weak self] in self?.connection = $0 }.store(in: &cancellables)
        t.$lastMessage.compactMap { $0 }.receive(on: RunLoop.main).sink { [weak self] message in
            if case let .snapshot(value) = message { self?.snapshot = value }
        }.store(in: &cancellables)
        t.start()
    }

    func sendProposal(_ intent: OrderIntent) {
        do { try transport?.send(.orderProposal(intent)); proposedOrder = intent }
        catch { connection = error.localizedDescription }
    }
}

import Foundation
@preconcurrency import MultipeerConnectivity

final class CompanionTransport: NSObject, ObservableObject {
    enum Role { case macHost, phoneClient }
    @Published private(set) var peers: [MCPeerID] = []
    @Published private(set) var status = "Disconnected"
    @Published var lastMessage: CompanionMessage?

    private let service = "quickytrade"
    private let peer = MCPeerID(displayName: String(ProcessInfo.processInfo.hostName.prefix(48)))
    private let session: MCSession
    private var advertiser: MCNearbyServiceAdvertiser?
    private var browser: MCNearbyServiceBrowser?
    private let role: Role
    private let pairingCode: String

    init(role: Role, pairingCode: String) {
        self.role = role
        self.pairingCode = pairingCode
        session = MCSession(peer: peer, securityIdentity: nil, encryptionPreference: .required)
        super.init()
        session.delegate = self
    }

    func start() {
        switch role {
        case .macHost:
            advertiser = MCNearbyServiceAdvertiser(peer: peer, discoveryInfo: ["desk": "1"], serviceType: service)
            advertiser?.delegate = self
            advertiser?.startAdvertisingPeer()
            status = "Advertising securely"
        case .phoneClient:
            browser = MCNearbyServiceBrowser(peer: peer, serviceType: service)
            browser?.delegate = self
            browser?.startBrowsingForPeers()
            status = "Searching for Mac"
        }
    }

    func send(_ message: CompanionMessage) throws {
        guard !session.connectedPeers.isEmpty else { throw URLError(.notConnectedToInternet) }
        try session.send(JSONEncoder.desk.encode(message), toPeers: session.connectedPeers, with: .reliable)
    }
}

extension CompanionTransport: MCSessionDelegate {
    nonisolated func session(_ session: MCSession, peer peerID: MCPeerID, didChange state: MCSessionState) {
        Task { @MainActor in self.peers = session.connectedPeers; self.status = state == .connected ? "Encrypted · \(peerID.displayName)" : state == .connecting ? "Pairing…" : "Disconnected" }
    }
    nonisolated func session(_ session: MCSession, didReceive data: Data, fromPeer peerID: MCPeerID) { if let m = try? JSONDecoder.desk.decode(CompanionMessage.self, from: data) { Task { @MainActor in self.lastMessage = m } } }
    nonisolated func session(_ session: MCSession, didReceive stream: InputStream, withName streamName: String, fromPeer peerID: MCPeerID) {}
    nonisolated func session(_ session: MCSession, didStartReceivingResourceWithName resourceName: String, fromPeer peerID: MCPeerID, with progress: Progress) {}
    nonisolated func session(_ session: MCSession, didFinishReceivingResourceWithName resourceName: String, fromPeer peerID: MCPeerID, at localURL: URL?, withError error: Error?) {}
}

extension CompanionTransport: MCNearbyServiceAdvertiserDelegate, MCNearbyServiceBrowserDelegate {
    nonisolated func advertiser(_ advertiser: MCNearbyServiceAdvertiser, didReceiveInvitationFromPeer peerID: MCPeerID, withContext context: Data?, invitationHandler: @escaping (Bool, MCSession?) -> Void) { let supplied = context.flatMap { String(data: $0, encoding: .utf8) }; invitationHandler(supplied == pairingCode, supplied == pairingCode ? session : nil) }
    nonisolated func advertiser(_ advertiser: MCNearbyServiceAdvertiser, didNotStartAdvertisingPeer error: Error) { Task { @MainActor in self.status = error.localizedDescription } }
    nonisolated func browser(_ browser: MCNearbyServiceBrowser, foundPeer peerID: MCPeerID, withDiscoveryInfo info: [String : String]?) { browser.invitePeer(peerID, to: session, withContext: pairingCode.data(using: .utf8), timeout: 20) }
    nonisolated func browser(_ browser: MCNearbyServiceBrowser, lostPeer peerID: MCPeerID) {}
    nonisolated func browser(_ browser: MCNearbyServiceBrowser, didNotStartBrowsingForPeers error: Error) { Task { @MainActor in self.status = error.localizedDescription } }
}

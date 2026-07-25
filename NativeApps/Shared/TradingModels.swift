import Foundation

enum Underlying: String, Codable, CaseIterable, Identifiable, Sendable {
    case SPY, QQQ, IWM, TSLA
    var id: String { rawValue }
}

enum OptionRight: String, Codable, CaseIterable, Sendable { case call = "C", put = "P" }
enum DeskOrderType: String, Codable, CaseIterable, Sendable { case market = "MKT", limit = "LMT" }

struct LiveQuote: Codable, Identifiable, Sendable {
    let conID: Int
    let symbol: String
    let bid: Double?
    let ask: Double?
    let last: Double?
    let delta: Double?
    let timestamp: Date
    var id: Int { conID }
    var mid: Double? { guard let bid, let ask else { return last }; return (bid + ask) / 2 }
}

struct BrokerPosition: Codable, Identifiable, Sendable {
    let conID: Int
    let description: String
    let quantity: Double
    let averageCost: Double
    let marketPrice: Double
    let unrealizedPnL: Double
    let realizedPnL: Double
    var id: Int { conID }
}

struct BrokerOrder: Codable, Identifiable, Sendable {
    let orderID: Int
    let conID: Int
    let description: String
    let quantity: Double
    let filled: Double
    let status: String
    let limitPrice: Double?
    var id: Int { orderID }
}

struct Execution: Codable, Identifiable, Sendable {
    let executionID: String
    let orderID: Int
    let conID: Int
    let side: String
    let quantity: Double
    let price: Double
    let time: Date
    var id: String { executionID }
}

struct AccountSnapshot: Codable, Sendable {
    var connected = false
    var account = ""
    var buyingPower = 0.0
    var netLiquidation = 0.0
    var dailyPnL = 0.0
    var quotes: [String: LiveQuote] = [:]
    var positions: [BrokerPosition] = []
    var orders: [BrokerOrder] = []
    var executions: [Execution] = []
    var updatedAt = Date()
    var error: String?
}

struct OrderIntent: Codable, Sendable {
    let requestID: UUID
    let conID: Int
    let symbol: Underlying
    let right: OptionRight
    let quantity: Int
    let type: DeskOrderType
    let limitPrice: Double?
    let takeProfitPercents: [Double]
    let stopPercent: Double
}

enum CompanionMessage: Codable, Sendable {
    case snapshot(AccountSnapshot)
    case orderProposal(OrderIntent)
    case cancelProposal(orderID: Int, requestID: UUID)
    case acknowledgement(requestID: UUID, accepted: Bool, message: String)
    case ping(Date)
}

extension JSONEncoder {
    static var desk: JSONEncoder { let e = JSONEncoder(); e.dateEncodingStrategy = .iso8601; return e }
}
extension JSONDecoder {
    static var desk: JSONDecoder { let d = JSONDecoder(); d.dateDecodingStrategy = .iso8601; return d }
}

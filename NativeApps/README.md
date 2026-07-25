# QuickyTrade Native Apps (legacy, execution retired)

These SwiftUI artifacts are not a supported broker-order path in the current
TradingView slice. Their former direct Python bridge is fail-closed so it cannot
open a second IBKR session or bypass durable intent persistence. Use the root
web control center plus `core/` for all supported IBKR paper submissions.

Two native SwiftUI targets share broker models and an encrypted companion protocol:

- **QuickyTradeMac** is a legacy read-only/proposal shell and does not connect to IBKR.
- **QuickyTradeIOS** pairs with the Mac using encrypted Apple Multipeer Connectivity. It never stores IBKR credentials or directly contacts IBKR.

## Generate the Xcode project

```bash
cd NativeApps
xcodegen generate
open QuickyTradeNative.xcodeproj
```

## IB Gateway paper setup

1. Install and run IB Gateway, then sign into the paper account.
2. Confirm API socket port `4002`, localhost connections, and disable read-only mode only when ready to submit paper orders.
3. Install IBKR's official TWS API package and its Python `ibapi` module. Do not substitute an unofficial trading wrapper.
4. Start the root `core/` service and web control center. The native target is
   not part of the supported execution topology.

To use another configured port when launching the Mac target, set the `IB_GATEWAY_PORT` environment variable in the Xcode scheme.

## Companion security model

- Multipeer sessions require encryption.
- Invitations require the six-digit code displayed by the Mac app.
- IBKR credentials remain exclusively in IB Gateway.
- The iPhone receives broker snapshots and can send proposals; final live order transmission should require confirmation on macOS.
- Use paper trading until contract resolution, order acknowledgements, partial fills, bracket/OCA exits, reconnect behavior, and API errors have been validated.

## Current implementation boundary

The legacy bridge is retired. The native UI intentionally shows broker values
as unavailable and blocks order/cancel proposals. A future native client must
consume the private core API; it must not own another TWS socket connection.

# Graph Report - .  (2026-07-24)

## Corpus Check
- 88 files · ~104,866 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1469 nodes · 3670 edges · 89 communities (69 shown, 20 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 444 edges (avg confidence: 0.54)
- Token cost: 245,109 input · 0 output

## Community Hubs (Navigation)
- IBKR Execution Engine
- Operator Dashboard Frontend (app.js)
- Broker-Neutral Domain Contracts
- Management Transition State Machine
- Node Server & API Routes
- Execution Ledger
- Official IBAPI Transport
- Submission Registry
- Core Config & HTTP Service
- Reduce-Only Close Tests
- Agent Routing & Safety Rules
- Execution Ledger Tests
- Read Endpoint Tests
- TradingView Webhook Ingress
- Execution Contract Tests
- Operator SQLite Store
- Companion Transport (MultipeerConnectivity)
- Native Trading Models (Swift)
- Alert Timeline & Submission Store
- BrokerTransport Interface
- Option Strike Selection
- App-Core Response Contract & UI Panels
- Desktop Shell & Requirement Docs
- Tauri Desktop Runtime (Rust)
- Protection Ledger
- Protection Order Evidence
- Protection Placement Tests
- Management Policy Tests
- Fake Broker Transport Fixture
- Native Mac Desk UI
- Native App Entrypoints
- Core HTTP Request Handler
- Protection Test Fixture
- Broker Reconciliation Sweep
- Management Contract Parsing
- Electron Desktop Shell
- Tauri Desktop Capability Schema
- Tauri Desktop Schema Definitions
- Tauri macOS Capability Schema
- Tauri macOS Schema Definitions
- Target-Range Strike Tests
- NPM Scripts
- Tauri Desktop Permissions Schema
- Tauri macOS Permissions Schema
- Build Tooling Dependencies
- Alert Processor & Redaction
- Largest-Remainder Allocation
- Execution Engine Cross-Doc Concepts
- Companion Message Protocol
- Tauri Desktop Windows/Webviews Schema
- Tauri macOS Windows/Webviews Schema
- Electron-Builder macOS Config
- Electron Packaged Files List
- Tauri Desktop Capability Remote URLs
- Tauri macOS Capability Remote URLs
- Package Metadata
- Webhook Auth Verification
- Run-Desktop Driver Script (.agents)
- Run-Desktop Driver Script (.claude)
- Tauri Desktop Capability Definition
- Tauri macOS Capability Definition
- HTTP Server Tests
- Protection DB Migration
- App-Managed Execution Flag (Docs Conflict)
- Tauri Desktop Schema Root
- Tauri macOS Schema Root
- Tauri Desktop Local Permission Default
- Tauri macOS Local Permission Default
- Deliberate Scope Limits
- Desktop Renderer Security Boundary
- Test Alert Script
- Core Safety Contract
- Chain/Expiry Selection Spec
- 0DTE Open Design Decisions
- Electron Overlay Profile
- Desktop Preload Bridge
- Webhook Ingress Port Config
- Paper Trading Daily Checklist
- Manual Recovery Procedure
- TradingView Ingress Pipeline (doc)
- Live Trading Opt-In Requirements
- Safety Invariants List
- Webhook Auth Alternatives
- Python Core Package Metadata

## God Nodes (most connected - your core abstractions)
1. `ExecutionEngine` - 102 edges
2. `QualifiedContract` - 91 edges
3. `OfficialIbapiTransport` - 89 edges
4. `ExecutionTests` - 85 edges
5. `ExecutionLedger` - 70 edges
6. `BrokerAcknowledgement` - 56 edges
7. `SubmissionRegistry` - 53 edges
8. `ProtectionLedger` - 51 edges
9. `Quote` - 48 edges
10. `CoreConfig` - 46 edges

## Surprising Connections (you probably didn't know these)
- `Tauri v2 macOS desktop shell` --semantically_similar_to--> `QuickyTradeNative (xcodegen project)`  [INFERRED] [semantically similar]
  docs/TAURI_MACOS_DESKTOP.md → NativeApps/project.yml
- `quickytrade-pre-merge Pre-Merge Skill (.agents)` --semantically_similar_to--> `ibkr-evaluator Agent`  [INFERRED] [semantically similar]
  .agents/skills/quickytrade-pre-merge/SKILL.md → .claude/agents/ibkr-evaluator.md
- `quickytrade-pre-merge Pre-Merge Skill (.agents)` --semantically_similar_to--> `quickytrade-pre-merge Pre-Merge Skill (.claude)`  [INFERRED] [semantically similar]
  .agents/skills/quickytrade-pre-merge/SKILL.md → .claude/skills/quickytrade-pre-merge/SKILL.md
- `run-desktop Skill (.agents)` --semantically_similar_to--> `run-desktop Skill (.claude)`  [INFERRED] [semantically similar]
  .agents/skills/run-desktop/SKILL.md → .claude/skills/run-desktop/SKILL.md
- `No Live-Trading Automation Rule` --semantically_similar_to--> `Fail-Closed Legacy Python Bridge`  [INFERRED] [semantically similar]
  AGENTS.md → NativeApps/README.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **AGENTS.md Specialist Delegation Routing** — agents_agent_routing, claude_agents_ibkr_architect_ibkr_architect, claude_agents_ibkr_end_user_ibkr_end_user, claude_agents_ibkr_evaluator_ibkr_evaluator, claude_agents_ibkr_programmer_ibkr_programmer, claude_agents_ibkr_solution_designer_ibkr_solution_designer, claude_agents_ibkr_trade_analyst_ibkr_trade_analyst [EXTRACTED 0.95]
- **Non-Negotiable Safety Invariants Set** — agents_persist_before_broker_side_effect_rule, agents_duplicate_alert_dedup_rule, agents_submission_unknown_rule, agents_no_fabricated_quotes_rule, agents_reduce_only_exits_rule, agents_fail_closed_on_stale_evidence_rule, agents_no_live_trading_automation_rule, agents_never_render_missing_as_zero_rule [EXTRACTED 1.00]
- **Pre-Merge Checklists Implement AGENTS.md Rules** — agents_skills_quickytrade_pre_merge_skill_pre_merge_skill, claude_skills_quickytrade_pre_merge_skill_pre_merge_skill, agents_agent_routing [EXTRACTED 1.00]
- **macOS desktop shell implementations (Electron/Tauri current, SwiftUI retired)** — docs_electron_macos_desktop_doc, docs_tauri_macos_desktop_doc, nativeapps_project_quickytradenative [INFERRED 0.85]
- **SUBMISSION_UNKNOWN ambiguous-outcome handling across runbook, core, and implementation docs** — docs_ibkr_paper_runbook_unknown_submission_procedure, core_readme_response_contract, docs_ibkr_tradingview_implementation_state_model [EXTRACTED 0.90]
- **TARGET_RANGE 0DTE strike-selection feature (proposed spec, requirement row, and anticipatory UI)** — docs_0dte_strike_selection_requirements_target_range, docs_0dte_strike_selection_requirements_choose_strike_by_target_range, index_strike_selection_panel, docs_requirement_traceability_sel_requirements_row [INFERRED 0.80]

## Communities (89 total, 20 thin omitted)

### Community 0 - "IBKR Execution Engine"
Cohesion: 0.07
Nodes (45): LimitOrderRequest, QualifiedContract, _contract_from_json(), _contract_to_json(), _entry_reference(), ExecutionBlocked, ExecutionEngine, ExecutionResult (+37 more)

### Community 1 - "Operator Dashboard Frontend (app.js)"
Cohesion: 0.09
Nodes (70): activateProfile(), age(), api(), appendAttentionHeading(), appendOption(), attentionAcknowledgementKey(), attentionReason(), buildActiveTradeRow() (+62 more)

### Community 2 - "Broker-Neutral Domain Contracts"
Cohesion: 0.07
Nodes (27): BrokerAcknowledgement, BrokerAmbiguousError, BrokerDefinitiveError, OptionChain, PriceIncrement, Exception, Broker-neutral execution contracts for the QuickyTrade TWS core.  The transport, The broker definitively refused a request before accepting the order. (+19 more)

### Community 3 - "Management Transition State Machine"
Cohesion: 0.06
Nodes (21): _details(), _now(), Any, Connection, RLock, Idempotent: creates the durable PENDING row the first time this         transiti, PENDING -> APPLYING, durably, before any broker modify call.         Returns Fal, A pre-broker-call block (e.g. a stale/missing quote for         TRAIL_FRESH_BID, (+13 more)

### Community 4 - "Node Server & API Routes"
Cohesion: 0.06
Nodes (57): ALLOWED_STRATEGIES, ALLOWED_TICKERS, appendCoreLog(), assertWebhookRate(), buildManualCoreRequest(), checkCore(), checkPositions(), computeDailyPnl() (+49 more)

### Community 5 - "Execution Ledger"
Cohesion: 0.06
Nodes (24): ExecutionLedger, _normalized_execution_time(), _now(), Any, Connection, RLock, Return an absolute execution timestamp or None when IBKR evidence is ambiguous., Executions, commissions, rebuildable position cache, and reconciliation audit tr (+16 more)

### Community 6 - "Official IBAPI Transport"
Cohesion: 0.08
Nodes (7): _ib_contract(), OfficialIbapiTransport, _optional_positive_int(), Decimal, _qualified_contract_only(), Modify an existing working stop-limit order **in place**: re-sends         place, Places (order_id is None -- a fresh id is allocated) or modifies         (order_

### Community 7 - "Submission Registry"
Cohesion: 0.07
Nodes (15): Claim, _json(), _now(), Any, Connection, Path, RLock, The single shared sqlite3.Connection this registry owns.          Exposed so a s (+7 more)

### Community 8 - "Core Config & HTTP Service"
Cohesion: 0.10
Nodes (25): CoreConfig, _csv(), is_live_account(), is_paper_account(), Validated configuration for an IBKR core process.  Paper accounts (``DU...``) wo, _require_loopback(), CancelAcknowledgement, A *confirmed* broker cancellation (never returned for a timed-out or     otherwi (+17 more)

### Community 9 - "Reduce-Only Close Tests"
Cohesion: 0.12
Nodes (9): Position, close_request(), _FakeHttpSocket, http_exchange(), management_policy(), open_request(), option(), Seed a FILLED, APP_MANAGED entry, place its full protection         bracket via (+1 more)

### Community 10 - "Agent Routing & Safety Rules"
Cohesion: 0.11
Nodes (39): QuickyTrade Agent Routing, Duplicate-Alert Dedup Rule, Fail-Closed on Missing/Stale Evidence Rule, Never Render Missing Broker Values as Zero Rule, No Fabricated Quotes / Marketable Limits Rule, No Live-Trading Automation Rule, Persist Before Broker Side Effect Rule, Long-Only Opens / Reduce-Only Closes Rule (+31 more)

### Community 11 - "Execution Ledger Tests"
Cohesion: 0.13
Nodes (7): CommissionRecord, ExecutionRecord, IBKR sends its UNSET_DOUBLE sentinel (sys.float_info.max) -- or NaN --     for r, _sanitize_realized_pnl(), _bare_transport(), ExecutionLedgerTests, A transport instance with no real ibapi socket, exercising only the     pure rec

### Community 12 - "Read Endpoint Tests"
Cohesion: 0.10
Nodes (5): _FakeHttpSocket, http_get(), option(), Every ledger wired -- the "happy path" fixture., ReadEndpointsTests

### Community 13 - "TradingView Webhook Ingress"
Cohesion: 0.12
Nodes (21): DedupeConflictError, SubmissionUnknownError, WebhookError, canonicalize(), canonicalPayload(), extractBodyAuth(), TradingViewWebhookIngress, ACTIONS (+13 more)

### Community 14 - "Execution Contract Tests"
Cohesion: 0.10
Nodes (3): close_request(), ExecutionTests, open_request()

### Community 15 - "Operator SQLite Store"
Cohesion: 0.12
Nodes (12): assertMode(), isLoopbackHost(), MANAGEMENT_MODES, normalizeCapitalPerTradeDollars(), nowIso(), OperatorStore, PAPER_POLICY, PROFILE_SEEDS (+4 more)

### Community 16 - "Companion Transport (MultipeerConnectivity)"
Cohesion: 0.10
Nodes (26): AnyCancellable, Data, InputStream, MCNearbyServiceAdvertiser, MCNearbyServiceAdvertiserDelegate, MCNearbyServiceBrowser, MCNearbyServiceBrowserDelegate, MCPeerID (+18 more)

### Community 17 - "Native Trading Models (Swift)"
Cohesion: 0.22
Nodes (25): CaseIterable, Codable, Date, Double, Identifiable, AccountSnapshot, BrokerOrder, BrokerPosition (+17 more)

### Community 18 - "Alert Timeline & Submission Store"
Cohesion: 0.19
Nodes (5): lookupOriginatingAlert(), submitCloseIntent(), asIso(), parseJson(), TradingViewStore

### Community 19 - "BrokerTransport Interface"
Cohesion: 0.09
Nodes (7): BrokerTransport, Decimal, A protective STP LMT order (never a plain market-triggered STP -- this     codeb, High-level surface implemented by the official TWS API adapter., Cancel an existing working order and wait for a *definitive*         broker conf, StopLimitOrderRequest, Protocol

### Community 20 - "Option Strike Selection"
Cohesion: 0.22
Nodes (15): applicable_increment(), candidate_strikes(), choose_chain_and_expiry(), choose_listed_strike(), choose_strike_by_target_range(), marketable_limit(), datetime, Decimal (+7 more)

### Community 21 - "App-Core Response Contract & UI Panels"
Cohesion: 0.09
Nodes (22): App-to-core response contract (SUBMITTED / BLOCKED / SUBMISSION_UNKNOWN), choose_listed_strike, choose_strike_by_target_range (proposed), TARGET_RANGE strike_policy type, Disconnect procedure, Unknown submission (SUBMISSION_UNKNOWN) procedure, Ingress state model (READY/PROCESSING/SUBMITTED/BLOCKED/FAILED/SUBMISSION_UNKNOWN), Supported signal actions (OPEN/CLOSE variants) (+14 more)

### Community 22 - "Desktop Shell & Requirement Docs"
Cohesion: 0.13
Nodes (20): AGENTS.md ('only core/ owns the TWS connection'), Capital-based dynamic contract sizing, QuickyTrade TWS execution core README, 0DTE strike-selection requirements, Old sizing/scale-out explicitly not ported, Electron macOS desktop shell, IBKR supervised paper runbook, IBKR TradingView implementation (+12 more)

### Community 23 - "Tauri Desktop Runtime (Rust)"
Cohesion: 0.18
Nodes (15): Child, Drop, Mutex, Option, Result, AppState, ChildGuard, get_desktop_runtime() (+7 more)

### Community 24 - "Protection Ledger"
Cohesion: 0.16
Nodes (8): ProtectionClaim, ProtectionLedger, Any, Stop-loss / take-profit protection-order evidence and status., Transition a still-PENDING_FILL_CONFIRMATION row directly to         BLOCKED --, All rows (any top-up index) for one (correlation_id, role,         level/oca) fa, Contract-scoped (never global) ambiguity check -- deliberately         distinct, Every protection leg with ``cancel_status='CANCEL_UNKNOWN'`` --         the glob

### Community 25 - "Protection Order Evidence"
Cohesion: 0.11
Nodes (9): _now(), Idempotent direct write for a level whose computed quantity is         zero -- I, Commit final broker-call evidence before any socket side effect.         Only tr, A proven broker acknowledgement of the modify: the pending         trigger/limit, An ack timeout/indeterminate result on the modify call itself --         exactly, A *definitive* (non-ambiguous) broker rejection of the modify         itself --, Durable cancel-intent evidence, committed before any         ``cancel_order`` br, A proven broker cancellation (IBKR's orderStatus/error(202)         confirmation (+1 more)

### Community 26 - "Protection Placement Tests"
Cohesion: 0.25
Nodes (3): open_request(), option(), ProtectionPlacementTests

### Community 27 - "Management Policy Tests"
Cohesion: 0.21
Nodes (3): http_exchange(), management_policy(), manual_request()

### Community 29 - "Native Mac Desk UI"
Cohesion: 0.17
Nodes (10): PhoneDashboard, PhoneRootView, MacDeskModel, Int, Void, MacDeskView, MacSettingsView, Never (+2 more)

### Community 30 - "Native App Entrypoints"
Cohesion: 0.16
Nodes (9): App, Combine, Foundation, MultipeerConnectivity, QuickyTradeIOSApp, Scene, QuickyTradeMacApp, Scene (+1 more)

### Community 31 - "Core HTTP Request Handler"
Cohesion: 0.29
Nodes (5): BaseHTTPRequestHandler, _Handler, Any, Best-effort, non-raising extraction of request["signal"]["action"] for     the p, _signal_action()

### Community 33 - "Broker Reconciliation Sweep"
Cohesion: 0.21
Nodes (5): Any, Backfill same-day fills via reqExecutions.          Documented, permanent limita, Backfill order-level completion status via reqCompletedOrders.          Confirme, Resolve unresolved SUBMISSION_UNKNOWN rows strictly from broker         evidence, Cross-day fallback for whatever remains unresolved after the above.          If

### Community 34 - "Management Contract Parsing"
Cohesion: 0.37
Nodes (11): _decimal_text(), ManagementContractError, parse_management_contract(), _percent(), Any, Decimal, ValueError, Immutable source, ownership, and trade-management intent contracts.  These value (+3 more)

### Community 35 - "Electron Desktop Shell"
Cohesion: 0.18
Nodes (7): allowedNavigation(), APP_ROOT, createWindow(), port, startLocalServer(), waitForServer(), electron/**/*

### Community 36 - "Tauri Desktop Capability Schema"
Cohesion: 0.15
Nodes (13): properties, Identifier, default, description, type, description, oneOf, type (+5 more)

### Community 37 - "Tauri Desktop Schema Definitions"
Cohesion: 0.15
Nodes (13): definitions, Number, PermissionEntry, Target, Value, anyOf, description, anyOf (+5 more)

### Community 38 - "Tauri macOS Capability Schema"
Cohesion: 0.15
Nodes (13): properties, Identifier, default, description, type, description, oneOf, type (+5 more)

### Community 39 - "Tauri macOS Schema Definitions"
Cohesion: 0.15
Nodes (13): definitions, Number, PermissionEntry, Target, Value, anyOf, description, anyOf (+5 more)

### Community 41 - "NPM Scripts"
Cohesion: 0.17
Nodes (12): scripts, check, desktop:dev, desktop:package:mac, desktop:tauri:build, desktop:tauri:dev, lint, lint:core (+4 more)

### Community 42 - "Tauri Desktop Permissions Schema"
Cohesion: 0.17
Nodes (12): $ref, array, null, description, items, type, uniqueItems, description (+4 more)

### Community 43 - "Tauri macOS Permissions Schema"
Cohesion: 0.17
Nodes (12): $ref, array, null, description, items, type, uniqueItems, description (+4 more)

### Community 44 - "Build Tooling Dependencies"
Cohesion: 0.18
Nodes (11): electron, electron-builder, eslint, globals, devDependencies, electron, electron-builder, eslint (+3 more)

### Community 45 - "Alert Processor & Redaction"
Cohesion: 0.33
Nodes (5): IbkrAlertProcessor, isAmbiguous(), redact(), redactString(), safeErrorCode()

### Community 46 - "Largest-Remainder Allocation"
Cohesion: 0.42
Nodes (4): largest_remainder_allocation(), Deterministic, exact (pure Decimal, no floats) largest-remainder     distributio, LargestRemainderAllocationTests, management_policy()

### Community 47 - "Execution Engine Cross-Doc Concepts"
Cohesion: 0.20
Nodes (10): ExecutionEngine (ensure_protection / ensure_transitions), ExecutionLedger, Management-policy transitions (MOVE_STOP_TO_BREAKEVEN / TRAIL_FRESH_BID), OfficialIbapiTransport, ProtectionLedger, Reconciliation sweep, SubmissionRegistry, ExecutionEngine._prepare_open (+2 more)

### Community 48 - "Companion Message Protocol"
Cohesion: 0.20
Nodes (8): CompanionMessage, acknowledgement, cancelProposal, orderProposal, ping, snapshot, JSONEncoder, Bool

### Community 49 - "Tauri Desktop Windows/Webviews Schema"
Cohesion: 0.20
Nodes (10): type, webviews, windows, items, description, items, type, description (+2 more)

### Community 50 - "Tauri macOS Windows/Webviews Schema"
Cohesion: 0.20
Nodes (10): type, webviews, windows, items, description, items, type, description (+2 more)

### Community 51 - "Electron-Builder macOS Config"
Cohesion: 0.22
Nodes (9): build, appId, asar, mac, productName, category, target, dmg (+1 more)

### Community 52 - "Electron Packaged Files List"
Cohesion: 0.36
Nodes (8): asarUnpack, files, app.js, index.html, operator.css, server.mjs, src/**/*, styles.css

### Community 53 - "Tauri Desktop Capability Remote URLs"
Cohesion: 0.22
Nodes (9): description, properties, required, type, CapabilityRemote, urls, urls, description (+1 more)

### Community 54 - "Tauri macOS Capability Remote URLs"
Cohesion: 0.22
Nodes (9): description, properties, required, type, CapabilityRemote, urls, urls, description (+1 more)

### Community 55 - "Package Metadata"
Cohesion: 0.25
Nodes (7): engines, node, main, name, private, type, version

### Community 56 - "Webhook Auth Verification"
Cohesion: 0.67
Nodes (6): constantTimeTextEqual(), headerValue(), validBearer(), validBodyToken(), validHmac(), verifyWebhookAuth()

### Community 57 - "Run-Desktop Driver Script (.agents)"
Cohesion: 0.33
Nodes (5): APP_DIR, COMMANDS, electronBin, rl, stdin

### Community 58 - "Run-Desktop Driver Script (.claude)"
Cohesion: 0.33
Nodes (5): APP_DIR, COMMANDS, electronBin, rl, stdin

### Community 60 - "Tauri Desktop Capability Definition"
Cohesion: 0.33
Nodes (6): description, required, type, Capability, identifier, permissions

### Community 61 - "Tauri macOS Capability Definition"
Cohesion: 0.33
Nodes (6): description, required, type, Capability, identifier, permissions

### Community 63 - "Protection DB Migration"
Cohesion: 0.40
Nodes (3): Connection, RLock, Additive-only migration for an already-populated pre-Phase-4         database (m

### Community 64 - "App-Managed Execution Flag (Docs Conflict)"
Cohesion: 0.40
Nodes (5): APP_MANAGEMENT_EXECUTION_AVAILABLE (described as live, auto-submits), Runtime modes (capture_only / paper_tws), Hardcoded scope limit: no order can currently reach IBKR from a webhook alert, capture_only safety mode, Controlled paper adapter (paper_tws)

### Community 65 - "Tauri Desktop Schema Root"
Cohesion: 0.40
Nodes (4): anyOf, description, $schema, title

### Community 66 - "Tauri macOS Schema Root"
Cohesion: 0.40
Nodes (4): anyOf, description, $schema, title

### Community 67 - "Tauri Desktop Local Permission Default"
Cohesion: 0.50
Nodes (4): default, description, type, local

### Community 68 - "Tauri macOS Local Permission Default"
Cohesion: 0.50
Nodes (4): default, description, type, local

## Ambiguous Edges - Review These
- `ibkr-evaluator Agent` → `docs/ Safety & Requirements Specs`  [AMBIGUOUS]
  .claude/agents/ibkr-evaluator.md · relation: references
- `Runtime modes (capture_only / paper_tws)` → `Hardcoded scope limit: no order can currently reach IBKR from a webhook alert`  [AMBIGUOUS]
  docs/TRADINGVIEW_ALERT_SETUP.md · relation: conceptually_related_to
- `APP_MANAGEMENT_EXECUTION_AVAILABLE (described as live, auto-submits)` → `Hardcoded scope limit: no order can currently reach IBKR from a webhook alert`  [AMBIGUOUS]
  docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md · relation: conceptually_related_to
- `QuickyTrade operator dashboard shell` → `Empty public/index.html stub`  [AMBIGUOUS]
  public/index.html · relation: conceptually_related_to

## Knowledge Gaps
- **208 isolated node(s):** `APP_DIR`, `electronBin`, `COMMANDS`, `stdin`, `rl` (+203 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **20 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `ibkr-evaluator Agent` and `docs/ Safety & Requirements Specs`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Runtime modes (capture_only / paper_tws)` and `Hardcoded scope limit: no order can currently reach IBKR from a webhook alert`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `APP_MANAGEMENT_EXECUTION_AVAILABLE (described as live, auto-submits)` and `Hardcoded scope limit: no order can currently reach IBKR from a webhook alert`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `QuickyTrade operator dashboard shell` and `Empty public/index.html stub`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `ExecutionEngine` connect `IBKR Execution Engine` to `Protection Test Fixture`, `Broker-Neutral Domain Contracts`, `Management Contract Parsing`, `Management Transition State Machine`, `Execution Ledger`, `Submission Registry`, `Core Config & HTTP Service`, `Target-Range Strike Tests`, `Reduce-Only Close Tests`, `Read Endpoint Tests`, `Execution Contract Tests`, `Largest-Remainder Allocation`, `BrokerTransport Interface`, `Option Strike Selection`, `Protection Ledger`, `Protection Placement Tests`, `Fake Broker Transport Fixture`, `Core HTTP Request Handler`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Why does `OfficialIbapiTransport` connect `Official IBAPI Transport` to `IBKR Execution Engine`, `Broker Reconciliation Sweep`, `Broker-Neutral Domain Contracts`, `Execution Ledger`, `Core Config & HTTP Service`, `Target-Range Strike Tests`, `Reduce-Only Close Tests`, `Execution Ledger Tests`, `Execution Contract Tests`, `BrokerTransport Interface`, `IBAPI Readiness Tests`, `Fake Broker Transport Fixture`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Why does `SubmissionRegistry` connect `Submission Registry` to `IBKR Execution Engine`, `Protection Test Fixture`, `Broker-Neutral Domain Contracts`, `Management Transition State Machine`, `Execution Ledger`, `Core Config & HTTP Service`, `Reduce-Only Close Tests`, `Execution Ledger Tests`, `Read Endpoint Tests`, `Execution Contract Tests`, `Largest-Remainder Allocation`, `BrokerTransport Interface`, `Protection Placement Tests`, `Fake Broker Transport Fixture`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
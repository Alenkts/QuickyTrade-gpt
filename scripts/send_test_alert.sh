#!/usr/bin/env bash
set -euo pipefail

# QuickyTrade TradingView Webhook Test Helper
# Usage:
#   bash scripts/send_test_alert.sh [ACTION] [TICKER] [SECRET] [PORT]
# Example:
#   bash scripts/send_test_alert.sh OPEN_LONG_CALL QQQ secret123 4180

ACTION="${1:-OPEN_LONG_CALL}"
TICKER="${2:-QQQ}"
SECRET="${3:-${QT_WEBHOOK_SECRET:-replace-with-a-long-random-secret}}"
PORT="${4:-${QT_WEBHOOK_PORT:-4180}}"
HOST="${QT_WEBHOOK_HOST:-127.0.0.1}"

URL="http://${HOST}:${PORT}/webhooks/tradingview"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

ACTION_LOWER="$(echo "${ACTION}" | tr '[:upper:]' '[:lower:]')"
TICKER_LOWER="$(echo "${TICKER}" | tr '[:upper:]' '[:lower:]')"
ALERT_ID="test-${TICKER_LOWER}-${ACTION_LOWER}-$(date +%s)"

echo "--------------------------------------------------------"
echo "Sending test TradingView alert to QuickyTrade..."
echo "  Target URL : ${URL}"
echo "  Alert ID   : ${ALERT_ID}"
echo "  Action     : ${ACTION}"
echo "  Ticker     : ${TICKER}"
echo "  Timestamp  : ${TIMESTAMP}"
echo "--------------------------------------------------------"

PAYLOAD="$(cat <<EOF
{
  "auth_token": "${SECRET}",
  "schema_version": "1",
  "alert_id": "${ALERT_ID}",
  "sent_at": "${TIMESTAMP}",
  "strategy_id": "qqq-alerts",
  "strategy_version": "2026.07.18",
  "action": "${ACTION}",
  "ticker": "${TICKER}",
  "target_dte": 0,
  "strike_policy": {
    "type": "ATM_OFFSET",
    "offset": 1
  },
  "risk_hint": {
    "max_contracts": 1
  }
}
EOF
)"

RESPONSE=$(curl -s -i -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")

echo "Response from QuickyTrade:"
echo "${RESPONSE}"
echo "--------------------------------------------------------"

if echo "${RESPONSE}" | grep -q "HTTP/1.1 202"; then
  echo "✅ Webhook alert successfully accepted (HTTP 202)!"
  echo "Check the QuickyTrade dashboard (http://127.0.0.1:4173) to view the recorded alert in the timeline."
else
  echo "⚠️ Webhook call finished. Check response above for status."
fi

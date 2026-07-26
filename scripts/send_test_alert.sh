#!/usr/bin/env bash
set -euo pipefail

# QuickyTrade TradingView Webhook Test Helper
# Usage:
#   QT_WEBHOOK_SECRET=... bash scripts/send_test_alert.sh [ACTION] [TICKER] [PORT]
# Example:
#   QT_WEBHOOK_SECRET=... bash scripts/send_test_alert.sh OPEN_LONG_CALL QQQ 4180
#
# The secret is read ONLY from the environment. It used to be accepted as the
# third positional argument, which put the live order-submission credential
# into shell history and made it visible in `ps` to every user on the machine
# for the lifetime of the process.

ACTION="${1:-OPEN_LONG_CALL}"
TICKER="${2:-QQQ}"
PORT="${3:-${QT_WEBHOOK_PORT:-4180}}"

if [[ -z "${QT_WEBHOOK_SECRET:-}" ]]; then
  echo "QT_WEBHOOK_SECRET is not set. Export it (or source your .env) and re-run;" >&2
  echo "this script deliberately no longer accepts the secret as an argument." >&2
  exit 2
fi
SECRET="${QT_WEBHOOK_SECRET}"
HOST="${QT_WEBHOOK_HOST:-127.0.0.1}"

URL="http://${HOST}:${PORT}/webhooks/tradingview"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

ACTION_LOWER="$(echo "${ACTION}" | tr '[:upper:]' '[:lower:]')"
TICKER_LOWER="$(echo "${TICKER}" | tr '[:upper:]' '[:lower:]')"
ALERT_ID="test-${TICKER_LOWER}-${ACTION_LOWER}-${TIMESTAMP}"

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
  "ticker": "${TICKER}"
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

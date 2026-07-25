import { SubmissionUnknownError } from './errors.js';
import { redact, safeErrorCode } from './redaction.js';

function isAmbiguous(error) {
  if (error instanceof SubmissionUnknownError) return true;
  if (error?.ambiguous === true) return true;
  if (error?.definitive === true) return false;
  // Once placeTrade has been called, an unclassified transport/adapter failure is
  // conservatively ambiguous. Retrying could create a second live IBKR order.
  return true;
}

export class IbkrAlertProcessor {
  constructor({ store, ibkrAdapter, logger = undefined }) {
    if (!store) throw new TypeError('store is required');
    if (typeof ibkrAdapter?.placeTrade !== 'function') {
      throw new TypeError('ibkrAdapter.placeTrade is required');
    }
    this.store = store;
    this.ibkrAdapter = ibkrAdapter;
    this.logger = logger;
  }

  async processNext({ source = 'tradingview' } = {}) {
    const intentId = this.store.findNextReady(source);
    return intentId === null ? null : this.processIntent(intentId);
  }

  async processIntent(intentId) {
    // This transaction creates a durable SUBMITTING order before IBKR is called.
    const prepared = this.store.prepareSubmission(intentId);
    if (!prepared) return null;
    if (!prepared.claimed) return this.store.getIntent(intentId);

    const { intent, orderId, idempotencyKey } = prepared;
    try {
      const result = await this.ibkrAdapter.placeTrade(Object.freeze({
        broker: 'IBKR',
        idempotencyKey,
        intentId: intent.id,
        correlationId: intent.correlation_id,
        source: intent.source,
        alertId: intent.alertId,
        managementMode: intent.managementMode,
        managementPolicyId: intent.managementPolicyId,
        managementPolicy: intent.managementPolicy,
        signal: Object.freeze({ ...intent.payload }),
      }));

      if (result?.status === 'BLOCKED') {
        const errorCode = safeErrorCode({ code: result.code }, 'IBKR_ORDER_BLOCKED');
        return this.store.finishSubmission(intent.id, orderId, { status: 'BLOCKED', errorCode });
      }
      if (result?.status !== 'SUBMITTED' || typeof result.brokerOrderId !== 'string' || result.brokerOrderId.length === 0) {
        const error = new Error('IBKR adapter returned an indeterminate result');
        error.ambiguous = true;
        error.code = 'IBKR_INDETERMINATE_RESULT';
        throw error;
      }
      return this.store.finishSubmission(intent.id, orderId, {
        status: 'SUBMITTED',
        brokerOrderId: result.brokerOrderId,
      });
    } catch (error) {
      const unknown = isAmbiguous(error);
      const status = unknown ? 'SUBMISSION_UNKNOWN' : 'FAILED';
      const errorCode = safeErrorCode(error, unknown ? 'IBKR_SUBMISSION_UNKNOWN' : 'IBKR_SUBMISSION_FAILED');
      this.logger?.error?.('IBKR alert processing ended without submission confirmation', redact({
        intent_id: intent.id,
        alert_id: intent.alertId,
        status,
        error_code: errorCode,
      }));
      return this.store.finishSubmission(intent.id, orderId, { status, errorCode });
    }
  }
}

export class WebhookError extends Error {
  constructor(code, message, statusCode = 400) {
    super(message);
    this.name = 'WebhookError';
    this.code = code;
    this.statusCode = statusCode;
  }
}

export class DedupeConflictError extends WebhookError {
  constructor() {
    super(
      'ALERT_ID_CONFLICT',
      'The alert_id was already used for a different payload',
      409,
    );
    this.name = 'DedupeConflictError';
  }
}

export class SubmissionUnknownError extends Error {
  constructor(message = 'IBKR submission outcome is unknown', options = {}) {
    super(message, options);
    this.name = 'SubmissionUnknownError';
    this.code = 'SUBMISSION_UNKNOWN';
    this.ambiguous = true;
  }
}

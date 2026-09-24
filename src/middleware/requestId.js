'use strict';

const { newId } = require('../utils/ids');

/** Max length accepted for an inbound correlation / request id. */
const MAX_CORRELATION_ID_LENGTH = 128;

/**
 * Safe correlation ids: printable, non-secret, and short enough to log.
 * Rejects anything that looks like it could carry a token or free-form PII
 * dump (spaces, quotes, control chars, oversized values).
 * @param {*} value
 * @returns {string|null}
 */
function sanitizeCorrelationId(value) {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > MAX_CORRELATION_ID_LENGTH) {
    return null;
  }
  if (!/^[A-Za-z0-9._:-]+$/.test(trimmed)) {
    return null;
  }
  return trimmed;
}

/**
 * Attach a unique correlation identifier to every request.
 *
 * Honour inbound `X-Request-Id` or `X-Correlation-Id` when they pass
 * sanitization so callers can stitch logs across services. Otherwise a
 * fresh id is generated. The same value is exposed as `req.id` and
 * `req.correlationId`, and echoed on both response headers so either
 * convention works for clients.
 */
function requestId(req, res, next) {
  const incoming =
    sanitizeCorrelationId(req.get('X-Request-Id')) ||
    sanitizeCorrelationId(req.get('X-Correlation-Id'));
  const id = incoming || newId();
  req.id = id;
  req.correlationId = id;
  res.set('X-Request-Id', id);
  res.set('X-Correlation-Id', id);
  next();
}

module.exports = requestId;
module.exports.sanitizeCorrelationId = sanitizeCorrelationId;
module.exports.MAX_CORRELATION_ID_LENGTH = MAX_CORRELATION_ID_LENGTH;

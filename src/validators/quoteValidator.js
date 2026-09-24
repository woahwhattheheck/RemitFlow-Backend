'use strict';

const currencyPolicy = require('../utils/currencyPolicy');

/**
 * Validate query parameters for GET /api/quote.
 * Uses the same currency policy as transfer creation so a preview that
 * succeeds is always executable (and an unsupported pair fails before
 * any FX math runs).
 * @param {import('express').Request} req
 * @returns {string[]} list of error messages.
 */
function validateQuoteQuery(req) {
  const { amount, from, to } = req.query;
  // Quotes do not enforce the transfer max — they are informational — but
  // they do enforce currency support, precision, and positive/safe range.
  return currencyPolicy.validateTransferPair(amount, from, to, {
    enforceMax: false,
  });
}

module.exports = {
  validateQuoteQuery,
};

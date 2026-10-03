'use strict';

const config = require('../config');
const rateService = require('./rateService');
const money = require('../utils/money');
const currencyPolicy = require('../utils/currencyPolicy');
const ApiError = require('../utils/ApiError');

/**
 * Quote calculation.
 * A quote tells the sender how much the recipient will receive after
 * RemitFlow's fee and the FX conversion are applied.
 *
 * Amounts are canonicalized through `currencyPolicy` so the send amount
 * recorded on a transfer matches the amount this preview returned.
 */

/**
 * Compute the fee charged on a send amount.
 * Fee is a percentage of the amount plus a small flat component, rounded
 * to the source currency's minor units.
 * @param {number} amount - canonical amount in the source currency.
 * @param {string} fromCode
 * @returns {number}
 */
function calculateFee(amount, fromCode = config.baseCurrency) {
  const percentFee = Number(amount) * (Number(config.fee.percent) / 100);
  const fee = percentFee + config.fee.flat;
  // Round both components together, compensating for binary drift at a
  // half-minor-unit boundary (for example, 480 JPY yields 7.499999999999999).
  return currencyPolicy.roundToCurrency(
    fee + Math.abs(fee) * Number.EPSILON,
    fromCode
  );
}

/**
 * Build a full quote for converting `amount` from `from` to `to`.
 * @param {number|string} amount
 * @param {string} from
 * @param {string} to
 * @returns {object} quote breakdown.
 */
function getQuote(amount, from, to) {
  const canonical = currencyPolicy.canonicalizeAmount(amount, from, {
    enforceMax: true,
  });
  if (!canonical.ok) {
    throw ApiError.badRequest(canonical.errors[0] || 'Invalid amount');
  }

  if (!currencyPolicy.isSupported(to)) {
    throw ApiError.badRequest(`Unsupported target currency: ${to}`);
  }

  const fromCode = canonical.currency;
  const toMeta = currencyPolicy.getMeta(to);
  const toCode = toMeta.code;
  const numericAmount = canonical.amount;

  if (fromCode === toCode) {
    throw ApiError.badRequest('from and to currencies must differ');
  }

  const fee = calculateFee(numericAmount, fromCode);
  const amountAfterFee = currencyPolicy.roundToCurrency(
    numericAmount - fee,
    fromCode
  );
  const rate = rateService.getRate(fromCode, toCode);
  // Receive side rounds to the *destination* currency's minor units so a
  // JPY payout never carries fractional yen that settlement cannot pay.
  const receiveAmount = currencyPolicy.roundToCurrency(
    amountAfterFee * rate,
    toCode
  );

  return {
    from: fromCode,
    to: toCode,
    sendAmount: numericAmount,
    fee,
    amountAfterFee,
    rate: money.round(rate),
    receiveAmount,
  };
}

module.exports = {
  calculateFee,
  getQuote,
};

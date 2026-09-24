'use strict';

const { store } = require('../store');
const stellarService = require('./stellarService');

/**
 * Idempotent settlement worker for terminal claim operations.
 *
 * Provider work is keyed by a stable operation id. Retries — including after a
 * worker-module reload that shares the same process store — return the first
 * settlement receipt instead of creating a second claimable balance.
 *
 * Receipts live in the shared store so their lifetime matches transfers: a
 * restart that clears transfers also clears receipts, keeping the two
 * consistent. When the store becomes durable, keep receipts on the same
 * boundary as the transfer row that commits the claim.
 */

/**
 * Settle a claim against the payment provider exactly once for `operationId`.
 * @param {string} operationId - stable id spanning retries of one claim attempt
 * @returns {{ operationId: string, claimableBalanceId: string, settledAt: string }}
 */
function settleClaim(operationId) {
  if (typeof operationId !== 'string' || operationId.trim() === '') {
    throw new Error('settlementWorker.settleClaim requires a non-empty operationId');
  }

  const existing = store.settlementReceipts.get(operationId);
  if (existing) {
    return existing;
  }

  const claimableBalanceId = stellarService.createClaimableBalanceId(operationId);
  const receipt = {
    operationId,
    claimableBalanceId,
    settledAt: new Date().toISOString(),
  };
  store.settlementReceipts.set(operationId, receipt);
  return receipt;
}

/**
 * Look up a prior settlement receipt without contacting the provider.
 * @param {string} operationId
 * @returns {object|null}
 */
function getReceipt(operationId) {
  return store.settlementReceipts.get(operationId) || null;
}

module.exports = {
  settleClaim,
  getReceipt,
};

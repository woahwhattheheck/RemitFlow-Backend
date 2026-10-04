'use strict';

const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { RATES_TO_USD } = require('../src/config/rates');
const rateService = require('../src/services/rateService');
const quoteService = require('../src/services/quoteService');
const fxCacheService = require('../src/services/fxCacheService');
const { store } = require('../src/store');

const NOW = 10_000_000;

beforeEach(() => {
  fxCacheService.reset();
  store.quotes.clear();
  quoteService.resetQuoteVersions();
  fxCacheService.seed({
    ratesToUsd: RATES_TO_USD,
    fetchedAt: NOW,
    providerId: 'precision-fixture',
  });
});

afterEach(() => {
  fxCacheService.reset();
  store.quotes.clear();
});

test('a cached pair retains a small positive FX ratio and snapshot provenance', () => {
  const pair = rateService.getPair('NGN', 'USD', { now: NOW });
  assert.equal(pair.rate, 0.00065);
  assert.equal(JSON.parse(JSON.stringify(pair)).rate, 0.00065);
  assert.equal(pair.freshness.providerId, 'precision-fixture');
  assert.equal(pair.freshness.fetchedAt, new Date(NOW).toISOString());
  assert.equal(fxCacheService.getProviderFetchCount(), 0);
});

test('a quote preserves the FX ratio while fee and receive amounts stay rounded', () => {
  const quote = quoteService.getQuote(10000, 'NGN', 'USD', { now: NOW });
  assert.equal(quote.rate, 0.00065);
  assert.equal(JSON.parse(JSON.stringify(quote)).rate, 0.00065);
  assert.deepEqual(
    [quote.sendAmount, quote.fee, quote.amountAfterFee, quote.receiveAmount],
    [10000, 150.3, 9849.7, 6.4]
  );
  assert.equal(quote.freshness.providerId, 'precision-fixture');
  assert.equal(quote.freshness.fetchedAt, new Date(NOW).toISOString());
  assert.equal(fxCacheService.getProviderFetchCount(), 0);
});

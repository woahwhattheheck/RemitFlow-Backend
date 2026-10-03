'use strict';

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

process.env.NODE_ENV = 'test';

const currencyPolicy = require('../src/utils/currencyPolicy');
const quoteService = require('../src/services/quoteService');
const { validateCreateTransfer } = require('../src/validators/transferValidator');
const { validateQuoteQuery } = require('../src/validators/quoteValidator');
const createApp = require('../src/app');
const { reset } = require('../src/store');

// ── Unit: policy table ──────────────────────────────────────────────────────

test('CURRENCY_META covers every rate-listed currency with ISO minor units', () => {
  assert.equal(currencyPolicy.getMeta('USD').minorUnits, 2);
  assert.equal(currencyPolicy.getMeta('jpy').minorUnits, 0);
  assert.equal(currencyPolicy.getMeta('JPY').minAmount, 1);
  assert.equal(currencyPolicy.isSupported('MXN'), true);
  assert.equal(currencyPolicy.isSupported('XYZ'), false);
});

test('canonicalizeAmount accepts a well-formed USD amount and returns canonical form', () => {
  const result = currencyPolicy.canonicalizeAmount('100.50', 'usd');
  assert.equal(result.ok, true);
  assert.equal(result.amount, 100.5);
  assert.equal(result.currency, 'USD');
});

test('canonicalizeAmount rejects unsupported currency before mutation', () => {
  const result = currencyPolicy.canonicalizeAmount(10, 'ZZZ');
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => /Unsupported currency/i.test(e)));
});

test('canonicalizeAmount rejects sub-minor-unit precision for USD', () => {
  const result = currencyPolicy.canonicalizeAmount(10.129, 'USD');
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => /decimal places/i.test(e)));
});

test('canonicalizeAmount rejects fractional JPY (zero-decimal currency)', () => {
  const result = currencyPolicy.canonicalizeAmount(100.5, 'JPY');
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => /whole number/i.test(e)));
});

test('canonicalizeAmount accepts whole JPY and rejects below minimum', () => {
  assert.equal(currencyPolicy.canonicalizeAmount(100, 'JPY').ok, true);
  const tooSmall = currencyPolicy.canonicalizeAmount(0.5, 'JPY');
  // 0.5 has a fractional digit so precision fails first; 0 is non-positive.
  const zero = currencyPolicy.canonicalizeAmount(0, 'JPY');
  assert.equal(zero.ok, false);
  assert.ok(tooSmall.ok === false);
});

test('canonicalizeAmount rejects overflow past safe minor-unit magnitude', () => {
  const result = currencyPolicy.canonicalizeAmount(1e21, 'USD');
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => /numeric range/i.test(e)));
});

test('canonicalizeAmount enforces the transfer max when requested', () => {
  const over = currencyPolicy.canonicalizeAmount(999999, 'USD', { enforceMax: true });
  assert.equal(over.ok, false);
  assert.ok(over.errors.some((e) => /must not exceed/i.test(e)));
  const quoteOk = currencyPolicy.canonicalizeAmount(999999, 'USD', { enforceMax: false });
  assert.equal(quoteOk.ok, true);
});

test('roundToCurrency uses destination minor units', () => {
  assert.equal(currencyPolicy.roundToCurrency(10.129, 'USD'), 10.13);
  assert.equal(currencyPolicy.roundToCurrency(10.6, 'JPY'), 11);
  assert.equal(currencyPolicy.roundToCurrency(10.4, 'JPY'), 10);
});

// ── Validators share the policy ─────────────────────────────────────────────

test('validateCreateTransfer rejects unsupported currencies', () => {
  const errors = validateCreateTransfer({
    body: {
      senderName: 'A',
      recipientName: 'B',
      amount: 10,
      from: 'USD',
      to: 'ZZZ',
    },
  });
  assert.ok(errors.some((e) => /Unsupported target currency/i.test(e)));
});

test('validateQuoteQuery rejects unsupported source currency', () => {
  const errors = validateQuoteQuery({
    query: { amount: '10', from: 'AAA', to: 'USD' },
  });
  assert.ok(errors.some((e) => /Unsupported source currency/i.test(e)));
});

test('validateCreateTransfer rejects JPY fractional send amounts', () => {
  const errors = validateCreateTransfer({
    body: {
      senderName: 'A',
      recipientName: 'B',
      amount: 50.5,
      from: 'JPY',
      to: 'USD',
    },
  });
  assert.ok(errors.some((e) => /whole number/i.test(e)));
});

// ── Preview matches execution ───────────────────────────────────────────────

test('quote preview sendAmount matches canonicalizeAmount for the same input', () => {
  const amount = '250.25';
  const from = 'USD';
  const to = 'EUR';
  const canonical = currencyPolicy.canonicalizeAmount(amount, from);
  const quote = quoteService.getQuote(amount, from, to);
  assert.equal(canonical.ok, true);
  assert.equal(quote.sendAmount, canonical.amount);
  assert.equal(quote.from, 'USD');
  assert.equal(quote.to, 'EUR');
});

test('JPY destination receiveAmount is a whole number', () => {
  const quote = quoteService.getQuote(100, 'USD', 'JPY');
  assert.equal(Number.isInteger(quote.receiveAmount), true);
});

for (const [currency, amount, expectedFee] of [
  ['JPY', 13, 0],
  ['JPY', 14, 1],
  ['JPY', 30, 1],
  ['JPY', 80, 2],
  ['JPY', 479, 7],
  ['JPY', 480, 8],
  ['JPY', 481, 8],
  ['USD', 100.50, 1.81],
  ['USD', 116.99, 2.05],
  ['USD', 117, 2.06],
  ['USD', 117.01, 2.06],
  ['USD', 125, 2.18],
]) {
  test(`combined fee for ${amount} ${currency} rounds once to ${expectedFee}`, () => {
    assert.equal(quoteService.calculateFee(amount, currency), expectedFee);
  });
}

test('getQuote rejects unsupported currency before FX math', () => {
  assert.throws(
    () => quoteService.getQuote(10, 'USD', 'ZZZ'),
    (err) => err.status === 400 || err.statusCode === 400 || /Unsupported/i.test(err.message)
  );
});

// ── HTTP contract ───────────────────────────────────────────────────────────

let server;
let baseUrl;

before(() => {
  const app = createApp();
  return new Promise((resolve) => {
    server = app.listen(0, () => {
      const { port } = server.address();
      baseUrl = `http://127.0.0.1:${port}`;
      resolve();
    });
  });
});

after(() => {
  if (server) server.close();
});

beforeEach(() => {
  reset();
});

async function fetchJson(path, options = {}) {
  const res = await fetch(`${baseUrl}${path}`, options);
  const body = await res.json();
  return { status: res.status, body };
}

test('GET /api/quote rejects unsupported currency', async () => {
  const { status, body } = await fetchJson('/api/quote?amount=10&from=USD&to=ZZZ');
  assert.equal(status, 400);
  assert.ok(
    body.error.details.errors.some((e) => /Unsupported target currency/i.test(e))
  );
});

test('GET /api/quote rejects a send amount beyond the transfer ceiling', async () => {
  const amount = 999999;
  const quote = await fetchJson(`/api/quote?amount=${amount}&from=USD&to=EUR`);
  assert.equal(quote.status, 400);
  assert.ok(quote.body.error.details.errors.some((e) => /must not exceed/i.test(e)));

  assert.ok(validateCreateTransfer({
    body: {
      senderName: 'Alice',
      recipientName: 'Bob',
      amount,
      from: 'USD',
      to: 'EUR',
    },
  }).some((e) => /must not exceed/i.test(e)));
  assert.throws(
    () => quoteService.getQuote(amount, 'USD', 'EUR'),
    (err) => /must not exceed/i.test(err.message)
  );
});

test('GET /api/quote rejects fractional JPY send amount', async () => {
  const { status, body } = await fetchJson('/api/quote?amount=10.5&from=JPY&to=USD');
  assert.equal(status, 400);
  assert.ok(body.error.details.errors.some((e) => /whole number/i.test(e)));
});

test('POST /api/transfers rejects unsupported currency before mutation', async () => {
  const { status, body } = await fetchJson('/api/transfers', {
    method: 'POST',
    headers: {
      Authorization: 'Bearer test-token-admin',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'idem-currency-1',
    },
    body: JSON.stringify({
      senderName: 'Alice',
      recipientName: 'Bob',
      amount: 25,
      from: 'USD',
      to: 'ZZZ',
    }),
  });
  assert.equal(status, 400);
  assert.ok(
    body.error.details.errors.some((e) => /Unsupported target currency/i.test(e))
  );
});

test('POST /api/transfers and GET /api/quote agree on canonical sendAmount', async () => {
  const amount = 88.88;
  const quoteRes = await fetchJson(`/api/quote?amount=${amount}&from=USD&to=EUR`);
  assert.equal(quoteRes.status, 200);

  const transferRes = await fetchJson('/api/transfers', {
    method: 'POST',
    headers: {
      Authorization: 'Bearer test-token-admin',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'idem-currency-preview-1',
    },
    body: JSON.stringify({
      senderName: 'Alice',
      recipientName: 'Bob',
      amount,
      from: 'USD',
      to: 'EUR',
    }),
  });
  assert.equal(transferRes.status, 201);
  assert.equal(transferRes.body.sendAmount, quoteRes.body.sendAmount);
  assert.equal(transferRes.body.from, quoteRes.body.from);
  assert.equal(transferRes.body.to, quoteRes.body.to);
  assert.equal(transferRes.body.receiveAmount, quoteRes.body.receiveAmount);
});

test('JPY quote and transfer use the fee rounded after both components are added', async () => {
  const quoteRes = await fetchJson('/api/quote?amount=30&from=JPY&to=USD');
  assert.equal(quoteRes.status, 200);
  assert.equal(quoteRes.body.sendAmount, 30);
  assert.equal(quoteRes.body.fee, 1);
  assert.equal(quoteRes.body.amountAfterFee, 29);
  assert.equal(quoteRes.body.receiveAmount, 0.19);

  const transferRes = await fetchJson('/api/transfers', {
    method: 'POST',
    headers: {
      Authorization: 'Bearer test-token-admin',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'idem-currency-jpy-fee',
    },
    body: JSON.stringify({
      senderName: 'Alice',
      recipientName: 'Bob',
      amount: 30,
      from: 'JPY',
      to: 'USD',
    }),
  });
  assert.equal(transferRes.status, 201);
  assert.equal(transferRes.body.sendAmount, 30);
  assert.equal(transferRes.body.fee, 1);
  assert.equal(transferRes.body.receiveAmount, 0.19);
});

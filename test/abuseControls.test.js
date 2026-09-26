'use strict';

const { test, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const express = require('express');

process.env.NODE_ENV = 'test';

const rateLimit = require('../src/middleware/rateLimit');
const requestId = require('../src/middleware/requestId');
const {
  fingerprint,
  resolveClientIp,
  resolveActorKey,
} = require('../src/utils/clientIdentity');
const { sanitizeCorrelationId } = require('../src/middleware/requestId');
const errorHandler = require('../src/middleware/errorHandler');
const ApiError = require('../src/utils/ApiError');

function mockReq(overrides = {}) {
  const headers = { ...(overrides.headers || {}) };
  return {
    ip: overrides.ip || '127.0.0.1',
    socket: { remoteAddress: overrides.remoteAddress || '127.0.0.1' },
    token: overrides.token,
    adminToken: overrides.adminToken,
    get(name) {
      const key = Object.keys(headers).find(
        (k) => k.toLowerCase() === String(name).toLowerCase()
      );
      return key ? headers[key] : undefined;
    },
    ...overrides,
  };
}

function mockRes() {
  const headers = {};
  return {
    headers,
    set(name, value) {
      headers[name] = String(value);
    },
    statusCode: 200,
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(body) {
      this.body = body;
      return this;
    },
  };
}

function run(middleware, req, res) {
  return new Promise((resolve, reject) => {
    middleware(req, res, (err) => {
      if (err) {
        resolve({ err, res });
      } else {
        resolve({ err: null, res });
      }
    });
  });
}

test('fingerprint is stable, truncated, and never echoes the secret', () => {
  const a = fingerprint('test-token-admin');
  const b = fingerprint('test-token-admin');
  assert.equal(a, b);
  assert.equal(a.length, 16);
  assert.ok(!a.includes('test-token'));
  assert.equal(fingerprint(''), 'anon');
});

test('resolveClientIp ignores X-Forwarded-For unless trustProxy is on', () => {
  const req = mockReq({
    remoteAddress: '10.0.0.5',
    // Express trust proxy 1 resolves the nearest untrusted hop.
    ip: '10.0.0.1',
    headers: { 'X-Forwarded-For': '203.0.113.9, 10.0.0.1' },
  });
  assert.equal(resolveClientIp(req, { trustProxy: false }), '10.0.0.5');
  assert.equal(resolveClientIp(req, { trustProxy: true }), '10.0.0.1');
});

test('resolveActorKey prefers token fingerprint over IP', () => {
  const req = mockReq({
    token: 'test-token-admin',
    remoteAddress: '10.0.0.5',
  });
  const key = resolveActorKey(req);
  assert.match(key, /^actor:/);
  assert.ok(!key.includes('test-token-admin'));
  assert.ok(!key.includes('10.0.0.5'));
});

test('sanitizeCorrelationId rejects oversized or unsafe values', () => {
  assert.equal(sanitizeCorrelationId('abc-123'), 'abc-123');
  assert.equal(sanitizeCorrelationId('  ok_id  '), 'ok_id');
  assert.equal(sanitizeCorrelationId('has space'), null);
  assert.equal(sanitizeCorrelationId('bad"quote'), null);
  assert.equal(sanitizeCorrelationId('x'.repeat(200)), null);
  assert.equal(sanitizeCorrelationId(''), null);
});

test('burst over max returns 429 with Retry-After and policy details', async () => {
  const limiter = rateLimit({
    name: 'mutation:transfers',
    windowMs: 60_000,
    max: 3,
    forceInTest: true,
    keyGenerator: () => 'actor:one',
  });

  for (let i = 0; i < 3; i += 1) {
    const { err } = await run(limiter, mockReq(), mockRes());
    assert.equal(err, null);
  }

  const { err, res } = await run(limiter, mockReq(), mockRes());
  assert.ok(err instanceof ApiError);
  assert.equal(err.statusCode, 429);
  assert.equal(err.details.policy, 'mutation:transfers');
  assert.equal(err.details.limit, 3);
  assert.ok(err.details.retryAfter >= 1);
  assert.equal(res.headers['Retry-After'], String(err.details.retryAfter));
  assert.equal(res.headers['X-RateLimit-Remaining'], '0');
});

test('identity isolation: one actor bursting does not block another', async () => {
  const limiter = rateLimit({
    name: 'mutation:transfers',
    windowMs: 60_000,
    max: 2,
    forceInTest: true,
    keyGenerator: (req) => resolveActorKey(req),
  });

  const actorA = mockReq({ token: 'token-a' });
  const actorB = mockReq({ token: 'token-b' });

  assert.equal((await run(limiter, actorA, mockRes())).err, null);
  assert.equal((await run(limiter, actorA, mockRes())).err, null);
  const blocked = await run(limiter, actorA, mockRes());
  assert.equal(blocked.err.statusCode, 429);

  const other = await run(limiter, actorB, mockRes());
  assert.equal(other.err, null);
});

test('proxy trust: forged X-Forwarded-For cannot rotate identity when trustProxy is false', async () => {
  const limiter = rateLimit({
    name: 'global',
    windowMs: 60_000,
    max: 2,
    forceInTest: true,
    trustProxy: false,
  });

  const first = mockReq({
    remoteAddress: '10.0.0.5',
    headers: { 'X-Forwarded-For': '198.51.100.1' },
  });
  const second = mockReq({
    remoteAddress: '10.0.0.5',
    headers: { 'X-Forwarded-For': '198.51.100.2' },
  });

  assert.equal((await run(limiter, first, mockRes())).err, null);
  assert.equal((await run(limiter, first, mockRes())).err, null);
  const blocked = await run(limiter, second, mockRes());
  assert.equal(blocked.err.statusCode, 429);
});

test('proxy trust: forged left-most X-Forwarded-For cannot rotate a one-hop identity', async () => {
  const app = express();
  app.set('trust proxy', 1);
  app.use(rateLimit({
    name: 'global',
    windowMs: 60_000,
    max: 1,
    forceInTest: true,
    trustProxy: true,
  }));
  app.get('/ping', (_req, res) => res.json({ ok: true }));
  app.use(errorHandler);

  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();

  try {
    async function ping(forwarded) {
      return fetch(`http://127.0.0.1:${port}/ping`, {
        headers: { 'X-Forwarded-For': forwarded },
      });
    }
    const first = await ping('198.51.100.1, 203.0.113.9');
    assert.equal(first.status, 200);
    const forged = await ping('198.51.100.2, 203.0.113.9');
    assert.equal(forged.status, 429);
    const other = await ping('198.51.100.2, 203.0.113.10');
    assert.equal(other.status, 200);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('maxKeys capacity preserves active budgets under identity flood', async () => {
  const limiter = rateLimit({
    name: 'global',
    windowMs: 60_000,
    max: 1,
    maxKeys: 5,
    forceInTest: true,
    keyGenerator: (req) => req.actor,
  });

  for (let i = 0; i < 5; i += 1) {
    const req = mockReq();
    req.actor = `actor-${i}`;
    assert.equal((await run(limiter, req, mockRes())).err, null);
  }

  const newcomer = mockReq();
  newcomer.actor = 'actor-new';
  const full = await run(limiter, newcomer, mockRes());
  assert.equal(full.err.statusCode, 429);
  assert.ok(Number(full.res.headers['Retry-After']) >= 1);
  assert.equal(limiter.size(), 5);

  const original = mockReq();
  original.actor = 'actor-0';
  assert.equal((await run(limiter, original, mockRes())).err.statusCode, 429);
});

test('correlation id is echoed on success and on 429 without leaking tokens', async () => {
  const app = express();
  app.use(requestId);
  app.use(
    rateLimit({
      name: 'mutation:quote',
      windowMs: 60_000,
      max: 1,
      forceInTest: true,
      keyGenerator: () => 'ip:test',
    })
  );
  app.get('/quote', (req, res) => {
    res.json({ ok: true, correlationId: req.correlationId });
  });
  app.use(errorHandler);

  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  const base = `http://127.0.0.1:${port}`;

  try {
    const first = await fetch(`${base}/quote`, {
      headers: { 'X-Correlation-Id': 'client-corr-1' },
    });
    assert.equal(first.status, 200);
    assert.equal(first.headers.get('x-correlation-id'), 'client-corr-1');
    assert.equal(first.headers.get('x-request-id'), 'client-corr-1');
    const firstBody = await first.json();
    assert.equal(firstBody.correlationId, 'client-corr-1');

    const second = await fetch(`${base}/quote`, {
      headers: {
        'X-Correlation-Id': 'client-corr-2',
        Authorization: 'Bearer test-token-admin',
      },
    });
    assert.equal(second.status, 429);
    assert.equal(second.headers.get('x-correlation-id'), 'client-corr-2');
    assert.ok(second.headers.get('retry-after'));
    const body = await second.json();
    assert.equal(body.error.status, 429);
    assert.equal(body.error.requestId, 'client-corr-2');
    assert.equal(body.error.details.policy, 'mutation:quote');
    const dumped = JSON.stringify(body);
    assert.ok(!dumped.includes('test-token-admin'));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('unsafe inbound correlation id is replaced, not echoed', async () => {
  const app = express();
  app.use(requestId);
  app.get('/ping', (req, res) => res.json({ id: req.id }));
  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();

  try {
    const res = await fetch(`http://127.0.0.1:${port}/ping`, {
      headers: { 'X-Request-Id': 'not a safe id' },
    });
    const body = await res.json();
    assert.notEqual(body.id, 'not a safe id');
    assert.equal(res.headers.get('x-request-id'), body.id);
    assert.equal(res.headers.get('x-correlation-id'), body.id);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

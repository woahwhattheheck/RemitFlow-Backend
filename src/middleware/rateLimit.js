'use strict';

const ApiError = require('../utils/ApiError');
const { resolveClientIp } = require('../utils/clientIdentity');

/**
 * In-memory fixed-window rate limiter with a bounded key table.
 *
 * Counters live only in process memory (fine for a single-node demo). A
 * production deployment should back this with a shared store such as Redis.
 * The map is capped by `maxKeys`: expired entries are pruned first, then the
 * oldest insert is dropped so a flood of unique identities cannot grow memory
 * without bound.
 *
 * Under `NODE_ENV=test` the limiter is a no-op unless
 * `ENABLE_RATE_LIMIT_IN_TEST=1`, so the rest of the suite is not coupled to
 * the abuse budget. Dedicated limiter and abuse-control tests opt back in.
 *
 * @param {object} [options]
 * @param {number} [options.windowMs] - length of the window in milliseconds.
 * @param {number} [options.max] - max requests allowed per window per key.
 * @param {number} [options.maxKeys] - hard cap on tracked identities.
 * @param {(req: import('express').Request) => string} [options.keyGenerator]
 * @param {string} [options.name] - label included in 429 details (no secrets).
 * @param {boolean} [options.trustProxy] - whether to honour X-Forwarded-For.
 * @param {boolean} [options.forceInTest] - enforce even when NODE_ENV=test.
 * @returns {import('express').RequestHandler & { reset: Function, size: Function }}
 */
function rateLimit(options = {}) {
  const windowMs = options.windowMs || 60 * 1000;
  const max = options.max || 100;
  const maxKeys = options.maxKeys || 10_000;
  const name = options.name || 'default';
  const trustProxy = Boolean(options.trustProxy);
  const forceInTest = Boolean(options.forceInTest);
  const keyGenerator =
    options.keyGenerator ||
    ((req) => resolveClientIp(req, { trustProxy }));

  /** @type {Map<string, { count: number, resetAt: number, touchedAt: number }>} */
  const hits = new Map();

  function pruneExpired(now) {
    for (const [key, entry] of hits) {
      if (now >= entry.resetAt) {
        hits.delete(key);
      }
    }
  }

  function evictIfNeeded(now) {
    if (hits.size < maxKeys) {
      return;
    }
    pruneExpired(now);
    while (hits.size >= maxKeys) {
      // Map iteration order is insertion order; drop the oldest key.
      const oldest = hits.keys().next().value;
      if (oldest === undefined) {
        break;
      }
      hits.delete(oldest);
    }
  }

  function rateLimitMiddleware(req, res, next) {
    const skipForTest =
      process.env.NODE_ENV === 'test' &&
      process.env.ENABLE_RATE_LIMIT_IN_TEST !== '1' &&
      !forceInTest;
    if (skipForTest) {
      return next();
    }

    const now = Date.now();
    const key = keyGenerator(req) || 'unknown';
    let entry = hits.get(key);

    if (!entry || now >= entry.resetAt) {
      evictIfNeeded(now);
      entry = { count: 0, resetAt: now + windowMs, touchedAt: now };
      // Re-insert so the key moves to the end of insertion order.
      hits.delete(key);
      hits.set(key, entry);
    }

    entry.count += 1;
    entry.touchedAt = now;

    const remaining = Math.max(0, max - entry.count);
    res.set('X-RateLimit-Limit', String(max));
    res.set('X-RateLimit-Remaining', String(remaining));
    res.set('X-RateLimit-Reset', String(Math.ceil(entry.resetAt / 1000)));
    res.set('X-RateLimit-Policy', name);

    if (entry.count > max) {
      const retryAfter = Math.max(1, Math.ceil((entry.resetAt - now) / 1000));
      res.set('Retry-After', String(retryAfter));
      return next(
        ApiError.tooManyRequests('Too many requests, please try again later', {
          retryAfter,
          limit: max,
          windowMs,
          policy: name,
        })
      );
    }

    return next();
  }

  rateLimitMiddleware.reset = function reset() {
    hits.clear();
  };

  rateLimitMiddleware.size = function size() {
    return hits.size;
  };

  return rateLimitMiddleware;
}

module.exports = rateLimit;

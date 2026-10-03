'use strict';

const ApiError = require('../utils/ApiError');
const { resolveClientIp } = require('../utils/clientIdentity');

/**
 * In-memory fixed-window rate limiter with a bounded key table.
 *
 * Counters live only in process memory (fine for a single-node demo). A
 * production deployment should back this with a shared store such as Redis.
 * The map is capped by `maxKeys`: expired entries are pruned first. When
 * all slots are live, new identities receive 429 until a slot expires rather
 * than evicting an active budget and allowing repeat attempts.
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
  let nextResetAt = Infinity;

  function pruneExpired(now) {
    // A full table should reject new identities without rescanning live budgets.
    if (now < nextResetAt) return;
    nextResetAt = Infinity;
    for (const [key, entry] of hits) {
      if (now >= entry.resetAt) {
        hits.delete(key);
      } else {
        nextResetAt = Math.min(nextResetAt, entry.resetAt);
      }
    }
  }

  function hasCapacity(now) {
    // Also prune before renewing an expired identity below capacity, so the
    // cached deadline remains exact if the wall clock later moves backward.
    pruneExpired(now);
    return hits.size < maxKeys;
  }

  function capacityRetryAfter(now) {
    return Math.max(1, Math.ceil((nextResetAt - now) / 1000));
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
      if (!hasCapacity(now)) {
        const retryAfter = capacityRetryAfter(now);
        res.set('X-RateLimit-Limit', String(max));
        res.set('X-RateLimit-Remaining', '0');
        res.set('X-RateLimit-Policy', name);
        res.set('Retry-After', String(retryAfter));
        return next(
          ApiError.tooManyRequests('Rate limit capacity reached, please try again later', {
            retryAfter,
            limit: max,
            windowMs,
            policy: name,
          })
        );
      }
      entry = { count: 0, resetAt: now + windowMs, touchedAt: now };
      hits.delete(key);
      hits.set(key, entry);
      nextResetAt = Math.min(nextResetAt, entry.resetAt);
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
    nextResetAt = Infinity;
  };

  rateLimitMiddleware.size = function size() {
    return hits.size;
  };

  return rateLimitMiddleware;
}

module.exports = rateLimit;

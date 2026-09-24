'use strict';

const config = require('../config');
const fxProviders = require('./fxProviders');
const ApiError = require('../utils/ApiError');

/**
 * Bounded FX rate cache with stampede protection and explicit stale policy.
 *
 * Cache key is a single global snapshot: RemitFlow converts via USD cross rates,
 * so one provider pull feeds every pair. TTL bounds how long a snapshot is
 * "fresh"; staleGraceMs bounds how long an expired snapshot may still be served
 * under allow_stale / outage policy.
 *
 * Stampede control is synchronous singleflight: the first miss starts a
 * provider fetch and marks the key in-flight. A re-entrant or concurrent caller
 * that arrives while the fetch is running must NOT start another provider call.
 * It either receives a still-within-grace cached snapshot marked stale, or a
 * 503 with code FX_REFRESH_IN_PROGRESS when no usable cache exists.
 */

const CACHE_KEY = 'rates:usd';

/** @type {Map<string, { ratesToUsd: object, fetchedAt: number, providerId: string, expiresAt: number }>} */
const cache = new Map();

/** @type {Set<string>} keys currently being refreshed */
const inflight = new Set();

/** Counter for tests: how many times providers were actually invoked. */
let providerFetchCount = 0;

function ttlMs() {
  return config.fx.cacheTtlMs;
}

function staleGraceMs() {
  return config.fx.staleGraceMs;
}

/**
 * Classify a cache entry relative to `now`.
 * @param {{ fetchedAt: number, expiresAt: number }} entry
 * @param {number} now
 * @returns {'fresh'|'stale'|'expired'}
 */
function classify(entry, now) {
  if (now < entry.expiresAt) return 'fresh';
  if (now < entry.expiresAt + staleGraceMs()) return 'stale';
  return 'expired';
}

/**
 * Attach freshness metadata without mutating the stored entry.
 * @param {object} entry
 * @param {number} now
 * @param {{ cacheHit: boolean, source: string }} extra
 */
function decorate(entry, now, extra) {
  const status = classify(entry, now);
  return {
    ratesToUsd: { ...entry.ratesToUsd },
    providerId: entry.providerId,
    fetchedAt: entry.fetchedAt,
    expiresAt: entry.expiresAt,
    ageMs: Math.max(0, now - entry.fetchedAt),
    status,
    stale: status !== 'fresh',
    cacheHit: Boolean(extra.cacheHit),
    source: extra.source,
  };
}

/**
 * Pull from providers (counted), store under TTL, return decorated fresh snapshot.
 * @param {number} now
 */
function refresh(now) {
  providerFetchCount += 1;
  const snapshot = fxProviders.fetchWithFallback({ now });
  const entry = {
    ratesToUsd: { ...snapshot.ratesToUsd },
    fetchedAt: snapshot.fetchedAt,
    providerId: snapshot.providerId,
    expiresAt: snapshot.fetchedAt + ttlMs(),
  };
  cache.set(CACHE_KEY, entry);
  return decorate(entry, now, { cacheHit: false, source: 'provider' });
}

/**
 * Resolve a rate snapshot under an explicit stale policy.
 *
 * Policies:
 * - `reject_stale` (transfer pricing): only fresh snapshots are usable. On
 *   provider outage we still refuse stale data rather than price a transfer
 *   on an expired rate.
 * - `allow_stale` (display / quotes for inspection): may return a within-grace
 *   stale snapshot when providers are down or a refresh is in flight. The
 *   response always marks `stale: true` so callers cannot mistake it for current.
 *
 * @param {object} [opts]
 * @param {number} [opts.now]
 * @param {'reject_stale'|'allow_stale'} [opts.policy]
 * @returns {ReturnType<typeof decorate>}
 */
function getSnapshot(opts = {}) {
  const now = opts.now != null ? opts.now : Date.now();
  const policy = opts.policy || 'reject_stale';
  const entry = cache.get(CACHE_KEY);

  if (entry) {
    const status = classify(entry, now);
    if (status === 'fresh') {
      return decorate(entry, now, { cacheHit: true, source: 'cache' });
    }
  }

  // Stampede guard: someone is already talking to providers for this key.
  if (inflight.has(CACHE_KEY)) {
    if (entry && classify(entry, now) === 'stale' && policy === 'allow_stale') {
      return decorate(entry, now, { cacheHit: true, source: 'cache-stale-inflight' });
    }
    throw ApiError.serviceUnavailable('FX rate refresh in progress', {
      code: 'FX_REFRESH_IN_PROGRESS',
    });
  }

  inflight.add(CACHE_KEY);
  try {
    return refresh(now);
  } catch (err) {
    // Provider path failed. Under allow_stale, a within-grace entry is still
    // usable for display — but it is visibly stale. Under reject_stale we
    // never price with it.
    if (entry && classify(entry, now) === 'stale' && policy === 'allow_stale') {
      return decorate(entry, now, { cacheHit: true, source: 'cache-stale-outage' });
    }
    throw err;
  } finally {
    inflight.delete(CACHE_KEY);
  }
}

/**
 * Read the cached entry without refreshing. Used by tests and diagnostics.
 * @param {number} [now]
 */
function peek(now = Date.now()) {
  const entry = cache.get(CACHE_KEY);
  if (!entry) return null;
  return decorate(entry, now, { cacheHit: true, source: 'peek' });
}

/** Drop cache and inflight state. */
function reset() {
  cache.clear();
  inflight.clear();
  providerFetchCount = 0;
}

function getProviderFetchCount() {
  return providerFetchCount;
}

/**
 * Seed the cache (tests). `fetchedAt` defaults to now; TTL applied from config.
 * @param {object} partial
 */
function seed(partial = {}) {
  const now = partial.fetchedAt != null ? partial.fetchedAt : Date.now();
  const entry = {
    ratesToUsd: { ...(partial.ratesToUsd || require('../config/rates').RATES_TO_USD) },
    fetchedAt: now,
    providerId: partial.providerId || 'primary',
    expiresAt: partial.expiresAt != null ? partial.expiresAt : now + ttlMs(),
  };
  cache.set(CACHE_KEY, entry);
  return entry;
}

module.exports = {
  CACHE_KEY,
  getSnapshot,
  peek,
  reset,
  seed,
  classify,
  getProviderFetchCount,
  ttlMs,
  staleGraceMs,
};

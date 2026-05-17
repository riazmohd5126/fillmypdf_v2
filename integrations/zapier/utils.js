'use strict';

const querystring = require('querystring');

/** @param {string | undefined} url */
function normalizeBaseUrl(url) {
  const u = String(url || '').trim().replace(/\/+$/, '');
  return u || 'http://localhost:8000';
}

/** @param {Record<string, string | number | boolean | undefined | null>} fields */
function encodeForm(fields) {
  const compact = {};
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null || v === '') continue;
    compact[k] = String(v);
  }
  return querystring.stringify(compact);
}

/** @param {import('zapier-platform-core').Bundle} bundle */
function authHeaders(bundle) {
  return {
    'X-API-Key': bundle.authData.api_key,
  };
}

/**
 * Relative paths from API (e.g. /api/v1/jobs/…) → absolute URL for Zap next steps.
 * @param {string} base
 * @param {string | null | undefined} path
 */
function absoluteUrl(base, path) {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return `${normalizeBaseUrl(base)}${path.startsWith('/') ? path : `/${path}`}`;
}

module.exports = {
  normalizeBaseUrl,
  encodeForm,
  authHeaders,
  absoluteUrl,
};

'use strict';

const { normalizeBaseUrl, authHeaders } = require('./utils');

module.exports = {
  type: 'custom',
  fields: [
    {
      key: 'base_url',
      label: 'API base URL',
      required: true,
      default: 'http://localhost:8000',
      helpText:
        'Origin only (no trailing slash), e.g. https://your-fillmypdf-host.example or http://localhost:8000',
    },
    {
      key: 'api_key',
      label: 'FillMyPDF API key',
      required: true,
      type: 'password',
      helpText: 'Same key used as HTTP header X-API-Key (create under API Keys in your deployment).',
    },
  ],
  connectionLabel: '{{bundle.authData.base_url}}',
  test: async (z, bundle) => {
    const base = normalizeBaseUrl(bundle.authData.base_url);
    const res = await z.request({
      url: `${base}/api/v1/templates`,
      headers: { ...authHeaders(bundle) },
    });
    if (res.status !== 200) {
      throw new Error(`FillMyPDF auth failed (${res.status}). Check API base URL and X-API-Key.`);
    }
    return res.json;
  },
};

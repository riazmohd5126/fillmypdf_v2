'use strict';

const { normalizeBaseUrl, authHeaders, absoluteUrl } = require('../utils');

module.exports = {
  key: 'get_job_status',
  noun: 'Job',
  display: {
    label: 'Get Job Status',
    description:
      'Poll GET /api/v1/jobs/{job_id}. When status is done, download_url points at the ZIP/PDF artifact.',
  },
  operation: {
    inputFields: [
      {
        key: 'job_id',
        label: 'Job ID',
        required: true,
        helpText: 'Returned by Submit Async … actions (e.g. job_a1b2c3d4e5f6).',
      },
    ],
    perform: async (z, bundle) => {
      const base = normalizeBaseUrl(bundle.authData.base_url);
      const id = encodeURIComponent(bundle.inputData.job_id.trim());

      const res = await z.request({
        url: `${base}/api/v1/jobs/${id}`,
        headers: { ...authHeaders(bundle) },
      });

      if (res.status === 404) {
        throw new Error(`Job '${bundle.inputData.job_id}' not found`);
      }
      if (res.status !== 200) {
        const detail = typeof res.json === 'object' ? JSON.stringify(res.json) : String(res.content);
        throw new Error(`FillMyPDF returned ${res.status}: ${detail}`);
      }

      const j = res.json;
      return {
        ...j,
        status_url_absolute: `${base}/api/v1/jobs/${id}`,
        download_url_absolute: absoluteUrl(bundle.authData.base_url, j.download_url),
      };
    },
    sample: {
      id: 'job_sample123456',
      status: 'done',
      kind: 'batch_fill',
      template_id: null,
      record_count: 2,
      progress_pct: 100,
      completed: 2,
      successful: 2,
      failed: 0,
      created_at: '2026-05-09T12:00:00+00:00',
      started_at: '2026-05-09T12:00:01+00:00',
      completed_at: '2026-05-09T12:03:45+00:00',
      download_url: '/api/v1/batch/download/job_sample123456.zip',
      download_url_absolute: 'http://localhost:8000/api/v1/batch/download/job_sample123456.zip',
      status_url_absolute: 'http://localhost:8000/api/v1/jobs/job_sample123456',
      avg_confidence: 0.94,
      cache_hits: 0,
      error: null,
      webhook_url: null,
      webhook_delivered: false,
    },
  },
};

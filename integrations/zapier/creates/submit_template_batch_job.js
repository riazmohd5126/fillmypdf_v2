'use strict';

const {
  normalizeBaseUrl,
  encodeForm,
  authHeaders,
  absoluteUrl,
} = require('../utils');

module.exports = {
  key: 'submit_template_batch_job',
  noun: 'Job',
  display: {
    label: 'Submit Async Template Batch Fill',
    description:
      'Queue fills for multiple records against a stored template ID. Returns job_id — use Get Job Status or configure webhook_url (e.g. Zapier Catch Hook).',
  },
  operation: {
    inputFields: [
      {
        key: 'template_id',
        label: 'Template ID',
        required: true,
        helpText:
          'Manifest id (e.g. pa_linzess_molina_tx). Run Search “Find Template” in Zapier first, map `id` from that step.',
      },
      {
        key: 'records',
        label: 'Records (JSON array)',
        required: true,
        type: 'text',
        default: '[{"patient_name":"Example"}]',
        helpText: 'JSON array of row objects passed to the AI mapper (same as API `records`).',
      },
      {
        key: 'mapping_llm_token',
        label: 'LLM credential (Gemini/OpenAI-compatible)',
        required: true,
        type: 'password',
        helpText:
          'Sent to FillMyPDF as `ai_api_key` — your model-provider token (not FillMyPDF `X-API-Key`).',
      },
      {
        key: 'ai_base_url',
        label: 'AI base URL',
        required: false,
        default: 'https://generativelanguage.googleapis.com/v1beta/openai/',
      },
      {
        key: 'ai_model',
        label: 'AI model',
        required: false,
        default: 'gemini-2.5-flash',
      },
      {
        key: 'dpi',
        label: 'Render DPI',
        required: false,
        default: '200',
      },
      {
        key: 'profile_id',
        label: 'Profile ID (optional)',
        required: false,
        helpText: 'Encrypted profile ID to merge before per-row data.',
      },
      {
        key: 'webhook_url',
        label: 'Completion webhook URL (optional)',
        required: false,
        helpText: 'e.g. a Zapier Catch Hook URL — FillMyPDF POSTs job JSON when finished.',
      },
      {
        key: 'webhook_secret',
        label: 'Webhook HMAC secret (optional)',
        required: false,
        type: 'password',
      },
    ],
    perform: async (z, bundle) => {
      const base = normalizeBaseUrl(bundle.authData.base_url);
      const body = encodeForm({
        template_id: bundle.inputData.template_id,
        records: bundle.inputData.records,
        ai_api_key: bundle.inputData.mapping_llm_token,
        ai_base_url: bundle.inputData.ai_base_url,
        ai_model: bundle.inputData.ai_model,
        dpi: bundle.inputData.dpi,
        profile_id: bundle.inputData.profile_id,
        webhook_url: bundle.inputData.webhook_url,
        webhook_secret: bundle.inputData.webhook_secret,
      });

      const res = await z.request({
        url: `${base}/api/v1/jobs/template-batch`,
        method: 'POST',
        headers: {
          ...authHeaders(bundle),
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body,
      });

      if (res.status !== 202) {
        const detail = typeof res.json === 'object' ? JSON.stringify(res.json) : String(res.content);
        throw new Error(`FillMyPDF returned ${res.status}: ${detail}`);
      }

      const j = res.json;
      return {
        ...j,
        status_url_absolute: absoluteUrl(bundle.authData.base_url, j.status_url),
      };
    },
    sample: {
      job_id: 'job_sample123456',
      status: 'queued',
      message: "Job queued — 1 records against template 'pa_demo'",
      status_url: '/api/v1/jobs/job_sample123456',
      status_url_absolute: 'http://localhost:8000/api/v1/jobs/job_sample123456',
    },
  },
};

'use strict';

const FormData = require('form-data');
const {
  normalizeBaseUrl,
  authHeaders,
  absoluteUrl,
} = require('../utils');

module.exports = {
  key: 'submit_pdf_batch_job',
  noun: 'Job',
  display: {
    label: 'Submit Async Batch Fill (PDF URL)',
    description:
      'Download a template PDF from a public HTTPS URL and queue an async batch fill (multipart upload equivalent to POST /api/v1/jobs/batch).',
  },
  operation: {
    inputFields: [
      {
        key: 'pdf_template_url',
        label: 'PDF template URL',
        required: true,
        helpText:
          'Direct HTTPS URL to the PDF Zapier can GET without cookies (use a share link or prior Zap step file URL).',
      },
      {
        key: 'records',
        label: 'Records (JSON array)',
        required: true,
        type: 'text',
        default: '[{"patient_name":"Example"}]',
      },
      {
        key: 'mapping_llm_token',
        label: 'LLM credential (Gemini/OpenAI-compatible)',
        required: true,
        type: 'password',
        helpText: 'Sent to FillMyPDF as `ai_api_key` (provider token).',
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
      },
      {
        key: 'webhook_url',
        label: 'Completion webhook URL (optional)',
        required: false,
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

      const pdfRes = await z.request({
        url: bundle.inputData.pdf_template_url,
        raw: true,
      });

      if (pdfRes.status >= 400) {
        throw new Error(`PDF download failed (${pdfRes.status})`);
      }

      const pdfBuffer =
        pdfRes.buffer ||
        pdfRes.body ||
        (typeof pdfRes.buffer === 'function' ? await pdfRes.buffer() : null);

      if (!pdfBuffer || !(pdfBuffer.length >= 4)) {
        throw new Error('PDF download returned empty body');
      }

      const form = new FormData();
      form.append('file', pdfBuffer, {
        filename: 'template.pdf',
        contentType: 'application/pdf',
      });
      form.append('records', bundle.inputData.records);
      form.append('ai_api_key', bundle.inputData.mapping_llm_token);
      if (bundle.inputData.ai_base_url) form.append('ai_base_url', bundle.inputData.ai_base_url);
      if (bundle.inputData.ai_model) form.append('ai_model', bundle.inputData.ai_model);
      form.append('dpi', String(bundle.inputData.dpi || 200));
      if (bundle.inputData.profile_id) form.append('profile_id', bundle.inputData.profile_id);
      if (bundle.inputData.webhook_url) form.append('webhook_url', bundle.inputData.webhook_url);
      if (bundle.inputData.webhook_secret) form.append('webhook_secret', bundle.inputData.webhook_secret);

      const res = await z.request({
        url: `${base}/api/v1/jobs/batch`,
        method: 'POST',
        body: form,
        headers: {
          ...form.getHeaders(),
          ...authHeaders(bundle),
        },
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
      job_id: 'job_samplepdf12',
      status: 'queued',
      message: 'Job queued — 1 records',
      status_url: '/api/v1/jobs/job_samplepdf12',
      status_url_absolute: 'http://localhost:8000/api/v1/jobs/job_samplepdf12',
    },
  },
};

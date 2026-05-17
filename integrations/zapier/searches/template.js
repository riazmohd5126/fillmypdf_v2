'use strict';

const { normalizeBaseUrl, authHeaders } = require('../utils');

/** Powers dynamic dropdowns for template_id on Creates. */
module.exports = {
  key: 'template',
  noun: 'Template',
  display: {
    label: 'Find Template',
    description: 'Search the FillMyPDF template library (optional filters).',
  },
  operation: {
    inputFields: [
      {
        key: 'drug',
        label: 'Drug name contains',
        required: false,
      },
      {
        key: 'payer',
        label: 'Payer name contains',
        required: false,
      },
      {
        key: 'state',
        label: 'State (2-letter)',
        required: false,
      },
    ],
    perform: async (z, bundle) => {
      const base = normalizeBaseUrl(bundle.authData.base_url);
      const params = {};
      if (bundle.inputData.drug) params.drug = bundle.inputData.drug;
      if (bundle.inputData.payer) params.payer = bundle.inputData.payer;
      if (bundle.inputData.state) params.state = bundle.inputData.state;

      const res = await z.request({
        url: `${base}/api/v1/templates`,
        headers: { ...authHeaders(bundle) },
        params,
      });
      if (res.status !== 200) {
        throw new Error(`List templates failed (${res.status})`);
      }
      const templates = res.json.templates || [];
      return templates.map((t) => ({
        id: t.id,
        name: t.name || t.id,
        label: `${t.name || t.id} (${t.id})`,
        drug_name: t.drug_name,
        payer_name: t.payer_name,
        state: t.state,
      }));
    },
    sample: {
      id: 'pa_example_tx',
      name: 'Example PA — TX',
      label: 'Example PA — TX (pa_example_tx)',
      drug_name: 'Example',
      payer_name: 'Example Payer',
      state: 'TX',
    },
  },
};

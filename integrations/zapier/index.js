'use strict';

const authentication = require('./authentication');
const templateSearch = require('./searches/template');
const submitTemplateBatch = require('./creates/submit_template_batch_job');
const submitPdfBatch = require('./creates/submit_pdf_batch_job');
const getJobStatus = require('./creates/get_job_status');

module.exports = {
  version: require('./package.json').version,
  platformVersion: require('zapier-platform-core').version,

  authentication,

  searches: {
    [templateSearch.key]: templateSearch,
  },

  creates: {
    [submitTemplateBatch.key]: submitTemplateBatch,
    [submitPdfBatch.key]: submitPdfBatch,
    [getJobStatus.key]: getJobStatus,
  },
};

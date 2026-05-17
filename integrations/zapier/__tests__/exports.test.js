'use strict';

describe('integration module', () => {
  test('exports authentication, searches, creates', () => {
    const app = require('../index');

    expect(app.authentication).toBeTruthy();
    expect(app.authentication.fields.length).toBeGreaterThanOrEqual(2);

    expect(app.searches.template).toBeTruthy();
    expect(app.creates.submit_template_batch_job).toBeTruthy();
    expect(app.creates.submit_pdf_batch_job).toBeTruthy();
    expect(app.creates.get_job_status).toBeTruthy();

    expect(app.version).toMatch(/^\d+\.\d+\.\d+/);
  });
});

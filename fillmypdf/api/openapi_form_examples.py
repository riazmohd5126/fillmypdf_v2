"""
OpenAPI examples for multipart form fields (Swagger UI / Redoc).

Use with: Annotated[str, Form(...), Field(examples=[...])]
"""

# JSON array as a single form field (paste into "records" / "user_data_array")
EX_JSON_RECORDS_TWO = (
    '[{"first_name":"Ada","patient_mrn":"P-1001"},'
    '{"first_name":"Bob","patient_mrn":"P-1002"}]'
)

EX_TEMPLATE_ID = "pa_linzess_molina_tx"

EX_AI_API_KEY = "YOUR_LLM_API_KEY"
EX_AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
EX_AI_MODEL = "gemini-2.5-flash"

EX_PROFILE_ID = "prof_abc123def456"
EX_WEBHOOK_URL = "https://hooks.zapier.com/hooks/catch/000000/xxxxxxxx/"
EX_WEBHOOK_SECRET = "whsec_signing_secret_optional"

# Single JSON object for POST /templates/{id}/fill (user_data form field)
EX_USER_DATA_SINGLE = (
    '{"patient_first_name":"Ada","patient_last_name":"Lovelace",'
    '"member_id":"H1234567","dob":"1985-06-09"}'
)

# Minimal valid TemplateManifest JSON for admin upload (manifest_json)
EX_MANIFEST_JSON_MIN = (
    '{"id":"upload_demo_pa","name":"Demo prior auth","category":"prior_authorization",'
    '"drug":{"name":"DemoDrug","generic_name":"demodrug"},"payer":{"name":"DemoPayer","state":"TX"},'
    '"questions":[{"key":"clinical_benefit","text":"Does patient meet criteria?","type":"yesno"}],'
    '"tags":["demo","upload"]}'
)

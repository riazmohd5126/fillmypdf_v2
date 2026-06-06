# 🚀 FillMyPDF - Production OOP System

**AI-Powered PDF Auto-Fill with User Profiles & Batch Processing**

Version 4.0.0 - Production Ready

---

## ✨ Features

### **Already Working** ✅
- ✅ **User Profiles** - Save data once, reuse everywhere (AES-256 encrypted)
- ✅ **Batch CSV Processing** - Upload CSV + PDF → Get ZIP with filled PDFs
- ✅ **Batch JSON Processing** - Process JSON arrays
- ✅ **Profile + Batch Integration** - Merge profiles with batch data automatically
- ✅ **AI Vision Auto-Fill** - Gemini reads form labels, maps data by position
- ✅ **PDF Conversion** - Static PDF → Fillable PDF
- ✅ **Clean OOP Architecture** - Scalable, maintainable codebase
- ✅ **API keys + rate limits** (`X-API-Key`), async jobs, template library, bulk extract/webhooks ([feature matrix](./docs/FEATURE_MATRIX_ROUTE.md))

### **Coming Soon** 🔜
- 🔜 **User dashboard product UI** — **MVP at [`/dashboard`](http://localhost:8000/dashboard)** (profiles, template search, draw/type e-sign); full billing/history still roadmap
- 🔜 **E-Signatures / multi-sign** — roadmap Phase 3 (see [`docs/feature-matrix.json`](./docs/feature-matrix.json))
- 🔜 **Optional Zapier marketplace app** — **today:** same **REST + OpenAPI** autofill API for any client; job **webhooks** work with Zapier Catch Hook / similar tools. **Private Zap CLI app:** [`integrations/zapier/README.md`](./integrations/zapier/README.md)

Canonical roadmap: **`docs/FEATURE_MATRIX_ROUTE.md`** · React matrix viewer · **`docs/FeatureMatrix.jsx`** + **`docs/feature-matrix.json`**

---

## Running Fully On-Prem (HIPAA / Local Qwen)

By default FillMyPDF uses Google Gemini for the AI field-mapping step. For
HIPAA compliance you can switch to a **self-hosted Qwen model** so **no
patient data ever leaves your server**. `commonforms` (field detection) and
all PDF filling already run locally — only the LLM call needs to change.

### Step 1 — Install Ollama and pull a model

```bash
# macOS
brew install ollama
ollama serve &                         # starts on port 11434

# 8 GB Mac (comfortable fit)
ollama pull qwen2.5:3b-instruct

# 16 GB+ or a dedicated GPU box
ollama pull qwen2.5:7b-instruct
```

To use a **separate machine** (recommended for production), replace
`localhost` below with that host's IP:

```
LOCAL_AI_BASE_URL=http://192.168.1.42:11434/v1
LOCAL_AI_MODEL=qwen2.5:7b-instruct
```

### Step 2 — Configure the app

In your `.env` file:

```env
AI_PROVIDER=local          # route all LLM calls to Ollama instead of Gemini
LOCAL_AI_BASE_URL=http://localhost:11434/v1
LOCAL_AI_MODEL=qwen2.5:3b-instruct
LOCAL_AI_API_KEY=ollama    # Ollama ignores this; any non-empty value works

# Hard guardrail: reject any request that would send data to an external host
AI_LOCAL_ONLY=True
```

No Gemini API key is needed in local mode. In the UI the dashboard navbar
will show a **"Local Qwen · HIPAA Mode"** green badge confirming the setting.

### Step 3 — Verify

```bash
curl -s http://localhost:8000/ai-provider | python3 -m json.tool
# Should show: "ai_provider": "local", "hipaa_mode": true
```

Batch fill requests can also override the provider per-request:

```bash
curl -X POST http://localhost:8000/api/v1/batch/fill-csv \
  -H "X-API-Key: <your-key>" \
  -F "pdf_template=@form.pdf" \
  -F "csv_file=@data.csv" \
  -F "ai_provider=local"   # no ai_api_key needed
```

---

## 🚀 Quick Start

### **1. Extract Project**
```bash
cd ~/Desktop
unzip FillMyPDF_OOP_Complete.zip
cd FillMyPDF_OOP
```

### **2. Install Dependencies**
```bash
pip3 install -r requirements.txt

# macOS
brew install poppler

# Ubuntu
sudo apt install poppler-utils
```

### **3. Configure**
```bash
cp .env.example .env
# Edit .env and set PROFILES_ENCRYPTION_KEY to a strong random value
```

### **4. Start Server**
```bash
python3 -m fillmypdf.main
```

### **5. Open API Docs**
```
http://localhost:8000/docs
```

---

## 📖 Usage Examples

### **Example 1: Create Profile**

```bash
curl -X POST http://localhost:8000/api/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Company",
    "profile_type": "business",
    "data": {
      "company_name": "Acme Corp",
      "company_address": "123 Business St",
      "company_phone": "(555) 999-0000",
      "ein": "12-3456789"
    }
  }'
```

**Response:**
```json
{
  "id": "prof_abc123def456",
  "name": "My Company",
  "profile_type": "business",
  "created_at": "2026-02-28T10:30:00",
  "usage_count": 0,
  "data_preview": {
    "company_name": "Acme Corp",
    "company_address": "123 Business St",
    "company_phone": "(555) 999-0000"
  }
}
```

Note: `ein` is encrypted and not shown in preview!

---

### **Example 2: Batch CSV with Profile**

**Create CSV (employees.csv):**
```csv
first_name,last_name,email,phone
John,Doe,john@acme.com,(555) 111-1111
Jane,Smith,jane@acme.com,(555) 222-2222
Bob,Johnson,bob@acme.com,(555) 333-3333
```

**Upload via API docs or curl:**
```bash
curl -X POST http://localhost:8000/api/v1/batch/fill-csv \
  -F "pdf_template=@w4_form.pdf" \
  -F "csv_file=@employees.csv" \
  -F "ai_api_key=YOUR_GEMINI_API_KEY" \
  -F "profile_id=prof_abc123def456" \
  -o result.zip
```

**Result:**
- Each PDF has: Company info (from profile) + Employee data (from CSV)
- ZIP contains: John_Doe.pdf, Jane_Smith.pdf, Bob_Johnson.pdf, batch_report.json

---

## 🏗️ Architecture

```
fillmypdf/
├── main.py                    # FastAPI application
├── config.py                  # Settings
│
├── models/                    # Pydantic schemas
│   └── __init__.py           # Profile, Batch, Response models
│
├── services/                  # Business logic
│   ├── profile_service.py    # Profile management
│   ├── batch_fill_service.py # Batch processing
│   ├── vision_service.py     # AI integration
│   └── pdf_service.py        # PDF operations
│
├── repositories/              # Data access
│   └── profile_repository.py # Profile persistence
│
├── utils/                     # Utilities
│   └── encryption.py         # AES-256 encryption
│
├── api/routes/               # API endpoints
│   ├── profiles.py           # Profile CRUD
│   └── batch_routes.py       # Batch operations
│
└── storage/                   # Data files
    └── profiles/             # Encrypted profiles
```

---

## 🔒 Security

### **Encryption**
- **Algorithm**: AES-256-GCM (authenticated encryption)
- **Key Derivation**: PBKDF2 with 100,000 iterations
- **Per-Encryption**: Random salt + nonce

### **What's Encrypted**
Automatically detects and encrypts sensitive fields:
- SSN, Tax IDs, EIN, ITIN
- Bank accounts, routing numbers
- Credit/debit card numbers
- Passwords, PINs
- Date of birth
- License/passport numbers

### **What's NOT Encrypted**
- Names, emails (needed for search/display)
- Company names, addresses
- Phone numbers
- Non-sensitive fields

---

## 📊 API Endpoints

### **Profiles**
```
POST   /api/v1/profiles          Create profile
GET    /api/v1/profiles          List all profiles
GET    /api/v1/profiles/{id}     Get profile
PATCH  /api/v1/profiles/{id}     Update profile
DELETE /api/v1/profiles/{id}     Delete profile
```

### **Batch Processing**
```
POST /api/v1/batch/fill-json     Batch fill from JSON array
POST /api/v1/batch/fill-csv      Batch fill from CSV upload
GET  /api/v1/batch/download/{file}  Download ZIP result
```

### **System**
```
GET  /health                      Health check
GET  /usage                       Usage statistics
```

---

## 🎯 Use Cases

### **HR Onboarding**
1. Create profile with company info (EIN, address, etc.)
2. Export new employees from HRIS to CSV
3. Batch fill W-4 forms
4. **Result**: 50 W-4s in 2 minutes instead of 500 minutes!

### **Tax Season**
1. Create profile with firm details
2. Export client data to CSV
3. Batch fill 1099 forms
4. **Result**: 200 1099s in 3 minutes!

### **Real Estate**
1. Create profile with agency info
2. CSV with property-specific data
3. Batch fill disclosure forms
4. **Result**: 30 disclosures automatically!

---

## 🔧 Configuration

Edit `.env` file:

```bash
# Security - CRITICAL: Change in production!
PROFILES_ENCRYPTION_KEY=use-a-strong-random-key-here

# Profile limits
PROFILES_FREE_LIMIT=1          # Free tier: 1 profile
PROFILES_PRO_LIMIT=-1          # Pro tier: unlimited

# AI defaults
DEFAULT_AI_MODEL=gemini-2.5-flash
DEFAULT_DPI=200
```

---

## 🐛 Troubleshooting

### **Error: "Port 8000 already in use"**
```bash
lsof -ti:8000 | xargs kill -9
```

### **Error: "Module not found"**
```bash
pip3 install -r requirements.txt
```

### **Error: "Poppler not found"**
```bash
# macOS
brew install poppler

# Ubuntu
sudo apt install poppler-utils
```

---

## 📈 Performance

| Operation | Time | Notes |
|-----------|------|-------|
| Single PDF | ~20s | Convert + AI + fill |
| Batch 10 PDFs | ~2min | Convert once + batch |
| Batch 100 PDFs | ~15min | May hit API limits |
| Profile Create | <100ms | With encryption |
| Profile Load | <50ms | With decryption |

---

## 🚀 Next Steps

### **Day 2: Add Authentication**
1. API key generation
2. Rate limiting (100/day free, 10k/day pro)
3. Usage tracking
4. Ready for monetization!

See `AUTHENTICATION_GUIDE.md` (coming next!)

---

## 💰 Revenue Potential

After adding authentication:

| Tier | Limits | Price | Target |
|------|--------|-------|--------|
| Free | 100 requests/day | $0 | Individuals |
| Pro | 10,000 requests/day | $29/month | Small Business |
| Enterprise | Unlimited | Custom | Large Orgs |

**Projected**: $1,580/month with 20 Pro + 2 Enterprise customers

---

## 📞 Support

- **API Docs**: http://localhost:8000/docs
- **Issues**: Check logs in terminal
- **Questions**: See troubleshooting section

---

## ✅ Success Checklist

- [ ] Extracted project
- [ ] Installed dependencies
- [ ] Copied .env.example to .env
- [ ] Started server
- [ ] Opened /docs
- [ ] Created test profile
- [ ] Tested batch CSV
- [ ] Verified profile + batch integration

---

**Version**: 4.0.0  
**License**: Proprietary  
**Status**: Production Ready ✅  

🎉 **You're all set! Start with the Quick Start above!**

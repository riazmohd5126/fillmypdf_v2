# ⚡ QUICK START - 5 Minutes to Running!

## 🎯 Step 1: Extract (30 seconds)

```bash
cd ~/Desktop
unzip FillMyPDF_OOP_Complete.zip
cd FillMyPDF_OOP
```

---

## 📦 Step 2: Install (2 minutes)

```bash
pip3 install -r requirements.txt

# macOS users only:
brew install poppler
```

---

## ⚙️ Step 3: Configure (1 minute)

```bash
# Copy environment file
cp .env.example .env

# IMPORTANT: Edit .env and change the encryption key!
nano .env  # or use: code .env
```

**Change this line:**
```
PROFILES_ENCRYPTION_KEY=change-this-to-a-strong-random-key-in-production
```

**To something like:**
```
PROFILES_ENCRYPTION_KEY=my-super-secret-key-abc123xyz789
```

---

## 🚀 Step 4: Start Server (30 seconds)

```bash
python3 -m fillmypdf.main
```

**Expected output:**
```
============================================================
🚀 FillMyPDF v4.0.0
============================================================
📁 Storage: .../fillmypdf/storage
🔐 Encryption: Enabled
👤 Profile limit (free): 1
📦 Batch processing: Enabled
📖 API Docs: http://localhost:8000/docs
============================================================
```

✅ **If you see this, server is running!**

---

## 🧪 Step 5: Test (1 minute)

### **Open Browser:**
```
http://localhost:8000/docs
```

### **Create Test Profile:**

1. Find: `POST /api/v1/profiles`
2. Click: "Try it out"
3. Paste this:
```json
{
  "name": "Test Company",
  "profile_type": "business",
  "data": {
    "company_name": "Acme Corp",
    "company_address": "123 Main St",
    "ein": "12-3456789"
  }
}
```
4. Click: "Execute"
5. **Save the profile ID** from response!

### **Test Batch CSV:**

1. Create test CSV:
```bash
cat > ~/Desktop/test.csv << 'EOF'
first_name,last_name,email
John,Doe,john@test.com
Jane,Smith,jane@test.com
EOF
```

2. In /docs, find: `POST /api/v1/batch/fill-csv`
3. Upload: PDF + CSV + API key + profile_id
4. Download: ZIP with 2 filled PDFs!

---

## ✅ Success!

If you got here, you have:
- ✅ OOP system running
- ✅ User profiles working
- ✅ Batch CSV working
- ✅ Profile + Batch integration working

---

## 🎯 What's Different from Old API?

| Feature | Old API | OOP System |
|---------|---------|------------|
| Batch CSV | ✅ Works | ✅ Works (better!) |
| **User Profiles** | ❌ No | ✅ **Save once, reuse!** |
| **Profile + Batch** | ❌ No | ✅ **Auto-merge!** |
| **Encryption** | ❌ No | ✅ **AES-256!** |
| CSV Size | Large (all data) | Small (profile + delta) |

**Example:**
- **Old**: CSV has company info in every row → 1000 lines
- **OOP**: Profile has company info, CSV only employee data → 50 lines!

---

## 🚀 Next: Add Authentication

See `AUTHENTICATION_GUIDE.md` to add:
- API keys
- Rate limiting
- Usage tracking
- Monetization!

**Takes 1 day to implement!**

---

## 📞 Need Help?

**Check:**
1. Server logs for errors
2. /docs API documentation
3. README.md for detailed docs

**Common issues:**
- Port 8000 in use: `lsof -ti:8000 | xargs kill -9`
- Missing modules: `pip3 install -r requirements.txt`
- Poppler not found: `brew install poppler` (macOS)

---

**You're ready! Start testing with Step 5 above!** 🎉

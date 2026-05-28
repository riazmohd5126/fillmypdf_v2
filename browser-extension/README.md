# FillMyPDF Browser Extension

A Chrome (Manifest V3) extension that auto-fills web forms and PDFs using your saved FillMyPDF profile data.

## Features

- **Profile picker** — select from your saved profiles via the popup
- **One-click form fill** — automatically maps profile data to detected form fields on any web page
- **PDF upload & fill** — upload a PDF through the popup, send to FillMyPDF API, and download the filled version
- **Template fill** — optionally select a template from your library to apply smart field mapping
- **Context menu** — right-click any page to fill or open the dashboard
- **Badge** — shows "PDF" badge on PDF pages
- **In-page toast** — confirms filled field count directly on the page

## Installation (Developer Mode)

1. Open Chrome and navigate to `chrome://extensions`
2. Enable **Developer mode** (toggle in top-right)
3. Click **Load unpacked**
4. Select the `browser-extension/` folder from this repo

## Configuration

On first launch the popup asks for your FillMyPDF API key:
- Get it from the dashboard at `http://localhost:8000/ui/keys.html`
- The extension stores it in Chrome's local storage

To point at a production server instead of localhost, change `DEFAULT_BASE` in `popup.js`:

```js
const DEFAULT_BASE = 'https://your-fillmypdf-server.com';
```

## How Field Matching Works

The extension uses a **keyword similarity score** to map profile fields to form inputs:

1. Collects `name`, `id`, `placeholder`, and `aria-label` from each form element
2. Flattens your profile JSON into dot-separated keys (e.g. `address_city`)
3. Strips separators and compares lowercased strings
4. Fills the field if similarity score ≥ 60 (exact or partial match)

## Permissions

| Permission | Why |
|---|---|
| `activeTab` | Read/write the current tab's DOM for form filling |
| `scripting` | Inject fill script into pages |
| `storage` | Store API key and preferences |
| `notifications` | Confirm successful fills |
| `host_permissions` | Call FillMyPDF API from the popup |

## File Structure

```
browser-extension/
├── manifest.json     — Extension metadata and permissions
├── popup.html        — Main popup UI
├── popup.js          — Popup logic (API calls, profile loading, fill)
├── background.js     — Service worker (badge, context menus, message bus)
├── content.js        — In-page script (toast notifications, PDF link hints)
└── icons/            — Extension icons (16, 32, 48, 128 px)
```

## Generating Icons

The `icons/` folder needs PNG icons. You can generate them from any 128×128 image:

```bash
# macOS — using sips
for size in 16 32 48 128; do
  sips -z $size $size your-icon.png --out icons/icon${size}.png
done
```

Or use any online icon generator with the FillMyPDF logo.

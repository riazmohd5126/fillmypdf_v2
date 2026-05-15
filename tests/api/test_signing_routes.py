"""API tests for POST /api/v1/signatures/apply"""

import io

from PIL import Image
from pypdf import PdfWriter

BASE = "/api/v1/signatures/apply"


def _plain(k):
    return k["plain"]


def _blank_pdf_bytes():
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _tiny_png():
    img = Image.new("RGBA", (32, 16), (200, 50, 50, 220))
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


class TestSigningApply:
    def test_requires_auth(self, client):
        pdf = _blank_pdf_bytes()
        resp = client.post(
            BASE,
            files={"file": ("p.pdf", pdf, "application/pdf")},
            data={"signature_text": "X"},
        )
        assert resp.status_code == 401

    def test_happy_path_typed_text(self, client, pro_api_key):
        pdf = _blank_pdf_bytes()
        resp = client.post(
            BASE,
            files={"file": ("p.pdf", pdf, "application/pdf")},
            data={"signature_text": "Pat Example", "page_index": "0"},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["filename"].startswith("signed_")
        assert body["filename"].endswith(".pdf")
        assert body["download_url"].endswith(body["filename"])

        dl = client.get(body["download_url"], headers={"X-API-Key": _plain(pro_api_key)})
        assert dl.status_code == 200
        assert "application/pdf" in (dl.headers.get("content-type") or "")
        assert len(dl.content) > len(pdf)

    def test_happy_path_png(self, client, pro_api_key):
        pdf = _blank_pdf_bytes()
        png = _tiny_png()
        resp = client.post(
            BASE,
            files={
                "file": ("p.pdf", pdf, "application/pdf"),
                "signature_png": ("s.png", png, "image/png"),
            },
            data={"page_index": "0", "x_pct": "10", "y_pct": "10", "width_pct": "30", "height_pct": "10"},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 200

    def test_both_image_and_text_returns_400(self, client, pro_api_key):
        pdf = _blank_pdf_bytes()
        png = _tiny_png()
        resp = client.post(
            BASE,
            files={
                "file": ("p.pdf", pdf, "application/pdf"),
                "signature_png": ("s.png", png, "image/png"),
            },
            data={"signature_text": "No"},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 400

    def test_neither_image_nor_text_returns_400(self, client, pro_api_key):
        pdf = _blank_pdf_bytes()
        resp = client.post(
            BASE,
            files={"file": ("p.pdf", pdf, "application/pdf")},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 400

    def test_non_pdf_returns_400(self, client, pro_api_key):
        resp = client.post(
            BASE,
            files={"file": ("x.txt", b"hi", "text/plain")},
            data={"signature_text": "A"},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 400

    def test_invalid_png_magic_returns_400(self, client, pro_api_key):
        pdf = _blank_pdf_bytes()
        resp = client.post(
            BASE,
            files={
                "file": ("p.pdf", pdf, "application/pdf"),
                "signature_png": ("s.png", b"not-a-png", "image/png"),
            },
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 400

"""
PDF Service
===========
Handles PDF conversion (static → fillable) and form field filling.
Uses commonforms for field detection on static PDFs, pypdf for
already-fillable PDFs.
"""

from pathlib import Path
from typing import Dict

from pypdf import PdfReader, PdfWriter


class PDFService:
    """Service for PDF operations"""

    def convert_to_fillable(self, input_path: Path, output_path: Path) -> bool:
        """
        Convert a PDF to a fillable form and write to output_path.

        Strategy:
        1. If the PDF already has AcroForm fields, copy it as-is.
        2. Otherwise attempt commonforms field detection/conversion.
        3. If commonforms fails, fall back to a direct copy so the
           pipeline can still proceed (fields_filled will just be 0).
        """
        try:
            reader = PdfReader(str(input_path))

            if reader.get_fields():
                # Already a fillable form — just copy it
                writer = PdfWriter()
                writer.append(reader)
                with open(output_path, "wb") as fh:
                    writer.write(fh)
                print(f"  📄 PDF already fillable ({len(reader.get_fields())} fields)")
                return True

            # Try commonforms conversion
            try:
                from commonforms import prepare_form  # type: ignore
                prepare_form(
                    str(input_path),
                    str(output_path),
                    model_or_path="FFDNet-L",
                    confidence=0.2,
                    use_signature_fields=True,
                    image_size=1600,
                )
                converted_reader = PdfReader(str(output_path))
                field_count = len(converted_reader.get_fields() or {})
                print(f"  🔄 PDF converted to fillable via commonforms ({field_count} fields)")
                return True
            except ImportError:
                print("  ⚠️  commonforms not available, copying PDF as-is")
            except Exception as cf_err:
                print(f"  ⚠️  commonforms conversion failed ({cf_err}), copying as-is")

            # Fallback: plain copy
            writer = PdfWriter()
            writer.append(reader)
            with open(output_path, "wb") as fh:
                writer.write(fh)
            return True

        except Exception as e:
            print(f"  ❌ Error converting PDF: {e}")
            return False

    def get_form_fields(self, pdf_path: Path) -> Dict[str, str]:
        """
        Return all AcroForm field names and their current values.
        Returns an empty dict if the PDF has no fields or cannot be read.
        """
        try:
            reader = PdfReader(str(pdf_path))
            raw = reader.get_fields()
            if not raw:
                return {}
            result = {}
            for name, field in raw.items():
                val = field.value
                result[name] = val if isinstance(val, str) else (str(val) if val is not None else "")
            return result
        except Exception as e:
            print(f"  ⚠️  Could not read form fields: {e}")
            return {}

    def fill_fields(
        self, input_path: Path, output_path: Path, field_values: Dict[str, str]
    ) -> bool:
        """
        Write field_values into the PDF at input_path and save to output_path.
        Returns True on success.
        """
        try:
            reader = PdfReader(str(input_path))
            writer = PdfWriter()
            writer.append(reader)

            for page in writer.pages:
                try:
                    writer.update_page_form_field_values(page, field_values)
                except Exception:
                    pass  # page may have no fields — skip quietly

            with open(output_path, "wb") as fh:
                writer.write(fh)
            return True

        except Exception as e:
            print(f"  ❌ Error filling PDF fields: {e}")
            return False

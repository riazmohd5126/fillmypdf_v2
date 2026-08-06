"""
PDF Service
===========
Handles PDF conversion (static → fillable) and form field filling.
Uses commonforms for field detection on static PDFs, pypdf for
already-fillable PDFs.
"""

from pathlib import Path
from typing import Any, Dict, Union

from pypdf import PdfReader, PdfWriter

PathLike = Union[str, Path]


class PDFService:
    """Service for PDF operations"""

    def convert_to_fillable(self, input_path: PathLike, output_path: PathLike) -> bool:
        """
        Convert a PDF to a fillable form and write to output_path.

        Strategy:
        1. If the PDF already has AcroForm fields, copy it as-is.
        2. Otherwise attempt commonforms field detection/conversion.
        3. If commonforms fails, fall back to a direct copy so the
           pipeline can still proceed (fields_filled will just be 0).
        """
        return bool(self.convert_to_fillable_detailed(input_path, output_path).get("ok"))

    def convert_to_fillable_detailed(
        self, input_path: PathLike, output_path: PathLike
    ) -> Dict[str, Any]:
        """
        Same as :meth:`convert_to_fillable` but returns a status report:

        ``status`` — ``already_fillable`` | ``converted`` | ``copied_as_is`` | ``error``
        ``engine`` — ``none`` | ``commonforms`` | ``cloud``
        """
        input_path = Path(input_path)
        output_path = Path(output_path)
        try:
            reader = PdfReader(str(input_path))
            page_count = len(reader.pages)
            before_fields = reader.get_fields() or {}
            field_count_before = len(before_fields)

            if before_fields:
                writer = PdfWriter()
                writer.append(reader)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as fh:
                    writer.write(fh)
                print(f"  📄 PDF already fillable ({field_count_before} fields)")
                return {
                    "ok": True,
                    "status": "already_fillable",
                    "engine": "none",
                    "field_count_before": field_count_before,
                    "field_count_after": field_count_before,
                    "page_count": page_count,
                    "message": f"PDF already fillable ({field_count_before} AcroForm fields).",
                }

            from ..config import settings
            mode = (getattr(settings, "COMMONFORMS_MODE", "local") or "local").lower()
            engine_tried = "cloud" if mode == "cloud" else "commonforms"
            converted = False

            if mode == "cloud":
                converted = self._convert_via_cloud(input_path, output_path)
                if not converted:
                    print("  ⚠️  cloud converter unavailable, copying PDF as-is")
            else:
                converted = self._convert_via_commonforms(input_path, output_path)

            if converted and output_path.exists():
                after = PdfReader(str(output_path))
                field_count_after = len(after.get_fields() or {})
                return {
                    "ok": True,
                    "status": "converted",
                    "engine": engine_tried,
                    "field_count_before": field_count_before,
                    "field_count_after": field_count_after,
                    "page_count": page_count,
                    "message": (
                        f"Converted via {engine_tried}: "
                        f"{field_count_after} fillable field(s) detected."
                    ),
                }

            # Fallback: plain copy so the pipeline can still proceed (0 fields).
            writer = PdfWriter()
            writer.append(reader)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as fh:
                writer.write(fh)
            return {
                "ok": True,
                "status": "copied_as_is",
                "engine": engine_tried,
                "field_count_before": 0,
                "field_count_after": 0,
                "page_count": page_count,
                "message": (
                    f"Conversion via {engine_tried} failed or found no fields; "
                    "returned original PDF unchanged."
                ),
            }

        except Exception as e:
            print(f"  ❌ Error converting PDF: {e}")
            return {
                "ok": False,
                "status": "error",
                "engine": "none",
                "field_count_before": 0,
                "field_count_after": 0,
                "page_count": 0,
                "message": str(e),
            }

    # ------------------------------------------------------------------
    # Flat -> fillable backends
    # ------------------------------------------------------------------

    def _convert_via_commonforms(self, input_path: Path, output_path: Path) -> bool:
        """Local commonforms conversion, honoring the configured model/size.

        Defaults to the small model (FFDNet-S) + fast ONNX path + a modest image
        size so it stays within a few hundred MB. Still heavier than acroform —
        thin clients should prefer COMMONFORMS_MODE=cloud.
        """
        from ..config import settings
        try:
            from commonforms import prepare_form  # type: ignore
            prepare_form(
                str(input_path),
                str(output_path),
                model_or_path=getattr(settings, "COMMONFORMS_MODEL", "FFDNet-S"),
                confidence=getattr(settings, "COMMONFORMS_CONFIDENCE", 0.1),
                use_signature_fields=True,
                image_size=getattr(settings, "COMMONFORMS_IMAGE_SIZE", 1024),
                fast=getattr(settings, "COMMONFORMS_FAST", True),
            )
            converted = PdfReader(str(output_path))
            field_count = len(converted.get_fields() or {})
            print(f"  🔄 PDF converted to fillable via commonforms ({field_count} fields)")
            return True
        except ImportError:
            print("  ⚠️  commonforms not available, copying PDF as-is")
        except Exception as cf_err:
            print(f"  ⚠️  commonforms conversion failed ({cf_err}), copying as-is")
        return False

    def _convert_via_cloud(self, input_path: Path, output_path: Path) -> bool:
        """Offload flat->fillable to the remote converter service (no torch here).

        Sends ONLY the blank form (no patient values) to the converter and writes
        back the returned fillable PDF. Returns False on any failure so the caller
        can fall back to a plain copy.
        """
        from ..config import settings
        url = (getattr(settings, "CONVERT_SERVICE_URL", "") or "").strip()
        if not url:
            print("  ⚠️  COMMONFORMS_MODE=cloud but CONVERT_SERVICE_URL is unset")
            return False
        try:
            import httpx

            headers = {}
            key = (getattr(settings, "CONVERT_SERVICE_KEY", "") or "").strip()
            if key:
                headers["X-Convert-Key"] = key
            timeout = float(getattr(settings, "CONVERT_SERVICE_TIMEOUT", 120.0))

            with open(input_path, "rb") as fh:
                files = {"file": (Path(input_path).name, fh, "application/pdf")}
                resp = httpx.post(url, files=files, headers=headers, timeout=timeout)

            if resp.status_code != 200:
                print(f"  ⚠️  cloud converter HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            ctype = resp.headers.get("content-type", "")
            if "application/pdf" not in ctype and not resp.content[:5] == b"%PDF-":
                print(f"  ⚠️  cloud converter returned non-PDF ({ctype})")
                return False

            with open(output_path, "wb") as out:
                out.write(resp.content)
            field_count = len(PdfReader(str(output_path)).get_fields() or {})
            print(f"  ☁️  PDF converted via cloud converter ({field_count} fields)")
            return True
        except Exception as exc:
            print(f"  ⚠️  cloud converter call failed ({exc})")
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
            def _is_pushbutton_dict(d) -> bool:
                """True if a field/widget dict is a pushbutton: /Btn type with
                the pushbutton flag (/Ff bit 17, 0x10000) set."""
                if str(d.get("/FT", "")) != "/Btn":
                    return False
                try:
                    return bool(int(d.get("/Ff", 0) or 0) & 0x10000)
                except (TypeError, ValueError):
                    return False

            result = {}
            for name, field in raw.items():
                # Skip PUSHBUTTONS — action controls (page navigation,
                # Print/Reset/Submit triggers), not data-entry fields: they
                # hold no value and would otherwise surface as bogus all-null
                # rows (e.g. "Button 1013.Page 20"). Real checkboxes/radios
                # are /Btn WITHOUT the pushbutton flag, so they stay.
                #  a) terminal pushbutton widget/field
                if _is_pushbutton_dict(field):
                    continue
                #  b) a non-terminal container node (no /FT of its own) whose
                #     /Kids are ALL pushbuttons — these appear as an extra
                #     value-less parent row ("Button 102") above the leaf
                #     "Button 102.Page 7" widgets.
                if not field.get("/FT"):
                    kids = field.get("/Kids")
                    if kids:
                        try:
                            kid_objs = [k.get_object() for k in kids]
                            if kid_objs and all(_is_pushbutton_dict(k) for k in kid_objs):
                                continue
                        except Exception:
                            pass
                #  c) a non-terminal GROUPING node whose /Kids are themselves
                #     named sub-fields (each kid carries its own /T). pypdf lists
                #     BOTH this parent ("Member Info T") and every leaf ("Member
                #     Info T.0", ".1", …). The parent holds no value of its own,
                #     so it would surface as a stray all-null row. Skip it; its
                #     leaves are reported individually. (A terminal text field
                #     whose kids are pure appearance widgets has NO /T on those
                #     kids — that parent is kept.)
                kids = field.get("/Kids")
                if kids:
                    try:
                        kid_objs = [k.get_object() for k in kids]
                        if kid_objs and all(k.get("/T") is not None for k in kid_objs):
                            continue
                    except Exception:
                        pass
                val = field.value
                result[name] = val if isinstance(val, str) else (str(val) if val is not None else "")
            return result
        except Exception as e:
            print(f"  ⚠️  Could not read form fields: {e}")
            return {}

    @staticmethod
    def _collect_button_states(reader: PdfReader) -> Dict[str, list]:
        """Map every /Btn field name → its list of ON-state export values
        (e.g. ["/Yes"] or ["/Male", "/Female"]).

        Walks the AcroForm field tree so radio groups whose states sit on
        their kid widgets' /AP /N (not on the parent) are captured. Each field
        is indexed by BOTH its fully-qualified dotted name and its leaf name.
        """
        states_map: Dict[str, list] = {}

        def _widget_states(obj) -> list:
            out: list = []
            try:
                ap = obj.get("/AP")
                if ap:
                    n = ap.get_object().get("/N")
                    if n:
                        for k in n.get_object().keys():
                            if str(k) != "/Off" and str(k) not in out:
                                out.append(str(k))
            except Exception:
                pass
            return out

        def _walk(node_ref, prefix: str) -> None:
            try:
                node = node_ref.get_object()
            except Exception:
                return
            t = node.get("/T")
            if t is not None:
                name = f"{prefix}.{t}" if prefix else str(t)
            else:
                name = prefix
            kids = node.get("/Kids")
            if str(node.get("/FT", "")) == "/Btn" and name:
                states = list(_widget_states(node))
                if kids:
                    for kid in kids:
                        try:
                            ko = kid.get_object()
                            if ko.get("/T") is None:  # pure appearance widget
                                for s in _widget_states(ko):
                                    if s not in states:
                                        states.append(s)
                        except Exception:
                            continue
                states_map[name] = states
                states_map.setdefault(name.split(".")[-1], states)
            # Recurse into named sub-fields (kids carrying their own /T).
            if kids:
                for kid in kids:
                    try:
                        if kid.get_object().get("/T") is not None:
                            _walk(kid, name)
                    except Exception:
                        continue

        try:
            acro = reader.trailer["/Root"].get("/AcroForm")
            fields = acro.get_object().get("/Fields") if acro else None
            if fields:
                for f in fields:
                    _walk(f, "")
        except Exception:
            pass
        return states_map

    _TRUE_STATES = {"yes", "true", "1", "on", "x", "checked", "selected"}

    @staticmethod
    def _resolve_button_state(value: str, states: list) -> str:
        """Resolve a user/AI value to a real button on-state name (e.g. "/Male",
        "/Yes", "/Off").

        - An explicit option ("Male", "No_2") matches its export state
          case-insensitively — this is what makes MULTI-option radios fillable.
        - A generic truthy value ("Yes"/"true"/"1"/"x") checks the box when the
          field has exactly one on-state, or picks a "Yes"-named state.
        - Anything falsey / unmatched → "/Off".
        """
        v = str(value).strip()
        vlow = v.lstrip("/").lower()
        non_off = [s for s in states if s != "/Off"]
        for s in states:                                  # exact export match
            if s.lstrip("/").lower() == vlow:
                return s
        if vlow in PDFService._TRUE_STATES:               # generic truthy
            if len(non_off) == 1:
                return non_off[0]
            for s in non_off:
                if s.lstrip("/").lower() == "yes":
                    return s
        return "/Off"

    def fill_fields(
        self, input_path: Path, output_path: Path, field_values: Dict[str, str]
    ) -> bool:
        """
        Write field_values into the PDF at input_path and save to output_path.
        Returns True on success.
        """
        try:
            reader = PdfReader(str(input_path))

            # Build a lookup: field name → its valid button on-states (export
            # values), e.g. ["/Yes"] for a plain checkbox or ["/Male",
            # "/Female"] for a Gender radio. pypdf's "/_States_" is unreliable
            # for radio groups (the states live on the KID widgets' /AP /N, not
            # the parent), so read them straight from the AcroForm tree.
            # Indexed by BOTH fully-qualified and leaf name so flat-name callers
            # still resolve.
            btn_states = self._collect_button_states(reader)

            # Normalise values: checkboxes/radios need a PDF name state
            # (/Male, /Yes, /Off); text fields pass through unchanged.
            normalised: Dict[str, str] = {}
            for k, v in field_values.items():
                if k in btn_states:
                    normalised[k] = self._resolve_button_state(v, btn_states[k])
                else:
                    normalised[k] = v

            writer = PdfWriter()
            writer.append(reader)

            for page in writer.pages:
                try:
                    writer.update_page_form_field_values(page, normalised)
                except Exception:
                    pass  # page may have no fields — skip quietly

            with open(output_path, "wb") as fh:
                writer.write(fh)
            return True

        except Exception as e:
            print(f"  ❌ Error filling PDF fields: {e}")
            return False

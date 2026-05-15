# FillMyPDF docs

| File | Purpose |
|------|---------|
| [FEATURE_MATRIX_ROUTE.md](./FEATURE_MATRIX_ROUTE.md) | How Cowork/feature-matrix phases map onto this backend |
| [feature-matrix.json](./feature-matrix.json) | Machine-readable feature rows (single source after snapshot script) |
| [FeatureMatrix.jsx](./FeatureMatrix.jsx) | Tailwind/React viewer (`import matrix from ./feature-matrix.json`) |

Refresh JSON after editing `scripts/feature_matrix_snapshot.py`:

```bash
python3 scripts/feature_matrix_snapshot.py
```

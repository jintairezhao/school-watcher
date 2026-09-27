# Self-hosted UI fonts

These assets are part of the application distribution. Browser rendering never
contacts Google Fonts or an external font service. Both families use SIL OFL 1.1;
the original licenses are included beside their files.

| Family | Internal font version | Assets | Cmap coverage |
| --- | --- | --- | --- |
| Inter | 4.001; git-66647c0bb | 7 WOFF2 shards | 1,622 codepoints |
| Noto Sans SC | 2.004-H2 | 101 Google Fonts shards + 34 supplements | 30,890 codepoints |

Inter supplies Latin text and numerals, followed by Noto Sans SC for Chinese.
The original Google Fonts v40 CSS supplied only 13,635 of the selected upstream
Noto Sans SC font's 30,890 cmap entries. We preserve those efficient common-text
shards and add 34 deterministic shards containing the 17,255 missing codepoints,
with at most 512 codepoints per supplement. The bundled union was compared for
exact equality with the pinned upstream cmap, not merely with sample page text.
All 142 WOFF2 files total 10,453,428 bytes; browsers load only needed Unicode ranges.
Do not preload all Chinese shards.

The upstream TTF came from the official Google Fonts repository. The raw GitHub
copy and the jsDelivr repository mirror have identical SHA-256 hashes. The 17 MB
source TTF is deliberately not shipped under static assets: rebuilding downloads
it to a temporary directory, validates its hash, and discards it afterwards.

The selected upstream supports 龘, 鱻, 喆 and 𬌗. It does not contain 𠮷 (U+20BB7).
Such characters retain the platform fallback; the app does not claim to supply
all Unicode glyphs. CSS exposes the UI's variable 400–700 weight range.

`manifest.json` records source URLs, exact SHA-256 hashes, byte lengths, original
stylesheet headers, internal versions, weight axes, source cmap coverage, and
supplement build-tool versions. `upstream-css.txt` is provenance only and is never
loaded as a stylesheet. `../css/fonts.css` uses local asset paths; typography roles
live in `../css/typography.css`.

Run from the project root:

```text
python scripts/vendor_fonts.py --verify
python scripts/vendor_fonts.py --restore
```

Verification is offline. Restore first checks existing assets, downloads only
missing pinned upstream assets, and rebuilds missing supplementary files using
exact source bytes and the locked build tools. Generated files must match the
recorded hashes before being written. To rebuild supplements, install the tooling:

```text
python -m pip install fonttools==4.66.0 brotli==1.2.0
```

These tools are not application runtime dependencies. Updating fonts is an
explicit maintainer action: audit the licenses and upstream cmap, update the
lock, and repeat hash, complete-cmap, reproducibility and rendered-font checks.

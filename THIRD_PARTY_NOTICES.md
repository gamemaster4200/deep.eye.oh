# Third-party notices

## Cazka/diepAPI

- Project: [Cazka/diepAPI](https://github.com/Cazka/diepAPI)
- Pinned release and asset digest: `extension/vendor/diepAPI.lock.json`
- Vendored build artifact: `extension/vendor/diepAPI.user.js`
- License: MIT
- Preserved license text: `third_party/diepAPI-LICENSE.txt`

The vendored userscript is an upstream build artifact. It is intentionally excluded from local formatting/whitespace normalization and must be updated only through the explicit `scripts/update-vendor.ps1` workflow.

**Not an active runtime dependency.** `extension/manifest.json` does not load `diepAPI.user.js`. It is kept in the repository only as researched, pinned provenance: live smoke testing found that this pinned build (v3.3.1) does not export an `entityManager` through its public `diepAPI.apis`, and that running it against the official client leads to `Uncaught RuntimeError: memory access out of bounds` in `diep.wasm` (through the vendor's `requestAnimationFrame` hook) — consistent with the open upstream issue [Cazka/diepAPI#80](https://github.com/Cazka/diepAPI/issues/80). Browser Oracle's neutral-square ground truth is instead produced by its own small Canvas2D observer in `extension/src/oracle.js`. Every pin change to the preserved vendor file still requires review of the provenance lock and license text before it is used for anything.

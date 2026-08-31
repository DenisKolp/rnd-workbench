# Third-party distribution gate

This directory contains license texts that must accompany any distributable
build. `THIRD_PARTY_NOTICES.md` is the component manifest and source of truth.

The repository currently publishes source code and a local build recipe. A
macOS or Windows binary is **not cleared for corporate distribution** until all
bundled Python/native packages and model weights have a complete SBOM, their
license texts are copied here, and the OmniVoice CC-BY-NC restriction is
resolved by written permission or a commercially permitted replacement.

The included `omnivoice.cpp-MIT.txt` covers the upstream runtime and the
repository patch in `patches/omnivoice/desktop-realtime-and-cancel.patch`.

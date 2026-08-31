# Third-party notices

The repository's MIT license applies only to original RnD Workbench source and
documentation for which the project owns the necessary rights. It does not
relicense third-party code, model weights, voice samples, fonts, screenshots,
or other assets. Those materials remain governed by their upstream licenses or
individual terms; the known model/runtime restrictions are listed below.

## OmniVoice model

- Project: `k2-fsa/OmniVoice`
- Source: https://github.com/k2-fsa/OmniVoice
- Model: https://huggingface.co/k2-fsa/OmniVoice
- Code license: Apache License 2.0
- Pre-trained weights license: CC-BY-NC

The official model card states that the non-commercial restriction is caused by
constraints in the training data. These weights must not be used for commercial
or corporate deployment without a separate rights review. Voice cloning must only
be performed with the speaker's permission.

## OmniVoice GGUF runtime

- Project: `ServeurpersoCom/omnivoice.cpp`
- Source: https://github.com/ServeurpersoCom/omnivoice.cpp
- Integrated revision: `4f33af825d66e6ef1cb185e87b4589cacf747291`
- License: MIT
- GGUF weights: https://huggingface.co/Serveurperso/OmniVoice-GGUF

The GGUF files are quantized representations of the OmniVoice pre-trained model
and remain subject to the upstream model's CC-BY-NC restriction.

The repository patch in
`patches/omnivoice/desktop-realtime-and-cancel.patch` is derived from this MIT
runtime. Its required upstream copyright and full MIT text are included in
`third_party/licenses/omnivoice.cpp-MIT.txt`.

## Distribution status

The checked-in source is shareable subject to the notices above, but the local
`.app`/Windows build recipe can bundle additional Python/native packages and
Qwen, Whisper and OmniVoice model weights. No binary is represented as cleared
for corporate redistribution. A release owner must generate a complete SBOM,
include every applicable license text, and resolve the OmniVoice CC-BY-NC
restriction before distributing a pilot installer.

## Desktop Java policy companion

The macOS application bundle and unsigned Windows QA artifact contain a `jlink`
image produced from OpenJDK/Temurin 21 and the pinned Java dependencies declared
in `core-java/build.gradle.kts`: Jackson 2.18.2 (Apache-2.0), Xerial SQLite JDBC
3.47.2.0 (Apache-2.0) and SLF4J 2.0.16 (MIT). The generated runtime keeps the
OpenJDK per-module `legal/` directory, and the dependency JARs retain their
embedded `META-INF` notices where supplied. Their inclusion does not change the
distribution gate above or clear either artifact for corporate rollout.

## Project Context Router

- Project: `ukolov-dev/mcp-project-context-router`
- Source: https://github.com/ukolov-dev/mcp-project-context-router
- Integrated revision: `69d41262ace8157a2353f138a703bba507488dbe`
- License: no open-source license granted; package metadata is `UNLICENSED`
- Scope in RnD Workbench: development-only tooling, not bundled into the user
  application or its distributable runtime

The public source may be inspected at its upstream repository, but no right to
copy, modify, use or redistribute it is granted by an open-source license.
RnD Workbench therefore does not publish the package, its lockfile, generated
templates or internal context records. The local MCP configuration is inert
until a developer independently obtains permission and installs the package.

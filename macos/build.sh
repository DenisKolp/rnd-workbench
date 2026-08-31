#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
project_dir="${script_dir:h}"
output_dir="${project_dir:h}"
legacy_app="$output_dir/Local Voice Assistant.app"
app_dir="$output_dir/RnD Workbench.app"
resources_dir="$app_dir/Contents/Resources"
runtime_dir="$resources_dir/runtime"
module_cache="$project_dir/.build/swift-module-cache"
sdk_path="$(xcrun --sdk macosx --show-sdk-path)"
sdk_interface="$sdk_path/usr/lib/swift/Swift.swiftmodule/arm64e-apple-macos.swiftinterface"
sdk_compiler_version="$(sed -n '/^\/\/ swift-compiler-version: /{s|// swift-compiler-version: ||;p;q;}' "$sdk_interface")"

if rg -q '^backend = "omnivoice_fast"' "$project_dir/config.toml"; then
  zsh "$script_dir/build-omnivoice.sh"
fi

if [[ -d "$legacy_app" && ! -e "$app_dir" ]]; then
  mv "$legacy_app" "$app_dir"
fi

mkdir -p "$app_dir/Contents/MacOS" "$resources_dir" "$runtime_dir" "$module_cache"

swiftc \
  -module-cache-path "$module_cache" \
  -interface-compiler-version "$sdk_compiler_version" \
  -parse-as-library \
  -swift-version 5 \
  -O \
  -framework SwiftUI \
  -framework AppKit \
  -framework ApplicationServices \
  -framework CoreGraphics \
  -framework Foundation \
  -framework Security \
  "$script_dir/VoiceAssistantApp.swift" \
  -o "$app_dir/Contents/MacOS/LocalVoiceAssistant"

cp "$script_dir/Info.plist" "$app_dir/Contents/Info.plist"
cp "$project_dir/config.toml" "$resources_dir/config.toml"
cp "$project_dir/THIRD_PARTY_NOTICES.md" "$resources_dir/THIRD_PARTY_NOTICES.md"
if [[ -d "$project_dir/third_party" ]]; then
  rm -rf -- "$resources_dir/third_party"
  cp -R "$project_dir/third_party" "$resources_dir/third_party"
fi

# Bundle a relocatable local runtime. APFS clone copies keep the downloaded
# model weights self-contained without consuming another physical 5.5 GB.
python_binary="${project_dir}/.venv/bin/python"
python_root="${${python_binary}:A:h:h}"
if [[ ! -x "$runtime_dir/python/bin/python3.12" ]]; then
  mkdir -p "$runtime_dir/python"
  cp -cR "$python_root/." "$runtime_dir/python/"
fi
if [[ ! -d "$runtime_dir/site-packages/mlx" ]]; then
  mkdir -p "$runtime_dir/site-packages"
  cp -cR "$project_dir/.venv/lib/python3.12/site-packages/." "$runtime_dir/site-packages/"
fi
mkdir -p "$runtime_dir/src"
cp -R "$project_dir/src/." "$runtime_dir/src/"
if [[ -d "$project_dir/runtime/omnivoice" ]]; then
  mkdir -p "$runtime_dir/omnivoice"
  cp -cR "$project_dir/runtime/omnivoice/." "$runtime_dir/omnivoice/"
fi
mkdir -p "$resources_dir/models"
for model_name in qwen3-4b whisper-large-v3-turbo; do
  if [[ ! -d "$resources_dir/models/$model_name" ]]; then
    cp -cR "$project_dir/models/$model_name" "$resources_dir/models/"
  fi
done
# Qwen3-TTS was used by older builds. OmniVoice replaces it, so do not leave
# 1.8 GB of stale weights in the distributable application bundle.
if rg -q '^backend = "omnivoice_fast"' "$project_dir/config.toml"; then
  rm -rf -- "$resources_dir/models/qwen3-tts"
fi
if [[ -f "$project_dir/models/omnivoice-fast/omnivoice-base-Q8_0.gguf" && \
      -f "$project_dir/models/omnivoice-fast/omnivoice-tokenizer-Q8_0.gguf" ]]; then
  mkdir -p "$resources_dir/models/omnivoice-fast"
  cp -c "$project_dir/models/omnivoice-fast/omnivoice-base-Q8_0.gguf" \
    "$resources_dir/models/omnivoice-fast/"
  cp -c "$project_dir/models/omnivoice-fast/omnivoice-tokenizer-Q8_0.gguf" \
    "$resources_dir/models/omnivoice-fast/"
fi

codesign --force --deep --sign - "$app_dir"

print "$app_dir"

#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
project_dir="${script_dir:h}"
source_dir="$project_dir/vendor/omnivoice.cpp"
build_dir="$source_dir/build-metal"
runtime_dir="$project_dir/runtime/omnivoice"
patch_file="$project_dir/patches/omnivoice/desktop-realtime-and-cancel.patch"
expected_vendor_revision="4f33af825d66e6ef1cb185e87b4589cacf747291"

if [[ ! -d "$source_dir/ggml" ]]; then
  print -u2 "Не найден vendor/omnivoice.cpp с submodules"
  exit 1
fi

actual_vendor_revision="$(git -C "$source_dir" rev-parse HEAD)"
if [[ "$actual_vendor_revision" != "$expected_vendor_revision" ]]; then
  print -u2 "Непроверенная ревизия OmniVoice: $actual_vendor_revision"
  print -u2 "Ожидалась: $expected_vendor_revision"
  exit 1
fi

if [[ ! -f "$patch_file" ]]; then
  print -u2 "Не найден обязательный patch OmniVoice: $patch_file"
  exit 1
fi
if git -C "$source_dir" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
  : # Patch already applied to this vendor checkout.
elif git -C "$source_dir" apply --check "$patch_file"; then
  git -C "$source_dir" apply "$patch_file"
else
  print -u2 "Vendor OmniVoice не соответствует проверенной ревизии patch"
  exit 1
fi

cmake \
  -S "$source_dir" \
  -B "$build_dir" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_METAL=ON \
  -DGGML_METAL_EMBED_LIBRARY=ON \
  -DGGML_ACCELERATE=ON \
  -DGGML_NATIVE=ON
cmake --build "$build_dir" --target tts-server --config Release -j 8

mkdir -p "$runtime_dir"
cp -f "$build_dir/tts-server" "$runtime_dir/tts-server"
for library in \
  libggml.0.dylib \
  libggml-base.0.dylib \
  libggml-blas.0.dylib \
  libggml-cpu.0.dylib \
  libggml-metal.0.dylib; do
  cp -fL "$build_dir/$library" "$runtime_dir/$library"
done

install_name_tool -delete_rpath "$build_dir" "$runtime_dir/tts-server"
install_name_tool -add_rpath @loader_path "$runtime_dir/tts-server"

for item in "$runtime_dir"/*; do
  codesign --force --sign - "$item"
done

print "$runtime_dir/tts-server"

#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
project_dir="${script_dir:h}"
source_dir="$project_dir/vendor/omnivoice.cpp"
build_dir="$source_dir/build-metal"
runtime_dir="$project_dir/runtime/omnivoice"

if [[ ! -d "$source_dir/ggml" ]]; then
  print -u2 "Не найден vendor/omnivoice.cpp с submodules"
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

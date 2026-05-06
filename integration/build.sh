#!/usr/bin/env bash
# Rebuild the Unfolded Circle integration tarball from source.
# Optional — pre-built ps5-uc-integration.tar.gz is included next to this
# script. Only run this if you want to verify the build or modify the source.

set -euo pipefail

cd "$(dirname "$0")/source"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker required for the build. Install: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

echo "Cleaning previous build output..."
rm -rf build_out pkg_root
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "Building linux/arm64 binary in Docker..."
docker buildx build --platform linux/arm64 -f Dockerfile.build \
  --target dist --output type=local,dest=./build_out .

echo "Packing tarball..."
mkdir -p pkg_root/bin
cp -r build_out/driver/. pkg_root/bin/
cp driver.json pkg_root/driver.json
( cd pkg_root && tar -czf ../../ps5-uc-integration.tar.gz ./bin ./driver.json )

OUT="../ps5-uc-integration.tar.gz"
echo
echo "✓ Built: $(cd .. && pwd)/ps5-uc-integration.tar.gz"
echo "  Size:  $(du -h "$OUT" | awk '{print $1}')"

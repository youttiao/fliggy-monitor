#!/usr/bin/env bash
# 打包 Chrome 扩展为 zip，供运营同事 sideload 用。
#
# 用法：
#   ./build.sh           # → dist/fliggy-cookie-sync.zip
#   VERSION=1.0.1 ./build.sh   # 自定义版本号
#
# 输出会附带 build-info.txt（打包时间 + commit），方便运维核对。

set -euo pipefail

cd "$(dirname "$0")"
DIST="dist"
mkdir -p "$DIST"

# 读取 manifest version，若没指定 VERSION 环境变量则用 manifest 里的
MANIFEST_VERSION=$(python3 -c 'import json,sys; print(json.load(open("manifest.json"))["version"])')
VERSION="${VERSION:-$MANIFEST_VERSION}"

STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
COMMIT=$(git -C ../.. rev-parse --short HEAD 2>/dev/null || echo "no-commit")

OUT="$DIST/fliggy-cookie-sync-${VERSION}.zip"
rm -f "$DIST"/fliggy-cookie-sync*.zip

# 临时拷贝要打包的文件到一个干净目录，避免把 README 之外的东西塞进去
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cp manifest.json popup.html popup.css popup.js README.md "$TMP/"
cp -r icons "$TMP/"

# 写一个 build-info.txt 进 zip
cat > "$TMP/build-info.txt" << EOF
version: ${VERSION}
built_at: ${STAMP}
commit: ${COMMIT}
EOF

( cd "$TMP" && zip -r "$OLDPWD/$OUT" . --quiet )
echo "✓ built $OUT"

# 同时写一个 latest 软链，方便 CI / 文档固定引用
ln -sf "$(basename "$OUT")" "$DIST/fliggy-cookie-sync.zip"
echo "✓ symlink $DIST/fliggy-cookie-sync.zip → $(basename "$OUT")"
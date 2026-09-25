#!/usr/bin/env bash
#
# 构建 memo-role 安卓客户端（原生界面 + 内置 Linux 容器）的 APK。
#
# 为什么不用 Gradle
# ----------------
# 这个 App 只用 Android 框架自带类（没有 AndroidX、没有第三方依赖），
# 走 Gradle + AGP 反而要额外下载几百 MB 依赖，并受 AGP 与 JDK 版本匹配的约束。
# 直接用 SDK 里的 aapt2 / d8 / zipalign / apksigner 串起来，步骤少、失败点少：
#
#   aapt2 compile → aapt2 link → javac → d8 → 塞入 classes.dex → zipalign → apksigner
#
# 用法：
#   ./build.sh                    # 用 $ANDROID_SDK_ROOT 或 /opt/android-sdk
#   SDK=/path/to/sdk ./build.sh
#
set -euo pipefail

cd "$(dirname "$0")"

SDK="${SDK:-${ANDROID_SDK_ROOT:-${ANDROID_HOME:-/opt/android-sdk}}}"
BUILD_TOOLS="${BUILD_TOOLS:-34.0.0}"
PLATFORM="${PLATFORM:-android-34}"
MIN_SDK="${MIN_SDK:-24}"
# targetSdk 必须停在 28：Android 10（API 29）起禁止「从应用可写主目录执行文件」
# （W^X）。容器里的 bash / python3 都位于应用数据目录，一旦 targetSdk ≥ 29 就会
# 全部无法 exec。Termux 与 UserLAnd 至今保持 28 也是这个原因。
#
# 代价：无法上架 Google Play（那里要求更高的 targetSdk），只能侧载安装；
# 但 Android 只拒绝 targetSdk < 23 的应用，28 在 Android 14/15 上可正常安装。
TARGET_SDK="${TARGET_SDK:-28}"
VERSION_CODE="${VERSION_CODE:-1}"
VERSION_NAME="${VERSION_NAME:-0.1.0}"
JAVA_RELEASE="${JAVA_RELEASE:-11}"

BT="$SDK/build-tools/$BUILD_TOOLS"
AJAR="$SDK/platforms/$PLATFORM/android.jar"
BUILD="build"
DIST="dist"
KEYSTORE="debug.keystore"
APK_NAME="memo-role-$VERSION_NAME.apk"

for tool in aapt2 d8 zipalign apksigner; do
  if [ ! -x "$BT/$tool" ]; then
    echo "缺少构建工具：$BT/$tool" >&2
    echo "请先安装：sdkmanager \"build-tools;$BUILD_TOOLS\" \"platforms;$PLATFORM\"" >&2
    exit 1
  fi
done
[ -f "$AJAR" ] || { echo "缺少 $AJAR（platforms;$PLATFORM 未安装）" >&2; exit 1; }

# d8（R8）在 JDK 21+ 上会崩（NullPointerException），而 build-tools 34 自带的
# 版本按 JDK 11~17 验证。所以这里主动挑一个 11~17 的 JDK，而不是盲信 JAVA_HOME。
jdk_major() {
  "$1/bin/java" -version 2>&1 | head -n 1 | sed -E 's/.*version "([0-9]+).*/\1/'
}

pick_jdk() {
  local candidates=() dir major
  for dir in "${JAVA_HOME:-}" /usr/lib/jvm/* /usr/lib64/jvm/* \
             "$HOME"/.local/share/mise/installs/java/* \
             /opt/java/* /opt/jdk*; do
    [ -n "$dir" ] && [ -x "$dir/bin/java" ] && candidates+=("$dir")
  done
  for dir in "${candidates[@]}"; do
    major="$(jdk_major "$dir" 2>/dev/null || echo 0)"
    [ "$major" = "1" ] && major=8   # 1.8.0 → 8
    if [ "$major" -ge 11 ] 2>/dev/null && [ "$major" -le 17 ] 2>/dev/null; then
      echo "$dir"
      return 0
    fi
  done
  return 1
}

if ! PICKED_JDK="$(pick_jdk)"; then
  echo "找不到 JDK 11~17（d8 在 JDK 21+ 上会崩）。" >&2
  echo "请安装 JDK 17 后重试，例如：JAVA_HOME=/path/to/jdk17 $0" >&2
  exit 1
fi
export JAVA_HOME="$PICKED_JDK"
export PATH="$JAVA_HOME/bin:$PATH"
echo "使用 JDK：$JAVA_HOME（$(jdk_major "$JAVA_HOME")）"

echo "==> 0/7 生成图标"
python3 tools/make_icons.py

rm -rf "$BUILD"
mkdir -p "$BUILD/res" "$BUILD/classes" "$BUILD/gen" "$BUILD/dex" "$DIST"

echo "==> 1/7 编译资源（aapt2 compile）"
"$BT/aapt2" compile --dir res -o "$BUILD/res.zip"

echo "==> 2/7 链接资源与清单（aapt2 link）"
# -A 把 assets/ 打进 APK：容器引导脚本与 proot 运行时都在里面，
# App 首次运行时再把它们复制到应用私有目录（见 Container.java）。
"$BT/aapt2" link \
  -o "$BUILD/base.apk" \
  -I "$AJAR" \
  --manifest AndroidManifest.xml \
  -A assets \
  --java "$BUILD/gen" \
  --min-sdk-version "$MIN_SDK" \
  --target-sdk-version "$TARGET_SDK" \
  --version-code "$VERSION_CODE" \
  --version-name "$VERSION_NAME" \
  "$BUILD/res.zip"

echo "==> 3/7 编译 Java（javac --release $JAVA_RELEASE）"
find src "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
javac --release "$JAVA_RELEASE" -encoding UTF-8 \
  -cp "$AJAR" -d "$BUILD/classes" "@$BUILD/sources.txt"

echo "==> 4/7 转成 dex（d8）"
find "$BUILD/classes" -name '*.class' > "$BUILD/classes.txt"
"$BT/d8" --lib "$AJAR" --min-api "$MIN_SDK" --output "$BUILD/dex" "@$BUILD/classes.txt"

echo "==> 5/7 打包并字节对齐（zip + zipalign）"
(cd "$BUILD/dex" && zip -q "../base.apk" classes.dex)
"$BT/zipalign" -f -p 4 "$BUILD/base.apk" "$BUILD/aligned.apk"

echo "==> 6/7 签名（apksigner）"
if [ ! -f "$KEYSTORE" ]; then
  # 首次构建生成一把自签名调试密钥并保留：换了密钥，覆盖安装会因签名不一致失败
  keytool -genkeypair -keystore "$KEYSTORE" -alias androiddebugkey \
    -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=memo-role Debug, O=memo-role, C=CN" >/dev/null 2>&1
  echo "    已生成调试密钥 $KEYSTORE（请勿用于正式发布）"
fi
"$BT/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$DIST/$APK_NAME" "$BUILD/aligned.apk"

echo "==> 7/7 校验"
"$BT/apksigner" verify --print-certs "$DIST/$APK_NAME" | head -n 4
"$BT/aapt2" dump badging "$DIST/$APK_NAME" | grep -E "^(package|application-label|launchable-activity|sdkVersion|targetSdkVersion)" || true

ls -lh "$DIST/$APK_NAME"
echo
echo "完成：$DIST/$APK_NAME"
echo "安装：adb install -r $DIST/$APK_NAME   （或把 APK 拷到手机上点击安装）"
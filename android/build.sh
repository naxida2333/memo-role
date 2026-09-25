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
VERSION_CODE="${VERSION_CODE:-8}"
VERSION_NAME="${VERSION_NAME:-0.3.0}"
JAVA_RELEASE="${JAVA_RELEASE:-11}"

BT="$SDK/build-tools/$BUILD_TOOLS"
AJAR="$SDK/platforms/$PLATFORM/android.jar"
BUILD="build"
DIST="dist"
KEYSTORE="debug.keystore"
# 记录签名证书指纹，用于在构建期发现「密钥被换过」（见文件末尾的校验步骤）。
# 这个文件要纳入版本管理；密钥本身（debug.keystore）不要。
CERT_FILE="signing-cert.sha256"
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

echo "==> 0/8 生成图标"
python3 tools/make_icons.py

# ----------------------------------------------------------------------
# 1/8 展开并校验本地推理引擎（llama-server）
#
# 这一份（约 27 MB 原始文件）不直接入库，仓库里放的是 tools/vendor_llama.py
# 裁好的 vendor/llama-*-ubuntu-arm64.tar.gz（约 12 MB）：构建时展开，再跑一次
# tests/check-llama.sh —— 漏文件、架构不对、glibc 要求过高都会在这里失败，
# 而不是等到手机上启动模型时才报一句看不懂的错。
# ----------------------------------------------------------------------
echo "==> 1/8 展开并校验本地推理引擎（llama-server）"
VENDOR_TARBALL="$(ls -1 vendor/llama-*-ubuntu-arm64.tar.gz 2>/dev/null | tail -1 || true)"
if [ -z "$VENDOR_TARBALL" ]; then
  echo "缺少 vendor/llama-*-ubuntu-arm64.tar.gz，先生成：" >&2
  echo "  python3 tools/vendor_llama.py    # 或 --archive /path/to/llama-xxx.tar.gz" >&2
  exit 1
fi
rm -rf assets/llama
mkdir -p assets/llama
tar -xzf "$VENDOR_TARBALL" -C assets/llama
bash tests/check-llama.sh

rm -rf "$BUILD"
mkdir -p "$BUILD/res" "$BUILD/classes" "$BUILD/gen" "$BUILD/dex" "$DIST"

echo "==> 2/8 编译资源（aapt2 compile）"
"$BT/aapt2" compile --dir res -o "$BUILD/res.zip"

echo "==> 3/8 链接资源与清单（aapt2 link）"
# -A 把 assets/ 打进 APK：容器引导脚本、proot 运行时与本地推理引擎都在里面，
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

echo "==> 4/8 编译 Java（javac --release $JAVA_RELEASE）"
find src "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
javac --release "$JAVA_RELEASE" -encoding UTF-8 \
  -cp "$AJAR" -d "$BUILD/classes" "@$BUILD/sources.txt"

echo "==> 5/8 转成 dex（d8）"
find "$BUILD/classes" -name '*.class' > "$BUILD/classes.txt"
"$BT/d8" --lib "$AJAR" --min-api "$MIN_SDK" --output "$BUILD/dex" "@$BUILD/classes.txt"

echo "==> 6/8 打包并字节对齐（zip + zipalign）"
(cd "$BUILD/dex" && zip -q "../base.apk" classes.dex)
"$BT/zipalign" -f -p 4 "$BUILD/base.apk" "$BUILD/aligned.apk"

echo "==> 7/8 准备签名密钥并校验指纹"
if [ ! -f "$KEYSTORE" ]; then
  # 首次构建生成一把自签名调试密钥并保留：换了密钥，覆盖安装会因签名不一致失败
  keytool -genkeypair -keystore "$KEYSTORE" -alias androiddebugkey \
    -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=memo-role Debug, O=memo-role, C=CN" >/dev/null 2>&1
  echo "    已生成调试密钥 $KEYSTORE（请勿用于正式发布）"
fi

# ----------------------------------------------------------------------
# 签名指纹校验
#
# 安装新版本时，安卓要求签名与已装版本一致，否则只能先卸载 —— 而卸载会连
# 应用数据一起删掉（这里就是整个容器、依赖和模型，几个 GB）。所以密钥必须
# 稳住：把指纹记在仓库里，密钥一变就立刻失败。
#
# 检查放在签名之前（直接读密钥文件），这样失败时 dist/ 里不会留下一个
# 「看着像成品、其实装不上去」的 APK。
#
# 确实要换密钥时（换机器、密钥丢了）：删掉 signing-cert.sha256 重新生成，
# 或用 ALLOW_KEY_CHANGE=1 临时放行；但要知道用户那次必须卸载重装。
# ----------------------------------------------------------------------
KEY_FPRINT="$(keytool -list -v -keystore "$KEYSTORE" -storepass android \
  -alias androiddebugkey 2>/dev/null \
  | grep -i 'SHA256:' | head -n 1 \
  | sed 's/.*SHA256:[[:space:]]*//' | tr -d ':' | tr 'A-Z' 'a-z')"
if [ -z "$KEY_FPRINT" ]; then
  echo "无法读取密钥指纹，构建中止" >&2
  exit 1
fi
EXPECTED=""
[ -f "$CERT_FILE" ] && EXPECTED="$(tr -d ' \n' < "$CERT_FILE" | tr 'A-Z' 'a-z')"
if [ -z "$EXPECTED" ]; then
  printf '%s\n' "$KEY_FPRINT" > "$CERT_FILE"
  echo "    已记录签名指纹到 $CERT_FILE：$KEY_FPRINT"
elif [ "$EXPECTED" != "$KEY_FPRINT" ]; then
  echo "" >&2
  echo "!!! 签名密钥与仓库记录的不一致，构建中止 !!!" >&2
  echo "    记录值：$EXPECTED" >&2
  echo "    本次值：$KEY_FPRINT" >&2
  echo "    用这把密钥签出的 APK 无法覆盖安装到旧版本上，只能卸载重装（数据会丢）。" >&2
  echo "    先找回原来的 $KEYSTORE；确实要换密钥就删掉 $CERT_FILE 重新构建。" >&2
  if [ "${ALLOW_KEY_CHANGE:-0}" != "1" ]; then
    echo "    （临时放行：ALLOW_KEY_CHANGE=1 ./build.sh）" >&2
    exit 1
  fi
  echo "    已按 ALLOW_KEY_CHANGE=1 放行，继续。" >&2
else
  echo "    密钥指纹与记录一致：$KEY_FPRINT"
fi

echo "==> 8/8 签名并校验产物"
"$BT/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$DIST/$APK_NAME" "$BUILD/aligned.apk"

# 再量一次成品：确认这个 APK 确实是上文那把密钥签的（防的是签错文件这类意外）
APK_FPRINT="$("$BT/apksigner" verify --print-certs "$DIST/$APK_NAME" 2>/dev/null \
  | grep 'SHA-256 digest' | head -n 1 | sed 's/.*: //' | tr -d ':' | tr 'A-Z' 'a-z')"
if [ "$APK_FPRINT" != "$KEY_FPRINT" ]; then
  echo "产物签名与密钥不符（密钥 $KEY_FPRINT / 产物 $APK_FPRINT），构建中止" >&2
  exit 1
fi
echo "    产物签名已核验：$APK_FPRINT"

"$BT/aapt2" dump badging "$DIST/$APK_NAME" | grep -E "^(package|application-label|launchable-activity|sdkVersion|targetSdkVersion)" || true

ls -lh "$DIST/$APK_NAME"
echo
echo "完成：$DIST/$APK_NAME"
echo "安装：adb install -r $DIST/$APK_NAME   （或把 APK 拷到手机上点击安装）"
#!/usr/bin/env bash
# CortexI 发布脚本：bump 版本 -> commit/push -> 打 tag -> 建 GitHub release -> 传客户端包
#
# 用法:
#   scripts/release.sh 0.1.1 "修了 xxx / 加了 yyy"
#
# 需要环境变量 GH_PAT（GitHub token），别写进 repo。
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:?用法: release.sh <version> [notes]}"
NOTES="${2:-CortexI v${VERSION}}"
REPO="${CORTEXI_REPO:-CrypticDriver/cortexi}"
: "${GH_PAT:?请先 export GH_PAT=<github token>}"

echo "$VERSION" > VERSION
git add -A
git commit -m "release: v${VERSION}" || echo "(nothing to commit)"

# push code (token via header, not stored in remote)
git -c http.extraHeader="Authorization: token ${GH_PAT}" push origin main

# build client tarball
bash scripts/package-release.sh "$VERSION"
ASSET="dist/cortexi-mac-v${VERSION}.tar.gz"

# create release + upload asset via API
GH_PAT="$GH_PAT" REPO="$REPO" VERSION="$VERSION" NOTES="$NOTES" ASSET="$ASSET" python3 - <<'PY'
import json, os, urllib.request, urllib.error
PAT=os.environ["GH_PAT"]; REPO=os.environ["REPO"]; V=os.environ["VERSION"]
NOTES=os.environ["NOTES"]; ASSET=os.environ["ASSET"]
def api(path, data=None, method="GET", host="api.github.com", ctype="application/json", raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data else None)
    req=urllib.request.Request(f"https://{host}{path}", data=body, method=method,
        headers={"Authorization":"token "+PAT,"Accept":"application/vnd.github+json",
                 "User-Agent":"cortexi-bot","Content-Type":ctype})
    return json.load(urllib.request.urlopen(req))
rel=api(f"/repos/{REPO}/releases", {"tag_name":f"v{V}","name":f"CortexI v{V}",
        "body":NOTES,"target_commitish":"main"}, method="POST")
print("release:", rel["html_url"])
name=os.path.basename(ASSET)
up=api(f"/repos/{REPO}/releases/{rel['id']}/assets?name={name}", raw=open(ASSET,"rb").read(),
       method="POST", host="uploads.github.com", ctype="application/gzip")
print("asset:", up["browser_download_url"])
PY
echo "✅ released v${VERSION}"

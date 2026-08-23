"""Chrome 扩展下载路由：把 `extensions/fliggy-cookie-sync/dist/` 里的 zip 暴露给 dashboard。

触发：用户从 /settings 页面点「下载 Cookie 同步扩展」。
安全：要求 dashboard 登录（require_login），跟其它内部路由一致。

zip 由 `extensions/fliggy-cookie-sync/build.sh` 在部署时生成
（见 `.github/workflows/deploy.yml` 的 remote install 步骤）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import auth as authmod

log = logging.getLogger("web.routes.extensions")

# 解析扩展 dist 目录：项目根 / extensions / fliggy-cookie-sync / dist
_EXT_ROOT = Path(__file__).resolve().parents[2] / "extensions" / "fliggy-cookie-sync"
_DIST_DIR = _EXT_ROOT / "dist"
_BUILD_SH = _EXT_ROOT / "build.sh"
_MANIFEST = _EXT_ROOT / "manifest.json"

router = APIRouter(prefix="/api/extensions", tags=["extensions"])


def _current_zip() -> Path | None:
    """返回 `dist/fliggy-cookie-sync.zip`（latest 软链）路径；不存在则返回 None。"""
    latest = _DIST_DIR / "fliggy-cookie-sync.zip"
    if latest.is_symlink() or latest.exists():
        return latest
    return None


def _ensure_zip() -> Path | None:
    """确保 zip 存在；缺失则尝试 build.sh。返回 zip 绝对路径。"""
    z = _current_zip()
    if z and z.exists():
        return z.resolve()

    if not _BUILD_SH.exists() or not _MANIFEST.exists():
        log.warning("extension source missing: build.sh=%s manifest=%s",
                    _BUILD_SH.exists(), _MANIFEST.exists())
        return None

    log.info("zip missing → running build.sh")
    try:
        subprocess.run(  # noqa: S607 — build.sh 是项目内受控脚本
            ["/bin/bash", str(_BUILD_SH)],
            cwd=str(_EXT_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.exception("build.sh failed: %s", e)
        return None
    z = _current_zip()
    return z.resolve() if z and z.exists() else None


def _version_from_manifest() -> str:
    try:
        import json
        return json.loads(_MANIFEST.read_text(encoding="utf-8")).get("version", "?")
    except (OSError, ValueError, KeyError):
        return "?"


@router.get("/fliggy-cookie-sync/zip")
async def download_cookie_sync_zip(
    request: Request,
    _=Depends(authmod.require_login),
):
    """下载 Cookie 同步扩展 zip。缺失则尝试 build.sh 重建。"""
    zip_path = _ensure_zip()
    if not zip_path:
        return JSONResponse(
            {"ok": False, "error": "扩展 zip 不存在且构建失败；"
                                   "请在服务器上跑 extensions/fliggy-cookie-sync/build.sh"},
            status_code=503,
        )

    # 用版本号做文件名（latest 软链 → 解析后的真名）
    real_name = zip_path.name  # e.g. fliggy-cookie-sync-1.0.0.zip
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=real_name,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Extension-Version": _version_from_manifest(),
        },
    )


@router.get("/fliggy-cookie-sync/info")
async def extension_info(
    request: Request,
    _=Depends(authmod.require_login),
):
    """给 UI 用的轻量元数据：版本号 + zip 大小 + mtime。"""
    z = _ensure_zip()
    return {
        "version": _version_from_manifest(),
        "zip_name": z.name if z else None,
        "zip_bytes": z.stat().st_size if z and z.exists() else 0,
        "zip_mtime": int(z.stat().st_mtime) if z and z.exists() else None,
        "download_url": "/api/extensions/fliggy-cookie-sync/zip",
    }
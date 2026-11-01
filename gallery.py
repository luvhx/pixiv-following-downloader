"""已下载作品的文件定位与删除。

缩略图改由 Pixiv 中图直链提供，这里不再生成、缓存任何缩略图。
删除遵循「文件全部删掉才删记录」：任一文件删除失败就保留数据库记录，
以便用户重试，不会出现记录消失但文件残留（或反之）的不一致。
"""
import json
import logging
import os
from urllib.parse import quote

import config
from storage import WebDAVStorage

logger = logging.getLogger(__name__)


def webdav_public_url(relative_path: str, root: str) -> str:
    """构造 WebDAV 浏览器直链（仅原图 302 使用）。"""
    base = (config.WEBDAV_PUBLIC_URL or config.WEBDAV_URL or "").rstrip("/")
    if not base:
        return ""
    url = base
    root = (root or "").strip("/")
    if root:
        url += "/" + quote(root, safe="/")
    rel = (relative_path or "").replace(os.sep, "/").lstrip("/")
    url += "/" + quote(rel, safe="/")
    return url


def _safe_rel(rel: str) -> str:
    """校验相对路径，拒绝绝对路径、盘符和上跳。"""
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise ValueError(f"非法相对路径: {rel}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"非法相对路径: {rel}")
    return "/".join(parts)


def _files_from_row(row: dict) -> list:
    raw = row.get("files")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for f in items:
        try:
            out.append(_safe_rel(f))
        except ValueError:
            continue
    return out


def _legacy_scan(storage, row: dict, dir_cache: dict = None) -> list:
    """旧数据按画师目录扫描。"""
    author = row.get("author_name") or ""
    illust_id = str(row["illust_id"])
    if dir_cache is not None and author in dir_cache:
        files = dir_cache[author]
    else:
        try:
            files = storage.list_files(author)
        except Exception as e:
            logger.warning(f"扫描目录失败 [{author}]: {e}")
            files = []
        if dir_cache is not None:
            dir_cache[author] = files
    matched = [f for f in files if os.path.basename(f).startswith(f"{illust_id}_")]
    matched.sort()
    return matched


def resolve_files(storage, db, row: dict, persist: bool = True,
                  dir_cache: dict = None) -> list:
    """返回作品文件列表；旧记录命中后回填数据库。"""
    files = _files_from_row(row)
    if files:
        return files
    files = _legacy_scan(storage, row, dir_cache)
    if files and persist:
        try:
            db.set_files(row["illust_id"], files)
        except Exception as e:
            logger.warning(f"回填文件路径失败 {row.get('illust_id')}: {e}")
    return files


def image_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "webm": "video/webm",
        "mp4": "video/mp4",
        "mov": "video/quicktime",
    }.get(ext, "application/octet-stream")


def cleanup_missing(db, storage) -> dict:
    """扫描数据库里的文件路径，删除全部缺失的记录、修正部分缺失的记录。"""
    removed = trimmed = missing = errors = 0
    for illust_id, files in db.all_files():
        if not files:
            continue
        existing = []
        for path in files:
            try:
                if isinstance(storage, WebDAVStorage):
                    # 严格模式：网络错误时保留记录，避免误删
                    exists = storage.exists(path, strict=True)
                else:
                    exists = storage.exists(path)
                if exists:
                    existing.append(path)
                else:
                    missing += 1
            except Exception as e:
                logger.warning(f"检查文件失败 {path}: {e}")
                errors += 1
                existing.append(path)  # 检查失败时保留，避免误删记录
        if not existing:
            db.delete(illust_id)
            removed += 1
        elif len(existing) != len(files):
            db.set_files(illust_id, existing)
            trimmed += 1
    logger.info(
        f"清理失效记录：移除 {removed}，修正 {trimmed}，缺失文件 {missing}，错误 {errors}"
    )
    return {
        "removed": removed, "trimmed": trimmed,
        "missing": missing, "errors": errors,
    }


def delete_illust(db, storage, row: dict) -> dict:
    """删除作品文件；全部成功（或文件本就不存在）后才删除数据库记录。"""
    files = resolve_files(storage, db, row)
    deleted = missing = 0
    errors = []
    for path in files:
        try:
            if storage.delete(path):
                deleted += 1
            else:
                missing += 1
        except Exception as e:
            logger.warning(f"删除文件失败 {path}: {e}")
            errors.append(str(e))

    result = {
        "deleted": deleted,
        "missing": missing,
        "errors": len(errors),
        "total": len(files),
        "removed": False,
    }
    if errors:
        logger.warning(
            f"作品 {row['illust_id']} 有 {len(errors)} 个文件删除失败，保留记录以便重试"
        )
        return result

    db.delete(row["illust_id"])
    result["removed"] = True
    return result

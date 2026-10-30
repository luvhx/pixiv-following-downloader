"""Pixiv 认证、抓取、过滤与作品下载。

与旧版的关键差异：
- 抓取支持水位（stop_at_id），只翻到已见过的最新作品为止；
- 缩略图直接取 Pixiv 中图直链（thumb_url），不再下载原图生成缩略图；
- 过滤规则来自 rules.snapshot()：tag 支持 include / exclude 两种模式；
- 下载先全部落到临时目录再上传，上传失败会回滚已写入的文件，避免孤儿；
- 请求节流参数由规则页控制。
"""
import logging
import os
import random
import re
import tempfile
import threading
import time

from pixivpy3 import AppPixivAPI

import config
from database import DownloadDB
from storage import get_storage

logger = logging.getLogger(__name__)

_auth_lock = threading.Lock()
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_FOLLOWING_PAGES = 100
MAX_ILLUST_PAGES = 60


# ---------- 认证 ----------
def is_authenticated(api: AppPixivAPI) -> bool:
    return bool(getattr(api, "access_token", None))


def create_api() -> AppPixivAPI:
    api = AppPixivAPI()
    if config.HTTPS_PROXY:
        api.set_proxy(config.HTTPS_PROXY)

    last_err = None
    for attempt in range(1, 4):
        try:
            api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
            logger.info("Pixiv API 认证成功")
            return api
        except Exception as e:
            last_err = e
            if attempt < 3:
                logger.warning(f"认证失败（第 {attempt}/3 次），{5 * attempt}s 后重试: {e}")
                time.sleep(5 * attempt)

    logger.error(f"认证失败，请检查 refresh_token 是否有效: {last_err}")
    raise last_err


def reauthenticate(api: AppPixivAPI):
    with _auth_lock:
        try:
            logger.warning("access_token 已失效，正在重新认证...")
            api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
            logger.info("access_token 刷新成功")
        except Exception as e:
            logger.error(f"重新认证失败，refresh_token 可能已失效: {e}")
            raise


def apply_proxy(api: AppPixivAPI):
    if config.HTTPS_PROXY:
        api.set_proxy(config.HTTPS_PROXY)
        logger.info(f"已更新代理: {config.HTTPS_PROXY}")


def start_token_refresher(api: AppPixivAPI):
    def _loop():
        last_interval = max(1, config.TOKEN_REFRESH_INTERVAL)
        next_at = time.time() + last_interval * 60
        while True:
            interval = max(1, config.TOKEN_REFRESH_INTERVAL)
            now = time.time()
            if interval != last_interval:
                last_interval = interval
                next_at = now + interval * 60
                logger.info(f"Token 刷新间隔已更新为 {interval} 分钟")
            if now >= next_at:
                try:
                    logger.info(f"后台定时刷新 access_token（间隔 {interval} 分钟）...")
                    with _auth_lock:
                        api.auth(refresh_token=config.PIXIV_REFRESH_TOKEN)
                    logger.info("后台刷新成功")
                except Exception as e:
                    logger.error(f"后台刷新失败: {e}")
                next_at = time.time() + interval * 60
            time.sleep(min(30, max(1, next_at - time.time())))

    threading.Thread(target=_loop, daemon=True, name="token-refresher").start()
    logger.info("已启动后台 token 刷新线程")


# ---------- 请求 / 抓取 ----------
def _is_auth_error(obj) -> bool:
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            text = " ".join(str(v) for v in err.values())
        else:
            text = str(err) if err else ""
    else:
        text = str(obj)
    text = text.lower()
    return "invalid_grant" in text or "authentication required" in text


def api_call(api: AppPixivAPI, func, *args, delay=None, jitter=None,
             retries=None, **kwargs):
    """带 token 刷新与退避重试的 API 调用。

    delay / jitter 留空时使用配置页里的请求节流参数；浏览接口可传更小的值。
    """
    if delay is None:
        delay = config.REQUEST_DELAY
    if jitter is None:
        jitter = config.REQUEST_JITTER
    if retries is None:
        retries = config.MAX_RETRIES
    rate_limit_wait = config.RATE_LIMIT_WAIT

    for attempt in range(1, retries + 1):
        try:
            if delay or jitter:
                time.sleep(max(0.0, delay) + random.uniform(0, max(0.0, jitter)))
            resp = func(*args, **kwargs)
            # Pixiv 错误常以 200 + error 返回
            if _is_auth_error(resp):
                logger.warning(f"检测到认证失效，刷新 access_token 后重试（第 {attempt} 次）")
                reauthenticate(api)
                continue
            return resp
        except Exception as e:
            if _is_auth_error(e):
                logger.warning(f"请求异常包含认证失效，刷新 access_token 后重试: {e}")
                reauthenticate(api)
                continue
            err_str = str(e)
            if "429" in err_str or "RateLimit" in err_str:
                wait = rate_limit_wait * attempt
                logger.warning(f"触发限速(429)，第 {attempt} 次重试，等待 {wait}s")
                time.sleep(wait)
            elif attempt < retries:
                logger.warning(f"请求异常: {e}，第 {attempt} 次重试")
                time.sleep(5 * attempt)
            else:
                raise
    raise RuntimeError("请求失败：已达最大重试次数")


def get_following_users(api: AppPixivAPI, user_id: str) -> list:
    """返回 [{user_id, user_name, avatar_url}]。"""
    users = []
    next_qs = {"user_id": user_id}
    pages = 0
    while next_qs and pages < MAX_FOLLOWING_PAGES:
        resp = api_call(api, api.user_following, **next_qs) or {}
        if resp.get("error"):
            # 不要用半截列表覆盖本地缓存
            raise RuntimeError(f"获取关注列表失败: {resp['error']}")
        for u in resp.get("user_previews") or []:
            user = u.get("user") or {}
            if not user.get("id"):
                continue
            previews = []
            for illust in u.get("illusts") or []:
                if not illust.get("id"):
                    continue
                previews.append({
                    "illust_id": illust["id"],
                    "title": illust.get("title") or "",
                    "thumb_url": select_thumb_url(illust),
                    "page_count": illust.get("page_count") or 1,
                    "bookmarks": illust.get("total_bookmarks") or 0,
                    "type": illust.get("type") or "illust",
                })
            # 用收藏数从接口返回的预览作品里挑 3 个作为「代表作」
            previews.sort(key=lambda item: item["bookmarks"], reverse=True)
            users.append({
                "user_id": user["id"],
                "user_name": user.get("name") or str(user["id"]),
                "avatar_url": (user.get("profile_image_urls") or {}).get("medium"),
                "previews": previews[:3],
            })
        next_qs = api.parse_qs(resp.get("next_url"))
        pages += 1
    if next_qs:
        logger.warning(f"关注列表超过 {MAX_FOLLOWING_PAGES} 页，只取前 {len(users)} 位画师")
    logger.info(f"共获取到 {len(users)} 位关注画师")
    return users


def _fetch_user_illusts_by_type(api: AppPixivAPI, artist_id: int, illust_type: str,
                                stop_at_id: int = 0):
    """按类型分页抓取；返回 (illusts, complete)。

    complete=False 表示因为页数上限提前停止，此时不应推进水位。
    """
    illusts = []
    next_qs = {"user_id": artist_id, "type": illust_type}
    pages = 0
    complete = True
    while next_qs:
        resp = api_call(api, api.user_illusts, **next_qs) or {}
        if resp.get("error"):
            # 分页中途出错：本轮结果不完整，不能推进水位
            logger.warning(f"  ⚠ 画师 {artist_id} API 返回错误: {resp['error']}")
            complete = False
            break
        page_items = resp.get("illusts") or []
        if stop_at_id and page_items and min(i["id"] for i in page_items) <= stop_at_id:
            # 作品按 id 倒序，出现已见作品即可停止
            illusts.extend(i for i in page_items if i["id"] > stop_at_id)
            complete = True
            break
        illusts.extend(page_items)
        next_qs = api.parse_qs(resp.get("next_url"))
        pages += 1
        if pages >= MAX_ILLUST_PAGES:
            logger.warning(f"  ⚠ 画师 {artist_id} 作品超过 {MAX_ILLUST_PAGES} 页，本轮提前停止")
            complete = False
            break
    return illusts, complete


def get_user_illusts(api: AppPixivAPI, artist_id: int, illust_type: str = "illust",
                     stop_at_id: int = 0):
    """返回 (illusts 按 id 倒序去重, complete)。"""
    if illust_type == "all":
        result, complete = {}, True
        for kind in ("illust", "manga"):
            items, done = _fetch_user_illusts_by_type(
                api, artist_id, kind, stop_at_id=stop_at_id
            )
            complete = complete and done
            for illust in items:
                result[illust["id"]] = illust
        merged = sorted(result.values(), key=lambda i: i["id"], reverse=True)
        return merged, complete
    if illust_type not in ("illust", "manga"):
        illust_type = "illust"
    return _fetch_user_illusts_by_type(
        api, artist_id, illust_type, stop_at_id=stop_at_id
    )


def _medium_from_original(url: str) -> str:
    """由原图 URL 推导中图直链（旧接口缺 medium 时兜底）。"""
    if not url or "/img-original/" not in url:
        return ""
    url = url.replace("/img-original/", "/c/540x540_70/img-master/")
    return re.sub(r"(_p\d+)\.(jpg|jpeg|png|gif)$", r"\1_master1200.\2", url, flags=re.I)


def select_thumb_url(illust: dict) -> str:
    """Pixiv 中图直链（用于画廊缩略图，不再本地生成）。"""
    pages = illust.get("meta_pages") or []
    page_urls = (pages[0].get("image_urls") or {}) if pages else {}
    top_urls = illust.get("image_urls") or {}
    single = illust.get("meta_single_page") or {}

    for urls in (page_urls, top_urls):
        url = urls.get("medium") or urls.get("large") or urls.get("square_medium")
        if url:
            return url
    url = single.get("medium_image_url") or single.get("large_image_url")
    if url:
        return url

    original = (
        page_urls.get("original") or top_urls.get("original")
        or single.get("original_image_url")
    )
    return _medium_from_original(original)


def illust_tag_names(illust: dict) -> list:
    """作品的全部 tag 名（原文 + 翻译名），保留原始大小写用于展示。"""
    names = []
    for tag in illust.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        for key in ("name", "translated_name"):
            value = (tag.get(key) or "").strip()
            if value and value not in names:
                names.append(value)
    return names


def _matches_tag(term: str, names: list) -> bool:
    term = term.strip().lower()
    return bool(term) and any(term == name.lower() for name in names)


def should_exclude(illust: dict, exclude_tags) -> bool:
    """排除：tag 精确命中，或标题包含该词（兼容旧的关键词过滤）。"""
    if not exclude_tags:
        return False
    title = (illust.get("title") or "").lower()
    names = illust_tag_names(illust)
    return any(_matches_tag(term, names) or term in title for term in exclude_tags)


def matches_include(illust: dict, include_tags) -> bool:
    """包含：tag 精确命中任一即可；列表为空表示不做白名单限制。"""
    if not include_tags:
        return True
    names = illust_tag_names(illust)
    return any(_matches_tag(term, names) for term in include_tags)


def meets_bookmark_requirement(illust: dict, min_count: int) -> bool:
    if min_count <= 0:
        return True
    return (illust.get("total_bookmarks", 0) or 0) >= min_count


# ---------- 下载 ----------
def sanitize_filename(name: str) -> str:
    """转为跨平台安全的文件名片段。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "").strip())
    name = name.strip(" .")
    if not name:
        return "untitled"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    encoded = name.encode("utf-8")
    if len(encoded) > 180:
        name = encoded[:180].decode("utf-8", "ignore")
    name = name.rstrip(" .")
    return name or "untitled"


def collect_image_urls(illust: dict, kind: str = "original",
                       first_page_only: bool = False) -> list:
    urls = []
    meta_pages = illust.get("meta_pages") or []
    meta_single = illust.get("meta_single_page") or {}
    if meta_pages:
        pages = meta_pages[:1] if first_page_only else meta_pages
        for page in pages:
            image_urls = page.get("image_urls") or {}
            url = image_urls.get(kind) or image_urls.get("large")
            if url:
                urls.append((page.get("page_index", 0), url))
    elif meta_single.get("original_image_url"):
        if kind == "original":
            url = meta_single["original_image_url"]
        elif kind == "large":
            url = meta_single.get("large_image_url") or meta_single["original_image_url"]
        else:
            url = meta_single.get("medium_image_url") or meta_single["original_image_url"]
        urls.append((0, url))
    else:
        image_urls = illust.get("image_urls") or {}
        url = image_urls.get(kind) or image_urls.get("large")
        if url:
            urls.append((0, url))
    return urls


def _download_pages(api: AppPixivAPI, urls: list, title: str,
                    illust_id: int, tmpdir: str, delay: float) -> list:
    """把页面下载到临时目录，返回 [(page_index, filename)]；失败抛异常。"""
    files = []
    for page_index, url in urls:
        ext = os.path.splitext(url.split("?")[0])[1].lower() or ".jpg"
        filename = (
            f"{illust_id}_{title}{ext}" if len(urls) == 1
            else f"{illust_id}_p{page_index}_{title}{ext}"
        )
        api.download(url, path=tmpdir, name=filename)
        tmp_file = os.path.join(tmpdir, filename)
        if not os.path.exists(tmp_file):
            raise RuntimeError(f"临时文件未生成: {tmp_file}")
        files.append((page_index, filename))
        if delay:
            time.sleep(delay)
    return files


def download_illust(api: AppPixivAPI, illust: dict, db: DownloadDB,
                    rules: dict = None) -> bool:
    """保存一个作品；返回是否完整保存（已有记录视为成功）。"""
    rules = rules or {}
    illust_id = illust["id"]
    if db.is_downloaded(illust_id):
        logger.debug(f"作品 {illust_id} 已下载，跳过")
        return True

    kind = rules.get("download_kind", "original")
    first_page_only = bool(rules.get("first_page_only", False))
    delay = config.DOWNLOAD_DELAY

    urls = collect_image_urls(illust, kind=kind, first_page_only=first_page_only)
    if not urls:
        logger.warning(f"作品 {illust_id} 未找到可下载 URL，跳过")
        return False

    author = illust.get("user") or {}
    author_id = author.get("id")
    author_name = sanitize_filename(author.get("name") or author_id or "unknown")
    title = sanitize_filename(illust.get("title", "untitled"))
    page_count = illust.get("page_count") or 1
    storage = get_storage()

    try:
        with tempfile.TemporaryDirectory(prefix=f"pixiv_{illust_id}_") as tmpdir:
            # 1) 先把所有页面下到临时目录：任何一页失败都不会写存储
            files = _download_pages(api, urls, title, illust_id, tmpdir, delay)

            # 2) 全部成功后按顺序上传；上传中途失败则回滚已写入的文件
            saved_paths = []
            try:
                for _page_index, filename in files:
                    relative_path = f"{author_name}/{filename}"
                    if storage.exists(relative_path):
                        saved_paths.append(relative_path)
                        continue
                    storage.save(os.path.join(tmpdir, filename), relative_path)
                    saved_paths.append(relative_path)
            except Exception as e:
                logger.error(f"保存到存储失败，回滚 {illust_id}: {e}")
                for path in saved_paths:
                    try:
                        storage.delete(path)
                    except Exception as cleanup_error:
                        logger.warning(f"回滚文件失败 {path}: {cleanup_error}")
                return False

        tags = illust_tag_names(illust)
        db.mark_downloaded(
            illust_id, title, author_id, author_name,
            tags, page_count, files=saved_paths,
            thumb_url=select_thumb_url(illust),
            illust_type=illust.get("type") or None,
            bookmarks=illust.get("total_bookmarks") or 0,
        )
        logger.info(
            f"✔ 已下载 [{author_name}] {title} "
            f"(ID: {illust_id}, 页数: {len(saved_paths)}/{page_count})"
        )
        return True
    except Exception as e:
        logger.error(f"下载作品 {illust_id} 失败: {e}")
        return False

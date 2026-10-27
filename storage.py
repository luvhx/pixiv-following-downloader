import os
import shutil
import logging
import threading
import time
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_RETRY_STATUS = {429, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_BASE = 2.0
_RETRY_MAX = 60.0
_VERIFY_INTERVAL = 2.0


def _norm_rel(relative_path: str) -> str:
    return str(relative_path).replace("\\", "/").strip("/")


def _quote_path(path: str) -> str:
    """逐段 URL 编码，保留 /，避免中文、空格、#、? 破坏 WebDAV 路径。"""
    return "/".join(quote(part, safe="") for part in _norm_rel(path).split("/") if part)


class LocalStorage:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _abs(self, relative_path: str) -> str:
        rel = _norm_rel(relative_path)
        full = os.path.abspath(os.path.join(self.root, rel))
        try:
            inside = os.path.commonpath([self.root, full]) == self.root
        except ValueError:
            inside = False  # Windows 跨盘符
        if not inside:
            raise ValueError(f"非法相对路径: {relative_path}")
        return full

    def abs_path(self, relative_path: str) -> str:
        return self._abs(relative_path)

    def exists(self, relative_path: str) -> bool:
        return os.path.exists(self._abs(relative_path))

    def save(self, local_temp_file: str, relative_path: str) -> str:
        """保存文件；跨分区时退化为 .part + rename。"""
        dest = self._abs(relative_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.replace(local_temp_file, dest)
            return dest
        except OSError:
            part = f"{dest}.part"
            try:
                shutil.copy2(local_temp_file, part)
                os.replace(part, dest)
            finally:
                for path in (part, local_temp_file):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
            return dest

    def delete(self, relative_path: str) -> bool:
        full = self._abs(relative_path)
        if not os.path.exists(full):
            return False
        os.remove(full)
        parent = os.path.dirname(full)
        if os.path.dirname(parent) == os.path.abspath(self.root):
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        return True

    def list_files(self, relative_dir: str) -> list:
        rel_dir = _norm_rel(relative_dir)
        full_dir = self._abs(rel_dir) if rel_dir else self.root
        if not os.path.isdir(full_dir):
            return []
        return [
            f"{rel_dir}/{name}" if rel_dir else name
            for name in os.listdir(full_dir)
            if os.path.isfile(os.path.join(full_dir, name))
        ]


class WebDAVStorage:
    """基于 requests 的最小 WebDAV 实现（MKCOL / HEAD / PUT / GET / DELETE / PROPFIND）。"""

    def __init__(self, base_url: str, username: str, password: str,
                 root: str = "", verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.root = root.strip("/")
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(username, password)
        self._session.verify = verify_ssl
        self._session.headers.update({"User-Agent": "pixiv-following-downloader/1.0"})
        # WebUI 多线程共享 Session，扩大连接池
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=32)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._dir_cache = set()
        self._dir_cache_lock = threading.Lock()

    def _retry_wait(self, resp, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        if retry_after:
            try:
                return min(_RETRY_MAX, max(1.0, float(retry_after)))
            except ValueError:
                pass
        return min(_RETRY_MAX, _RETRY_BASE * (2 ** attempt))

    def _request(self, method: str, url: str, *, attempts: int = _RETRY_ATTEMPTS, **kwargs):
        """带 429/5xx 退避重试的请求。"""
        last_error = None
        for attempt in range(attempts + 1):
            try:
                resp = self._session.request(method, url, **kwargs)
            except requests.RequestException as e:
                last_error = e
                if attempt >= attempts:
                    raise
                wait = min(_RETRY_MAX, _RETRY_BASE * (2 ** attempt))
                logger.warning(f"WebDAV {method} 异常，{wait:.0f}s 后重试: {e}")
                time.sleep(wait)
                continue
            if resp.status_code in _RETRY_STATUS and attempt < attempts:
                wait = self._retry_wait(resp, attempt)
                logger.warning(
                    f"WebDAV {method} {url} -> {resp.status_code}，"
                    f"{wait:.0f}s 后重试 ({attempt + 1}/{attempts})"
                )
                resp.close()
                time.sleep(wait)
                continue
            return resp
        raise last_error or RuntimeError(f"WebDAV {method} 请求失败: {url}")

    def _root_url(self) -> str:
        if not self.root:
            return self.base_url
        return f"{self.base_url}/{_quote_path(self.root)}"

    def _url_for(self, relative_path: str) -> str:
        rel = _quote_path(relative_path)
        return f"{self._root_url()}/{rel}" if rel else self._root_url()

    def download_to(self, relative_path: str, dest):
        """流式下载到路径或文件对象。"""
        url = self._url_for(relative_path)
        try:
            with self._request("GET", url, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"WebDAV 读取失败 {url}: {resp.status_code}"
                    )
                if hasattr(dest, "write"):
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            dest.write(chunk)
                else:
                    target = str(dest)
                    try:
                        with open(target, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)
                    except Exception:
                        try:
                            if os.path.exists(target):
                                os.remove(target)
                        except OSError:
                            pass
                        raise
        except requests.RequestException as e:
            raise RuntimeError(f"WebDAV GET 异常 {url}: {e}")

    def open_stream(self, relative_path: str, timeout: int = 300):
        """流式 GET；调用方负责 close。"""
        url = self._url_for(relative_path)
        try:
            resp = self._request("GET", url, stream=True, timeout=timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"WebDAV GET 异常 {url}: {e}")
        if resp.status_code != 200:
            resp.close()
            raise RuntimeError(f"WebDAV 读取失败 {url}: {resp.status_code}")
        return resp

    def delete(self, relative_path: str) -> bool:
        url = self._url_for(relative_path)
        try:
            resp = self._request("DELETE", url, timeout=60)
        except requests.RequestException as e:
            raise RuntimeError(f"WebDAV DELETE 异常 {url}: {e}")
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        raise RuntimeError(f"WebDAV 删除失败 {url}: {resp.status_code}")

    def list_files(self, relative_dir: str) -> list:
        rel_dir = _norm_rel(relative_dir)
        url = self._url_for(rel_dir) if rel_dir else self._root_url()
        url = url if url.endswith("/") else url + "/"
        try:
            resp = self._request(
                "PROPFIND", url, headers={"Depth": "1"}, timeout=60
            )
        except requests.RequestException as e:
            raise RuntimeError(f"WebDAV PROPFIND 异常 {url}: {e}")
        if resp.status_code == 404:
            return []
        if resp.status_code not in (200, 207):
            raise RuntimeError(f"WebDAV 列举失败 {url}: {resp.status_code}")
        names = self._parse_propfind(resp.content, url)
        return [f"{rel_dir}/{name}" if rel_dir else name for name in names]

    @staticmethod
    def _parse_propfind(xml_body: bytes, base_url: str) -> list:
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote, urlparse

        ns = "{DAV:}"
        base_path = unquote(urlparse(base_url).path).rstrip("/") + "/"
        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError as e:
            raise RuntimeError(f"WebDAV PROPFIND 响应解析失败: {e}")

        names = []
        for resp_el in root.findall(f"{ns}response"):
            href_el = resp_el.find(f"{ns}href")
            if href_el is None or not href_el.text:
                continue
            path = unquote(urlparse(href_el.text.strip()).path)
            if not path.startswith(base_path):
                continue
            name = path[len(base_path):].rstrip("/")
            if not name or "/" in name:
                continue
            if WebDAVStorage._is_collection(resp_el, ns):
                continue
            names.append(name)
        return names

    @staticmethod
    def _is_collection(resp_el, ns: str) -> bool:
        for propstat in resp_el.findall(f"{ns}propstat"):
            prop = propstat.find(f"{ns}prop")
            if prop is None:
                continue
            rt = prop.find(f"{ns}resourcetype")
            if rt is not None and rt.find(f"{ns}collection") is not None:
                return True
        return False

    def _mkcol(self, url: str):
        with self._dir_cache_lock:
            if url in self._dir_cache:
                return
            try:
                resp = self._request("MKCOL", url, timeout=30)
                sc = resp.status_code
                if sc in (201, 405) or (200 <= sc < 400):
                    self._dir_cache.add(url)
                elif sc == 409:
                    logger.warning(f"MKCOL {url} -> 409（父目录不存在）")
                else:
                    logger.warning(f"MKCOL {url} -> {sc} {resp.text[:120]}")
            except requests.RequestException as e:
                logger.warning(f"MKCOL {url} 异常: {e}")

    def _ensure_dir(self, relative_dir: str):
        """逐级创建 root 与 relative_dir。"""
        parts = [p for p in _norm_rel(self.root).split("/") if p]
        parts += [
            p for p in _norm_rel(relative_dir).split("/")
            if p and p != "."
        ]
        current = self.base_url
        for part in parts:
            current = f"{current}/{quote(part, safe='')}"
            self._mkcol(current)

    def exists(self, relative_path: str, attempts: int = _RETRY_ATTEMPTS) -> bool:
        url = self._url_for(relative_path)
        try:
            resp = self._request(
                "HEAD", url, attempts=attempts, timeout=30, allow_redirects=True,
            )
            # 部分 WebDAV 服务（如某些 Alist/Seafile 配置）不支持 HEAD
            if resp.status_code in (405, 501):
                resp = self._request(
                    "PROPFIND", url, attempts=attempts,
                    headers={"Depth": "0"}, timeout=30,
                )
        except requests.RequestException as e:
            logger.warning(f"WebDAV HEAD {url} 异常: {e}")
            return False
        if resp.status_code in (200, 207):
            return True
        if resp.status_code in (401, 403):
            raise RuntimeError(f"WebDAV 认证/权限失败 ({resp.status_code}) @ {url}")
        return False

    def _verify_saved(self, url: str, expected_size: int):
        """PUT 成功后确认文件已可见且大小正确；WebDAV 写入可能是异步的。"""
        import config

        timeout = max(0, int(getattr(config, "WEBDAV_VERIFY_TIMEOUT", 60) or 0))
        if timeout <= 0 or expected_size <= 0:
            return

        logger.info(f"等待 WebDAV 写入完成: {url}")
        deadline = time.monotonic() + timeout
        attempt = 0
        last_status = None
        while True:
            attempt += 1
            try:
                resp = self._request(
                    "HEAD", url, attempts=0, timeout=30, allow_redirects=True,
                )
                last_status = resp.status_code
                if resp.status_code in (200, 207):
                    length = resp.headers.get("Content-Length")
                    try:
                        remote_size = int(length) if length is not None else None
                    except (TypeError, ValueError):
                        remote_size = None
                    if remote_size is None or remote_size >= expected_size:
                        logger.info(f"WebDAV 文件已确认: {url}")
                        return
                elif resp.status_code in (405, 501):
                    resp = self._request(
                        "PROPFIND", url, attempts=0,
                        headers={"Depth": "0"}, timeout=30,
                    )
                    last_status = resp.status_code
                    if resp.status_code in (200, 207):
                        logger.info(f"WebDAV 文件已确认: {url}")
                        return
                elif resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"WebDAV 认证/权限失败 ({resp.status_code}) @ {url}"
                    )
            except requests.RequestException as e:
                last_status = str(e)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"WebDAV 文件未确认写入 {url}"
                    f"（大小 {expected_size}，最后状态 {last_status}）"
                )
            if attempt % 5 == 1:
                logger.info(f"WebDAV 尚未可见，继续等待: {url}（第 {attempt} 次）")
            time.sleep(min(_VERIFY_INTERVAL, max(0.1, remaining)))

    def save(self, local_temp_file: str, relative_path: str) -> str:
        # 即使文件位于 root 根目录，也要先创建目录链
        self._ensure_dir(os.path.dirname(relative_path))
        url = self._url_for(relative_path)

        last_error = None
        resp = None
        for attempt in range(_RETRY_ATTEMPTS + 1):
            try:
                with open(local_temp_file, "rb") as f:
                    resp = self._session.put(url, data=f, timeout=600)
            except requests.RequestException as e:
                last_error = e
                if attempt >= _RETRY_ATTEMPTS:
                    raise RuntimeError(f"WebDAV PUT 异常 {url}: {e}")
                wait = min(_RETRY_MAX, _RETRY_BASE * (2 ** attempt))
                logger.warning(f"WebDAV PUT 异常，{wait:.0f}s 后重试: {e}")
                time.sleep(wait)
                continue

            if resp.status_code in _RETRY_STATUS and attempt < _RETRY_ATTEMPTS:
                wait = self._retry_wait(resp, attempt)
                logger.warning(
                    f"WebDAV PUT {url} -> {resp.status_code}，"
                    f"{wait:.0f}s 后重试 ({attempt + 1}/{_RETRY_ATTEMPTS})"
                )
                resp.close()
                time.sleep(wait)
                continue
            break

        if resp is None:
            raise RuntimeError(f"WebDAV PUT 失败 {url}: {last_error}")
        if resp.status_code not in (200, 201, 204):
            body = resp.text[:200]
            resp.close()
            raise RuntimeError(
                f"WebDAV 上传失败 {url}: {resp.status_code} {body}"
            )
        self._verify_saved(url, os.path.getsize(local_temp_file))
        logger.debug(f"WebDAV 上传成功: {url}")
        return url


# ---------- 工厂 ----------
_storage = None
_storage_key = None
_storage_lock = threading.Lock()


def get_storage():
    """进程内存储单例，配置变化时自动重建。"""
    import config

    global _storage, _storage_key
    key = (
        config.STORAGE_BACKEND, config.DOWNLOAD_PATH,
        config.WEBDAV_URL, config.WEBDAV_USERNAME, config.WEBDAV_PASSWORD,
        config.WEBDAV_ROOT, config.WEBDAV_VERIFY_SSL,
    )
    if _storage is None or key != _storage_key:
        with _storage_lock:
            if _storage is None or key != _storage_key:
                if config.STORAGE_BACKEND == "webdav":
                    if not config.WEBDAV_URL:
                        raise ValueError("STORAGE_BACKEND=webdav 但未设置 WEBDAV_URL")
                    full = (
                        f"{config.WEBDAV_URL}/{config.WEBDAV_ROOT}"
                        if config.WEBDAV_ROOT else config.WEBDAV_URL
                    )
                    logger.info(f"存储后端: WebDAV ({full})")
                    _storage = WebDAVStorage(
                        base_url=config.WEBDAV_URL,
                        username=config.WEBDAV_USERNAME,
                        password=config.WEBDAV_PASSWORD,
                        root=config.WEBDAV_ROOT,
                        verify_ssl=config.WEBDAV_VERIFY_SSL,
                    )
                else:
                    logger.info(f"存储后端: 本地 ({config.DOWNLOAD_PATH})")
                    _storage = LocalStorage(config.DOWNLOAD_PATH)
                _storage_key = key
    return _storage
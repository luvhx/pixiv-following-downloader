"""入口：初始化 API/数据库/调度线程，并启动 WebUI 服务。"""
import logging
import sys

from pixivpy3 import AppPixivAPI

import config
from pixiv import create_api, start_token_refresher
from database import DownloadDB
from rules import Rules
from scheduler import Scheduler
from webui import create_app, install_log_capture

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    config.reload(force=True)

    # 认证失败不退出：用户可在 WebUI 中修正 refresh_token 后重新认证
    try:
        api = create_api()
    except Exception as e:
        logger.error(f"初始认证失败: {e}")
        logger.warning("以未认证状态启动，请在 WebUI 配置页修正 refresh_token")
        api = AppPixivAPI()
        if config.HTTPS_PROXY:
            api.set_proxy(config.HTTPS_PROXY)

    start_token_refresher(api)
    db = DownloadDB(config.DB_PATH)
    rules = Rules(db)
    rules.migrate_legacy_env()

    target_override = sys.argv[1] if len(sys.argv) > 1 else None
    if not target_override and not config.TARGET_USER_ID:
        logger.warning("未配置 TARGET_USER_ID，调度将跳过运行，请在 WebUI 配置页设置")

    install_log_capture()

    scheduler = Scheduler(api, db, rules, target_override)
    scheduler.start()

    logger.info(f"WebUI 监听 http://{config.WEBUI_HOST}:{config.WEBUI_PORT}")
    app = create_app(api, db, rules, scheduler)
    try:
        _serve(app)
    except KeyboardInterrupt:
        logger.info("收到中断信号，准备退出")
    finally:
        db.close()
        logger.info("程序退出")


def _serve(app):
    """优先使用 waitress 生产级 WSGI 服务器，未安装时回退到 Flask 内置服务器。"""
    try:
        from waitress import serve
    except ImportError:
        logger.warning("waitress 未安装，回退到 Flask 开发服务器（建议执行 pip install -r requirements.txt）")
        app.run(
            host=config.WEBUI_HOST,
            port=config.WEBUI_PORT,
            threaded=True,
            use_reloader=False,
        )
        return

    logger.info("使用 waitress 提供服务（多线程）")
    serve(app, host=config.WEBUI_HOST, port=config.WEBUI_PORT, threads=8)


if __name__ == "__main__":
    main()

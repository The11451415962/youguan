#!/usr/bin/env python3
"""
YouTube 频道更新监控脚本
- 监控多个 YouTube 频道的最新视频
- 发现新视频时通过邮件通知
- 通过 history.json 防止重复通知
- 多把 API Key 轮换：某把配额耗尽自动换下一把

用法:
    python yt_monitor.py           # 正常运行
    python yt_monitor.py --test    # 测试每把 API Key 和 SMTP 连通性
    python yt_monitor.py --init    # 首次运行，静默初始化（不发送邮件）
"""

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 加载环境变量 (.env 文件)
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"
LOG_PATH = BASE_DIR / "yt_monitor.log"

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 北京时间
# ---------------------------------------------------------------------------
BJT = timezone(timedelta(hours=8))

# 这些 reason 说明「这把钥匙今天没额度了」，应该换下一把而不是放弃
QUOTA_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}

# 运行期收集所有钥匙明文，用于把日志里的 key 打码
_SECRET_KEYS = []


def redact(text) -> str:
    """把日志里可能出现的 API Key 打码（CI 里 GitHub 也会打，本地不会）。"""
    out = str(text)
    for key in _SECRET_KEYS:
        if key:
            out = out.replace(key, "***")
    return out


def _http_error_detail(exc):
    """从 googleapiclient 的 HttpError 里取 (HTTP 状态码, reason)。"""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = ""
    try:
        payload = json.loads(exc.content.decode("utf-8", "ignore"))
        errors = (payload.get("error") or {}).get("errors") or []
        if errors:
            reason = errors[0].get("reason", "")
    except Exception:
        pass
    return status, reason


def is_quota_error(exc) -> bool:
    """判断这个异常是不是「钥匙配额耗尽」。"""
    status, reason = _http_error_detail(exc)
    if reason in QUOTA_REASONS:
        return True
    if status in (403, 429) and "quota" in str(exc).lower():
        return True
    return False


# ---------------------------------------------------------------------------
# 配置 / 历史记录
# ---------------------------------------------------------------------------

def _clean_channel_map(raw: dict) -> dict:
    """去掉 channel id 两端多余空白（历史配置里出现过带前导空格的 ID）。"""
    cleaned = {}
    for cid, name in (raw or {}).items():
        key = str(cid).strip()
        if key:
            cleaned[key] = name
    return cleaned


def load_config() -> dict:
    """加载 config.json，缺失字段时从环境变量补充。"""
    if not CONFIG_PATH.exists():
        logger.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 敏感信息优先从环境变量读取（覆盖 config.json）
    config["youtube_api_key"] = os.getenv("YOUTUBE_API_KEY", config.get("youtube_api_key", ""))
    config["smtp_password"] = os.getenv("SMTP_PASSWORD", config.get("smtp_password", ""))
    config["channels"] = _clean_channel_map(config.get("channels"))

    # 校验必要字段
    required = ["youtube_api_key", "channels", "smtp", "notification"]
    for key in required:
        if not config.get(key):
            logger.error(f"配置缺少必要字段: {key}")
            sys.exit(1)

    return config


def load_history() -> dict:
    """加载历史记录文件，并规范化 channel id。"""
    if not HISTORY_PATH.exists():
        return {}

    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    history = {}
    for cid, vids in (raw or {}).items():
        bucket = history.setdefault(str(cid).strip(), [])
        for vid in vids or []:
            if vid not in bucket:
                bucket.append(vid)
    return history


def save_history(history: dict) -> None:
    """保存历史记录文件。"""
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# YouTube API Key 池
# ---------------------------------------------------------------------------

class AllKeysExhausted(RuntimeError):
    """池子里所有钥匙都没有额度了。"""


class KeyPool:
    """多把 API Key 的轮换池。

    按顺序使用 YOUTUBE_API_KEY、YOUTUBE_API_KEY_2 … YOUTUBE_API_KEY_10，
    某把返回 quotaExceeded 就标记为耗尽、换下一把。

    注意：配额是按 Google Cloud「项目」算的，只有把钥匙分散在多个项目里，
    轮换才真的能扩容；同一个项目里多把钥匙仍然共用一份额度。
    """

    MAX_KEYS = 10

    def __init__(self):
        self.keys = self._load_keys()
        self.dead = {}       # key -> 原因
        self.clients = {}    # key -> youtube client

    @classmethod
    def _load_keys(cls):
        keys = []

        first = (os.getenv("YOUTUBE_API_KEY") or "").strip()
        if first and not first.startswith("YOUR_"):
            keys.append(first)

        for i in range(2, cls.MAX_KEYS + 1):
            k = (os.getenv(f"YOUTUBE_API_KEY_{i}") or "").strip()
            if k and not k.startswith("YOUR_") and k not in keys:
                keys.append(k)

        _SECRET_KEYS.extend(keys)
        return keys

    def index_of(self, key) -> str:
        """钥匙的展示名：只用序号，不输出任何 key 内容（仓库是公开的）。"""
        try:
            return f"#{self.keys.index(key) + 1}"
        except ValueError:
            return "#?"

    def alive(self):
        return [k for k in self.keys if k not in self.dead]

    def mark_dead(self, key, reason):
        self.dead[key] = reason

    def client(self, key):
        if key not in self.clients:
            self.clients[key] = build_youtube_client(key)
        return self.clients[key]

    def candidates(self):
        """按顺序产出（钥匙, 客户端），只给还有额度的。"""
        for key in self.alive():
            yield key, self.client(key)


def call_with_rotation(pool: KeyPool, fn):
    """用池子里还有额度的钥匙执行 fn；配额耗尽就换下一把。

    返回 (结果, 错误)。错误为 None 表示成功。
    非配额类错误（频道不存在、网络问题等）直接返回，不浪费其它钥匙。
    """
    last_error = None
    for key, youtube in pool.candidates():
        try:
            return fn(youtube), None
        except Exception as e:
            if is_quota_error(e):
                pool.mark_dead(key, "配额耗尽")
                logger.warning(f"    钥匙 {pool.index_of(key)} 配额耗尽，切换下一把")
                last_error = e
                continue
            return None, e
    return None, last_error or AllKeysExhausted("所有 API Key 都已耗尽配额")


# ---------------------------------------------------------------------------
# YouTube 数据获取
# ---------------------------------------------------------------------------

def get_uploads_playlist_id(youtube, channel_id: str) -> str:
    """获取频道对应的 uploads 播放列表 ID。"""
    request = youtube.channels().list(part="contentDetails", id=channel_id)
    response = request.execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(f"频道 ID 无效或不存在: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_latest_videos(youtube, uploads_playlist_id: str, max_results: int = 5) -> list:
    """从 uploads 播放列表获取最新视频，并获取时长以识别 Shorts。"""
    request = youtube.playlistItems().list(
        part="snippet",
        playlistId=uploads_playlist_id,
        maxResults=max_results,
    )
    response = request.execute()

    videos = []
    video_ids = []
    video_map = {}

    for item in response.get("items", []):
        snippet = item["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        published_at = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
        video_map[video_id] = {
            "video_id": video_id,
            "title": snippet["title"],
            "published_at": published_at,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "shorts_url": f"https://www.youtube.com/shorts/{video_id}",
        }
        video_ids.append(video_id)

    # 批量获取视频时长，判断是否为 Shorts (≤60秒)
    if video_ids:
        details_request = youtube.videos().list(
            part="contentDetails",
            id=",".join(video_ids),
        )
        details_response = details_request.execute()
        for detail_item in details_response.get("items", []):
            vid_id = detail_item["id"]
            duration_str = detail_item["contentDetails"]["duration"]
            seconds = parse_duration(duration_str)
            if vid_id in video_map:
                video_map[vid_id]["duration_seconds"] = seconds
                video_map[vid_id]["is_shorts"] = seconds is not None and 0 < seconds <= 60

    for vid in video_ids:
        if vid in video_map:
            videos.append(video_map[vid])

    return videos


def parse_duration(duration: str) -> int:
    """将 ISO 8601 时长 (PT#M#S) 转换为秒数。"""
    import re
    if not duration or duration == "P0D":
        return 0
    pattern = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
    match = pattern.match(duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------

def build_email(channel_name: str, videos: list, shorts_only: bool = False) -> MIMEMultipart:
    """构建通知邮件。"""
    video_type = "Shorts" if shorts_only else "视频"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[YouTube 监控] {channel_name} 更新了 {len(videos)} 个新{video_type}"
    msg["From"] = os.getenv("SMTP_SENDER", "")
    msg["To"] = os.getenv("NOTIFICATION_RECIPIENT", "")

    # 纯文本版本
    text_lines = [f"频道: {channel_name}\n发现 {len(videos)} 个新{video_type}:\n"]
    for v in videos:
        bjt_time = v["published_at"].astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
        # Shorts 用 shorts 链接，普通视频用 watch 链接
        link = v["shorts_url"] if shorts_only and v.get("is_shorts") else v["url"]
        text_lines.append(f"  - {v['title']}")
        text_lines.append(f"    链接: {link}")
        text_lines.append(f"    发布: {bjt_time} (北京时间)\n")
    text_content = "\n".join(text_lines)

    # HTML 版本
    html_lines = [
        f"<h2>频道: {channel_name}</h2>",
        f"<p>发现 <strong>{len(videos)}</strong> 个新{video_type}:</p>",
        "<ul>",
    ]
    for v in videos:
        bjt_time = v["published_at"].astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
        link = v["shorts_url"] if shorts_only and v.get("is_shorts") else v["url"]
        html_lines.append(
            f'<li><a href="{link}">{v["title"]}</a><br>'
            f'<small>发布: {bjt_time} (北京时间)</small></li>'
        )
    html_lines.append("</ul>")
    html_content = "\n".join(html_lines)

    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    return msg


def send_email(config: dict, msg: MIMEMultipart) -> None:
    """通过 SMTP 发送邮件，自动根据端口选择加密方式。"""
    smtp_cfg = config["smtp"]
    host = smtp_cfg["host"]
    port = smtp_cfg["port"]
    password = os.getenv("SMTP_PASSWORD", smtp_cfg.get("password", ""))
    sender = os.getenv("SMTP_SENDER", smtp_cfg.get("sender_email", ""))
    recipient = os.getenv("NOTIFICATION_RECIPIENT", config["notification"].get("recipient_email", ""))

    msg["From"] = sender
    msg["To"] = recipient

    try:
        if port == 465:
            # SSL/TLS 直连
            import ssl
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            # STARTTLS (587 等)
            server = smtplib.SMTP(host, port, timeout=30)

        server.ehlo()
        if port != 465 and smtp_cfg.get("use_tls", True):
            import ssl
            context = ssl.create_default_context()
            server.starttls(context=context)
            server.ehlo()

        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
        server.quit()
        logger.info(f"邮件发送成功 -> {recipient}")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        raise


def send_alert(config: dict, subject: str, lines: list) -> None:
    """发送「监控自身出问题了」的告警邮件。

    这是最容易失效、也最需要知道的一环：配额耗尽、钥匙全部不可用时，
    如果没有这封邮件，你会以为监控还在正常工作。
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[YouTube 监控异常] {subject}"
        msg["From"] = os.getenv("SMTP_SENDER", "")
        msg["To"] = os.getenv("NOTIFICATION_RECIPIENT", "")

        text = subject + "\n\n" + "\n".join(lines)
        html = (
            f"<h2>⚠️ {subject}</h2>"
            + "".join(f"<p>{redact(line)}</p>" for line in lines)
            + "<hr><p><small>这封邮件由监控脚本自己发出：它发现自己的检查已经失效。</small></p>"
        )
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        send_email(config, msg)
        logger.info("告警邮件已发送")
    except Exception as e:
        logger.error(f"告警邮件发送失败: {redact(e)}")


# ---------------------------------------------------------------------------
# YouTube 客户端
# ---------------------------------------------------------------------------

def build_youtube_client(api_key: str):
    """创建 YouTube API 客户端。

    - 本地开发：设置 CLASH_PROXY_HOST / CLASH_PROXY_PORT 走代理（默认 127.0.0.1:7897）
    - CI / 无需代理：不设（或设为空）上述环境变量，直接连接 YouTube API
    """
    from googleapiclient.discovery import build

    proxy_host = os.getenv("CLASH_PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("CLASH_PROXY_PORT", "7897")

    # 空值视为"无代理"，直接回退直连（避免 httplib2 带哑配置请求）
    if not proxy_host or not proxy_port:
        return build("youtube", "v3", developerKey=api_key)

    try:
        import httplib2
        proxy_info = httplib2.ProxyInfo(
            proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
            proxy_host=proxy_host,
            proxy_port=int(proxy_port),
        )
        http = httplib2.Http(proxy_info=proxy_info, timeout=15)
        return build("youtube", "v3", developerKey=api_key, http=http)
    except Exception:
        # 代理不可用时回退到直连
        logger.warning("代理连接失败，尝试直连 YouTube API")
        return build("youtube", "v3", developerKey=api_key)


# ---------------------------------------------------------------------------
# 主检查逻辑
# ---------------------------------------------------------------------------

def run_check(config: dict, init_mode: bool = False, shorts_only: bool = False) -> None:
    """主检查逻辑。

    Args:
        config: 配置字典
        init_mode: 首次初始化模式（只记录不发通知）
        shorts_only: 只监控 Shorts（过滤掉普通视频）
    """
    pool = KeyPool()
    if not pool.keys:
        logger.error("没有找到任何 YouTube API Key")
        sys.exit(1)

    logger.info(
        f"钥匙池: 共 {len(pool.keys)} 把 —— "
        + ", ".join(pool.index_of(k) for k in pool.keys)
    )

    history = load_history()
    channels = config["channels"]
    # Shorts 需要拉更多视频来过滤，普通视频看 5 个就够了
    max_videos = config["notification"].get("max_videos_per_channel", 15 if shorts_only else 5)

    total_new = 0
    failed = []
    mode_label = "Shorts" if shorts_only else "视频"

    for channel_id, channel_name in channels.items():
        logger.info(f"检查频道: {channel_name} ({channel_id})")

        def fetch(youtube, cid=channel_id, limit=max_videos):
            uploads_id = get_uploads_playlist_id(youtube, cid)
            return get_latest_videos(youtube, uploads_id, max_results=limit)

        videos, error = call_with_rotation(pool, fetch)
        if error is not None:
            logger.error(f"  获取频道 {channel_name} 视频失败: {redact(error)}")
            failed.append((channel_name, redact(error)))
            continue

        # Shorts 过滤
        if shorts_only:
            all_count = len(videos)
            videos = [v for v in videos if v.get("is_shorts")]
            logger.info(f"  过滤 Shorts: {all_count} 个视频中识别出 {len(videos)} 个 Shorts")

        if not videos:
            logger.info(f"  无新{mode_label}")
            continue

        # 筛选新视频（未通知过的）
        new_videos = []
        for v in videos:
            if v["video_id"] not in history.get(channel_id, []):
                new_videos.append(v)

        if not new_videos:
            logger.info(f"  无新{mode_label}")
            continue

        logger.info(f"  发现 {len(new_videos)} 个新{mode_label}")

        if init_mode:
            # 初始化模式：只记录，不发邮件
            for v in new_videos:
                history.setdefault(channel_id, []).append(v["video_id"])
            logger.info(f"  [初始化] 已记录 {len(new_videos)} 个{mode_label}，未发送通知")
        else:
            # 正常模式：发邮件 + 记录
            try:
                msg = build_email(channel_name, new_videos, shorts_only=shorts_only)
                send_email(config, msg)
                for v in new_videos:
                    history.setdefault(channel_id, []).append(v["video_id"])
                total_new += len(new_videos)
            except Exception as e:
                logger.error(f"  发送通知失败: {redact(e)}")

    save_history(history)

    # ---- 收尾：把「监控已经失效」这件事说清楚，并且让任务红掉 ----
    logger.info(f"钥匙池状态: 存活 {len(pool.alive())}/{len(pool.keys)} 把")
    for key, reason in pool.dead.items():
        logger.warning(f"  钥匙 {pool.index_of(key)} 已耗尽: {reason}")

    if init_mode:
        logger.info(f"初始化完成，后续运行将只通知新{mode_label}")
        return

    logger.info(f"检查完毕，共通知 {total_new} 个新{mode_label}")

    if not failed:
        return

    logger.error(f"{len(failed)}/{len(channels)} 个频道检查失败:")
    for name, reason in failed:
        logger.error(f"  - {name}: {reason}")

    all_down = not pool.alive() or len(failed) == len(channels)
    if all_down:
        subject = (
            "所有 API Key 配额耗尽" if not pool.alive()
            else "所有频道都检查失败"
        )
        send_alert(config, subject, [
            f"失败频道数: {len(failed)}/{len(channels)}",
            f"存活钥匙: {len(pool.alive())}/{len(pool.keys)}",
            "前几个失败原因:",
            *[f"· {name}: {reason[:120]}" for name, reason in failed[:3]],
            "这意味着本轮没有任何频道被检查到，监控处于失效状态。",
            "排查建议：检查 API Key 配额（Google Cloud 控制台）或频道 ID 是否还有效。",
        ])
        logger.error("监控已失效，退出码 2（让 GitHub Actions 变红）")
        sys.exit(2)


# ---------------------------------------------------------------------------
# 连通性测试
# ---------------------------------------------------------------------------

def run_test(config: dict) -> None:
    """测试每一把 API Key 和 SMTP 连通性。"""
    logger.info("=" * 50)
    logger.info("开始连通性测试")
    logger.info("=" * 50)

    pool = KeyPool()
    if not pool.keys:
        logger.error("没有找到任何 YouTube API Key")
        sys.exit(1)

    test_id = next(iter(config["channels"]))

    # 1. 逐把测试 API Key（这是验证「多把钥匙是不是都在干活」的关键）
    logger.info(f"\n[1/3] 测试 {len(pool.keys)} 把 YouTube API Key ...")
    usable = 0
    for key in pool.keys:
        label = pool.index_of(key)
        try:
            youtube = pool.client(key)
            get_uploads_playlist_id(youtube, test_id)
            logger.info(f"  ✓ 钥匙 {label} 正常")
            usable += 1
        except Exception as e:
            if is_quota_error(e):
                logger.warning(f"  ✗ 钥匙 {label} 配额已耗尽（今日不可用）")
            else:
                logger.error(f"  ✗ 钥匙 {label} 不可用: {redact(e)[:160]}")
    logger.info(f"  可用 {usable}/{len(pool.keys)} 把")
    if usable == 0:
        logger.error("没有一把钥匙可用，监控无法工作")
        return

    # 2. 测试所有频道 ID 是否合法
    logger.info("\n[2/3] 验证所有频道 ID ...")
    youtube = pool.client(pool.alive()[0])
    for cid, cname in config["channels"].items():
        try:
            get_uploads_playlist_id(youtube, cid)
            logger.info(f"  ✓ {cname} ({cid})")
        except Exception as e:
            logger.error(f"  ✗ {cname} ({cid}) - {redact(e)[:160]}")

    # 3. 测试 SMTP
    logger.info("\n[3/3] 测试 SMTP 邮件发送 ...")
    try:
        test_msg = MIMEMultipart()
        test_msg["Subject"] = "[YouTube 监控] 测试邮件"
        test_msg["From"] = os.getenv("SMTP_SENDER", config["smtp"].get("sender_email", ""))
        test_msg["To"] = os.getenv("NOTIFICATION_RECIPIENT", config["notification"].get("recipient_email", ""))
        body = "这是一封测试邮件。如果你收到了，说明 SMTP 配置正确。"
        test_msg.attach(MIMEText(body, "plain", "utf-8"))
        send_email(config, test_msg)
        logger.info("  ✓ 测试邮件已发送，请检查收件箱")
    except Exception as e:
        logger.error(f"  ✗ SMTP 测试失败: {redact(e)[:160]}")

    logger.info("\n" + "=" * 50)
    logger.info("测试完成")
    logger.info("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="YouTube 频道更新监控")
    parser.add_argument("--test", action="store_true", help="测试每把 API Key 和 SMTP 连通性")
    parser.add_argument("--init", action="store_true", help="首次运行，静默初始化（不发送邮件）")
    parser.add_argument("--web", action="store_true", help="启动 Web 仪表盘 (http://localhost:5000)")
    parser.add_argument("--shorts", action="store_true", help="只监控 Shorts 短视频（过滤普通视频）")
    args = parser.parse_args()

    if args.web:
        from web_dashboard import main as web_main
        web_main()
        return

    config = load_config()

    if args.test:
        run_test(config)
    else:
        run_check(config, init_mode=args.init, shorts_only=args.shorts)


if __name__ == "__main__":
    main()

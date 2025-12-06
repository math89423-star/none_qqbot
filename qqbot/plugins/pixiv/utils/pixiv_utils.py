import urllib.parse
import random
import aiohttp
import time
import threading
from http import HTTPStatus
from datetime import datetime, timedelta, timezone
from .error_utils import PixivAPIError
from ..config.config import (
    PIXIV_COOKIE, 
    PROXY, 
    PROXY_URL, 
    USE_PROXY, 
    EXCLUDE_DURATION
)

RECENT_IMAGES = {}
# 添加全局锁
RECENT_IMAGES_LOCK = threading.Lock()

# 核心辅助函数
def _is_r18_request(tags: list) -> bool:
    """检查是否明确请求R-18内容"""
    return any(tag.lower() in ["r-18", "r18", "r-18g", "r18g"] for tag in tags)

def _build_search_strategies() -> list:
    """构建三阶段搜索策略配置"""
    return [
        {
            "name": "精准模式(90天+高收藏)",
            "params": {
                "scd": (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d"),
                "ecd": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "blt": "500"
            }
        },
        {
            "name": "宽松模式(180天+中收藏)",
            "params": {
                "scd": (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d"),
                "ecd": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "blt": "100"
            }
        },
        {
            "name": "全站模式(无限制)",
            "params": {"blt": "0"}
        }
    ]

def _build_pixiv_headers(tags: list) -> dict:
    search_tag = " ".join(tags)
    """构建Pixiv请求头"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.pixiv.net/tags/{urllib.parse.quote(search_tag)}/artworks",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": PIXIV_COOKIE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest"
    }

async def _execute_search_strategy(
    search_tag: str,
    encoded_tag: str,
    strategy: dict,
    search_mode: str
) -> list:
    """执行单次搜索策略并返回原始结果列表"""
    headers = _build_pixiv_headers(search_tag)
    proxy = PROXY if USE_PROXY else None
    # 生成随机偏移
    offset = random.randint(0, 180)
    page = max(1, offset // 60 + 1)
    params = {
        "word": search_tag,
        "order": "popular_d",
        "mode": search_mode,
        "p": page,
        "s_mode": "s_tag",
        "type": "all",
        "lang": "zh",
        **strategy["params"]
    }
    # 发送搜索请求
    async with aiohttp.ClientSession() as session, \
        session.get(
            f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}",
            headers=headers,
            params=params,
            proxy=proxy,
            timeout=30
        ) as response:  # 合并写法
            if response.status != HTTPStatus.OK:
                raise PixivAPIError(
                    error_type = "api_failure",
                    strategy_name=strategy['name'],
                    details={"status": response.status}
                )
            data = await response.json()
            if not data.get("body") or not data["body"].get("illustManga", {}).get("data"):
                raise PixivAPIError(
                    error_type = "empty_data",
                    strategy_name=strategy['name'],
                    details={"status": response.status}
                )
            return data["body"]["illustManga"]["data"]

def _extract_tag_names(item: dict) -> list:
    """提取作品标签"""
    tags_info = item.get("tags", [])
    if isinstance(tags_info, dict):
        tags_info = tags_info.get("tags", [])

    return [
        tag.get("tag", "").lower()
        for tag in tags_info
        if isinstance(tag, dict)
    ]

def _is_r18_content(tag_names: list) -> bool:
    """检查R-18内容"""
    return any("r-18" in tag or "r18" in tag for tag in tag_names)

def _calculate_quality_scores(
    items: list,
    current_time: datetime
) -> list:
    """计算作品质量评分（含新鲜度加成）"""
    scored_items = []
    for item in items:
        # 基础质量指标
        bookmark_count = item.get("bookmarkCount", 0)
        like_count = item.get("likeCount", 0)
        view_count = item.get("viewCount", 0)
        quality_score = (bookmark_count * 3 + like_count * 2 + view_count * 0.05)
        # 新鲜度加成
        if create_date := item.get("createDate"):
            try:
                clean_date = create_date.split("T")[0]
                create_time = datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_old = (current_time - create_time).days
                freshness_factor = 1.5 if days_old <= 30 else 1.0
                freshness_factor = max(0.3, 1 - (days_old / 365)) if days_old > 90 else freshness_factor
                quality_score *= freshness_factor
            except Exception:
                pass  # 跳过日期解析错误
        scored_items.append((quality_score, item))
    return scored_items

def _process_search_results(
    raw_results: list,
    is_explicit_r18_request: bool,
    current_time: datetime
) -> list:
    """处理原始搜索结果（过滤/R-18验证/评分排序）"""
    # 过滤无效结果
    all_results = [
        item for item in raw_results
        if item and isinstance(item, dict)
        and item.get("id")
        and item.get("isAdContainer", 0) == 0
    ]
    # R-18内容过滤
    filtered_results = []
    for item in all_results:
        tag_names = _extract_tag_names(item)
        if is_explicit_r18_request or not _is_r18_content(tag_names):
            filtered_results.append(item)
    # 质量评分排序
    scored_items = _calculate_quality_scores(filtered_results, current_time)
    scored_items.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored_items[:100]]  # 返回前100候选

def _clean_old_cache(current_timestamp: float):
    """清理过期缓存作品"""
    for pid, timestamp in list(RECENT_IMAGES.items()):
        if current_timestamp - timestamp > EXCLUDE_DURATION:
            del RECENT_IMAGES[pid]

def _select_best_image(candidates: list, is_explicit_r18_request: bool) -> dict:
    """从候选作品中选择最佳作品（考虑历史使用）"""
    current_timestamp = time.time()
    # 使用锁来保护全局缓存
    with RECENT_IMAGES_LOCK:
        # 1. 优先选择高质量且未使用过的作品
        unused_high_quality = [
            item for item in candidates[:30] 
            if str(item["id"]) not in RECENT_IMAGES
        ]
        if unused_high_quality:
            return random.choice(unused_high_quality)
        # 2. 次选：所有未使用过的作品
        unused_all = [
            item for item in candidates
            if str(item["id"]) not in RECENT_IMAGES
        ]
        if unused_all:
            return random.choice(unused_all)
        # 3. 保底：使用最久未用的作品
        _clean_old_cache(current_timestamp)
        oldest_pid = min(RECENT_IMAGES.items(), key=lambda x: x[1])[0] if RECENT_IMAGES else None
        return next(
            (item for item in candidates if str(item["id"]) == oldest_pid),
            candidates[0]
        )

def _replace_image_domain(url: str) -> str:
    """将Pixiv图片域名替换为代理域名，并确保文件格式兼容"""
    if not url.startswith("http"):
        url = "https:" + url
    proxy_base = PROXY_URL.rstrip('/') + '/'
    # 修复URL中的转义字符
    url = url.replace("%2F", "/").replace("%3A", ":")
    if "i.pximg.net" in url:
        url = url.replace("https://i.pximg.net", proxy_base.rstrip('/'))
    elif "pixiv.cat" in url:
        url = url.replace("https://pixiv.cat", proxy_base.rstrip('/'))
    # 确保文件格式兼容（避免WebP等不支持的格式）
    if url.endswith('.webp'):
        url = url[:-5] + '.jpg'  # 转为 jpg
    elif url.endswith('.gif') and 'ugoira' not in url:  # 非动图GIF转为JPG
        url = url[:-4] + '.jpg'
    # 替换URL中的特殊字符（防止路径问题）
    url = url.replace(' ', '%20').replace('&', '%26').replace('?', '%3F')
    return url

async def _validate_and_build_response(
    selected: dict,
    is_explicit_r18_request: bool,
    encoded_tag: list
) -> dict:
    """获取作品详情并验证R-18内容"""
    # 获取作品详情
    illust_id = selected["id"]
    illust_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    headers = _build_pixiv_headers(encoded_tag)
    headers.update({"Referer": f"https://www.pixiv.net/artworks/{illust_id}"})

    async with aiohttp.ClientSession() as session, \
        session.get(
            illust_url,
            headers=headers,
            proxy=PROXY if USE_PROXY else None,
            timeout=20
        ) as response:
            if response.status != HTTPStatus.OK:
                raise Exception(f"获取作品详情失败: {response.status}")

            data = await response.json()
            if data.get("error"):
                raise Exception(f"作品详情错误: {data.get('message', '未知错误')}")

            # 二次R-18验证
            work_tags = _extract_tag_names(data["body"])
            if not is_explicit_r18_request and (_is_r18_content(work_tags)):
                raise Exception("检测到R-18内容但未明确请求")

            # 构建返回结果
            body = data["body"]
            return {
                "image_url": _replace_image_domain(body["urls"]["original"]),
                "pid": str(illust_id),
                "title": body["title"],
                "author": body["userName"],
                "author_id": body["userId"],
                "work_url": f"https://www.pixiv.net/artworks/{illust_id}",
                "preview_url": _replace_image_domain(body["urls"]["regular"]),
                "original_url": body["urls"]["original"],
                "stats": {
                    "bookmarks": selected.get("bookmarkCount", 0),
                    "likes": selected.get("likeCount", 0),
                    "views": selected.get("viewCount", 0)
                },
                "strategy_used": selected.get("strategy_used", "unknown")
            }
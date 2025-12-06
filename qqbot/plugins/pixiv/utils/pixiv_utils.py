import urllib.parse
import random
import aiohttp
import time
import threading
import math
import logging
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

# 创建日志
logger = logging.getLogger()
logging.basicConfig(level = logging.INFO,format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

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
            },
            "page": 1,  # 精准模式固定第一页
            "mode": "s_tag"  # 明确搜索模式
        },
        {
            "name": "宽松模式(360天+中收藏)",
            "params": {
                "scd": (datetime.now(timezone.utc) - timedelta(days=360)).strftime("%Y-%m-%d"),
                "ecd": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "blt": "100"
            },
            "page_range": (1, 3),  # 1-3页
            "mode": "s_tag"
        },
        {
            "name": "全站模式(无限制)",
            "params": {},
            "page_range": (1, 5),  # 1-5页
            "mode": "s_tag"
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
) -> list:
    """执行单次搜索策略并返回原始结果列表"""
    headers = _build_pixiv_headers(search_tag)
    proxy = PROXY if USE_PROXY else None
       # 从策略获取页码 (精准模式固定第一页)
    if "page" in strategy:
        page = strategy["page"]
    else:
        page_range = strategy.get("page_range", (1, 3))
        page = random.randint(*page_range)
    # 从策略获取搜索模式
    search_mode = strategy.get("mode", "s_tag")
    params = {
        "word": search_tag,
        "order": "popular_d",  # 按受欢迎度排序
        "mode": search_mode,
        "p": page,
        "s_mode": "s_tag",     # 标签完全匹配
        "type": "all",
        "lang": "zh",
        **strategy["params"]
    }
    # 添加调试日志
    logger.debug(f"请求策略: {strategy['name']}, 页码: {page}, 参数: {params}")
    async with aiohttp.ClientSession() as session, \
        session.get(
            f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}",
            headers=headers,
            params=params,
            proxy=proxy,
            timeout=30
        ) as response:
            if response.status != HTTPStatus.OK:
                raise PixivAPIError(
                    error_type = "api_failure",
                    strategy_name = strategy['name'],
                    details={"status": response.status}
                )
            data = await response.json()
            if not data.get("body") or not data["body"].get("illustManga", {}).get("data"):
                raise PixivAPIError(
                    error_type = "empty_data",
                    strategy_name = strategy['name'],
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
    """计算作品质量评分（优化版：综合考虑绝对数量、互动比率和新鲜度）"""
    scored_items = []
    for item in items:
        # 基础指标
        bookmark_count = item.get("bookmarkCount", 0)  # 收藏数
        like_count = item.get("likeCount", 0)          # 点赞数
        view_count = max(1, item.get("viewCount", 1))  # 浏览量，至少为1
        # 1. 计算基础质量得分
        # 1.1 绝对互动分（收藏权重更高，反映Pixiv平台特性）
        absolute_score = bookmark_count * 5 + like_count * 3
        # 1.2 比率分（高质量低曝光作品的补偿机制）
        bookmark_ratio = bookmark_count / view_count
        like_ratio = like_count / view_count
        # 比率分：将互动比率转换为加分项（收藏率5%+和点赞率15%+被视为高质量）
        ratio_score = 0
        if bookmark_ratio > 0.05:  # 收藏率超过5%
            ratio_score += (bookmark_ratio - 0.05) * 2000  # 每超过1%加20分
        if like_ratio > 0.15:      # 点赞率超过15%
            ratio_score += (like_ratio - 0.15) * 500       # 每超过1%加5分
        # 1.3 高互动率作品额外加成（针对新作品或小众优质作品）
        if view_count < 1000 and bookmark_ratio > 0.1:  # 低浏览但高收藏率
            ratio_score *= 1.5
        # 1.4 综合基础质量得分
        quality_score = absolute_score + ratio_score
        # 2. 新鲜度加成
        if create_date := item.get("createDate"):
            try:
                clean_date = create_date.split("T")[0]
                create_time = datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_old = (current_time - create_time).days
                # 新鲜度因子（更平滑的衰减曲线）
                if days_old <= 3:      # 3天内
                    freshness_factor = 2.0
                elif days_old <= 7:    # 一周内
                    freshness_factor = 1.6
                elif days_old <= 14:   # 两周内
                    freshness_factor = 1.3
                elif days_old <= 30:   # 一个月内
                    freshness_factor = 1.15
                elif days_old <= 60:   # 两个月内
                    freshness_factor = 1.05
                elif days_old <= 90:   # 三个月内
                    freshness_factor = 1.0
                else:
                    # 90天以上，每多30天衰减0.05，最低0.5
                    decay_factor = max(0, (days_old - 90) / 30) * 0.05
                    freshness_factor = max(0.5, 1.0 - decay_factor)
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

def _cleanup_recent_images():
    """清理超过24小时的图片ID"""
    now = time.time()
    for image_id, timestamp in list(RECENT_IMAGES.items()):
        if now - timestamp > 24 * 3600:  # 24小时
            del RECENT_IMAGES[image_id]

import aiohttp
import aiofiles
import json
import urllib.parse
import random
import time
import configparser
import os
import asyncio
import ssl
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 读取环境变量
config_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(config_dir, 'config.conf')
config = configparser.ConfigParser()
config.read(config_path)

# 优先使用环境变量，其次使用默认值
PROXY = config.get('DEFAULT', 'PROXY', fallback='http://127.0.0.1:7890')
USE_PROXY = config.getboolean('DEFAULT', 'USE_PROXY', fallback=True)
PROXY_URL = config.get('DEFAULT', 'PROXY_URL', fallback='https://quiet-hill-31f3.math89423.workers.dev/')
PIXIV_COOKIE = config.get('DEFAULT', 'PIXIV_COOKIE', fallback='PHPSESSID=14916444_EuNtNE3Yd2ZZ50A7UzivUlxP7O2hLP7s; device_token=ccd49454e972c3b547f1db56a3560575; p_ab_id=1; p_ab_id_2=1')
COOLDOWN_TIME = config.getint('DEFAULT', 'COOLDOWN_TIME', fallback=25)
MAX_DOWNLOAD_CHUNK = config.getint('DEFAULT', 'MAX_DOWNLOAD_CHUNK', fallback=1024 * 64)
DOWNLOAD_TIMEOUT = config.getint('DEFAULT', 'DOWNLOAD_TIMEOUT', fallback=60)
MAX_ATTEMPTS = config.getint('DEFAULT', 'MAX_ATTEMPTS', fallback=2)
EXCLUDE_DURATION = config.getint('DEFAULT', 'EXCLUDE_DURATION', fallback=3600)  

# 基础项目目录
BASE_DIR = Path(__file__).parent.parent.parent.absolute()
DATA_DIR = BASE_DIR / "data"
TEMP_DIR = DATA_DIR / "pixiv_temp"  # 专用临时目录

# 创建目录
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 近期图片缓存排除机制
RECENT_IMAGES = {}  # {pid: last_used_time}

# 创建日志
logger = logging.getLogger()
logging.basicConfig(level = logging.INFO,format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# ====== PIXIV逻辑核心函数 ======
async def search_pixiv_by_tag(tags: list, max_results=10) -> dict:
    """
    TODO:
    通过角色标签搜索Pixiv图片（智能适应新角色/冷门角色，三阶段重试策略）
    策略1：精准模式 -> 满足最近90Day发布 + 收藏数 >= 500
    策略2：宽松模式 -> 满足最近180Day发布 + 收藏数 >= 100
    策略3：全站模式 -> 无限制搜索
    """
    search_tag = " ".join(tags)
    encoded_tag = urllib.parse.quote(search_tag)

    # ===== 检查是否明确请求R-18内容 =====
    is_explicit_r18_request = any(tag.lower() in ["r-18", "r18", "r-18g", "r18g"] for tag in tags)
    search_mode = "all" if is_explicit_r18_request else "safe"

    # ===== 三阶段搜索策略配置 =====
    # 构建搜索请求体
    search_strategies = [
        {
            "name": "精准模式(90天+高收藏)",
            "params": {
                "scd": (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
                "ecd": datetime.now().strftime("%Y-%m-%d"),
                "blt": "500"  # 初始高质量阈值
            }
        },
        {
            "name": "宽松模式(180天+中收藏)",
            "params": {
                "scd": (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d"),
                "ecd": datetime.now().strftime("%Y-%m-%d"),
                "blt": "100"  # 降低质量阈值
            }
        },
        {
            "name": "全站模式(无限制)",
            "params": {
                # 不设置时间范围
                "blt": "0"  # 最低收藏数设为0
            }
        }
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.pixiv.net/tags/{encoded_tag}/artworks",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": PIXIV_COOKIE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest"
    }

    proxy = PROXY if USE_PROXY else None
    current_time = datetime.now(timezone.utc)

    # ===== 三阶段重试策略 =====
    for strategy_idx, strategy in enumerate(search_strategies):
        try:
            # 为每次尝试生成新的随机偏移（扩大覆盖范围）
            offset = random.randint(0, 180)
            page = max(1, offset // 60 + 1)
            url = f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}"
            params = {
                "word": search_tag,
                "order": "popular_d",
                "mode": search_mode,
                "p": page,
                "s_mode": "s_tag",
                "type": "all",
                "lang": "zh",
                **strategy["params"]  # 合并当前策略参数
            }
            async with aiohttp.ClientSession() as session:
                # ===== 执行搜索请求 =====
                async with session.get(
                    url, 
                    headers=headers, 
                    params=params, 
                    proxy=proxy, 
                    timeout=30
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"策略[{strategy['name']}]请求失败: {response.status}")
                    
                    data = await response.json()
                    if not data.get("body") or not data["body"].get("illustManga", {}).get("data"):
                        raise Exception(f"策略[{strategy['name']}]返回空数据")
                    
                    # ===== 预处理结果 =====
                    all_results = [
                        item for item in data["body"]["illustManga"]["data"]
                        if item and isinstance(item, dict) 
                        and item.get("id") 
                        and item.get("isAdContainer", 0) == 0
                    ]
                    
                    # ===== R-18内容智能过滤 =====
                    filtered_results = []
                    for item in all_results:
                        # 安全获取标签
                        tags_info = item.get("tags", [])
                        if isinstance(tags_info, dict):
                            tags_info = tags_info.get("tags", [])
                        
                        tag_names = [tag.get("tag", "").lower()
                                   for tag in tags_info
                                   if isinstance(tag, dict)]
                        
                        # 检测R-18内容
                        is_r18 = any("r-18" in tag or "r18" in tag for tag in tag_names)
                        is_r18g = any("r-18g" in tag or "r18g" in tag for tag in tag_names)
                        
                        # 仅保留符合条件的作品
                        if is_explicit_r18_request or (not is_r18 and not is_r18g):
                            filtered_results.append(item)
                    
                    # ===== 质量+新鲜度评分 =====
                    scored_items = []
                    for item in filtered_results:
                        # 基础质量指标
                        bookmark_count = item.get("bookmarkCount", 0)
                        like_count = item.get("likeCount", 0)
                        view_count = item.get("viewCount", 0)
                        
                        # 质量分数 (降低权重放大效应)
                        quality_score = (bookmark_count * 3 + like_count * 2 + view_count * 0.05)
                        
                        # 新鲜度加成 (新角色保护)
                        create_date = item.get("createDate", "")
                        if create_date:
                            try:
                                # 统一日期格式处理
                                clean_date = create_date.split("T")[0]
                                create_time = datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                                days_old = (current_time - create_time).days
                                
                                # 新角色特殊处理：30天内作品获得额外权重
                                if days_old <= 30:
                                    freshness_factor = 1.5  # 新作品1.5倍权重
                                elif days_old <= 90:
                                    freshness_factor = 1.0
                                else:
                                    # 超过90天但符合当前策略的作品
                                    freshness_factor = max(0.3, 1 - (days_old / 365))
                                
                                quality_score *= freshness_factor
                            except Exception as date_error:
                                logger.debug(f"日期解析失败({item.get('id')}): {str(date_error)}")
                        
                        scored_items.append((quality_score, item))
                    
                    # 按质量排序
                    scored_items.sort(key=lambda x: x[0], reverse=True)
                    candidates = [item for _, item in scored_items[:100]]  # 保留100个候选
                    
                    # ===== 结果验证 =====
                    if not candidates:
                        logger.debug(f"策略[{strategy['name']}]无有效结果，尝试下一策略")
                        continue
                    
                    # ===== 智能选择机制 =====
                    selected = None
                    current_timestamp = time.time()
                    
                    # 1. 优先选择高质量且未使用过的作品
                    unused_high_quality = [
                        item for item in candidates[:30] 
                        if str(item["id"]) not in RECENT_IMAGES
                    ]
                    
                    if unused_high_quality:
                        selected = random.choice(unused_high_quality)
                    # 2. 次选：所有未使用过的作品
                    elif len(candidates) > 1:
                        unused_all = [
                            item for item in candidates
                            if str(item["id"]) not in RECENT_IMAGES
                        ]
                        if unused_all:
                            selected = random.choice(unused_all)
                    
                    # 3. 保底：使用最久未用的作品
                    if not selected:
                        # 清理过期缓存
                        for pid, timestamp in list(RECENT_IMAGES.items()):
                            if current_timestamp - timestamp > EXCLUDE_DURATION:
                                del RECENT_IMAGES[pid]
                        
                        # 找出最久未用的作品
                        oldest_pid = min(RECENT_IMAGES.items(), key=lambda x: x[1])[0] if RECENT_IMAGES else None
                        if oldest_pid:
                            selected = next(
                                (item for item in candidates if str(item["id"]) == oldest_pid),
                                candidates[0]
                            )
                        else:
                            selected = candidates[0]
                    
                    # ===== 获取作品详情 =====
                    illust_id = selected["id"]
                    illust_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
                    illust_headers = {
                        **headers,
                        "Referer": f"https://www.pixiv.net/artworks/{illust_id}",
                        "X-Requested-With": "XMLHttpRequest"
                    }
                    
                    async with session.get(
                        illust_url, 
                        headers=illust_headers, 
                        proxy=proxy, 
                        timeout=20
                    ) as illust_response:
                        if illust_response.status != 200:
                            raise Exception(f"获取作品详情失败: {illust_response.status}")
                        
                        illust_data = await illust_response.json()
                        if illust_data.get("error"):
                            raise Exception(f"作品详情错误: {illust_data.get('message', '未知错误')}")
                        
                        illust_body = illust_data["body"]
                        
                        # 二次R-18验证
                        work_tags = [tag.get("tag", "").lower() 
                                   for tag in illust_body.get("tags", {}).get("tags", [])]
                        is_work_r18 = any(tag in ["r-18", "r18"] for tag in work_tags)
                        is_work_r18g = any(tag in ["r-18g", "r18g"] for tag in work_tags)
                        
                        if not is_explicit_r18_request and (is_work_r18 or is_work_r18g):
                            logger.warning(f"检测到R-18内容但未明确请求，跳过作品ID: {illust_id}")
                            # 从候选列表中移除该作品并重试
                            candidates = [item for item in candidates if item["id"] != illust_id]
                            if candidates:
                                continue  # 用剩余候选重试
                            raise Exception("检测到不适当内容，已跳过。请尝试其他标签。")
                        
                        # ===== 构建返回结果 =====
                        original_img_url = illust_body["urls"]["original"]
                        regular_img_url = illust_body["urls"]["regular"]
                        
                        # 记录使用时间
                        RECENT_IMAGES[str(illust_id)] = current_timestamp
                        
                        return {
                            "image_url": replace_image_domain(original_img_url),
                            "pid": str(illust_id),
                            "title": illust_body["title"],
                            "author": illust_body["userName"],
                            "author_id": illust_body["userId"],
                            "work_url": f"https://www.pixiv.net/artworks/{illust_id}",
                            "preview_url": replace_image_domain(regular_img_url),
                            "original_url": original_img_url,
                            "stats": {
                                "bookmarks": selected.get("bookmarkCount", 0),
                                "likes": selected.get("likeCount", 0),
                                "views": selected.get("viewCount", 0)
                            },
                            "strategy_used": strategy["name"]  # 记录使用的策略
                        }
        
        except Exception as e:
            logger.warning(f"策略[{strategy['name']}]执行失败: {str(e)}")
            if strategy_idx == len(search_strategies) - 1:  # 最后一个策略也失败
                raise Exception(f"所有搜索策略均失败: {str(e)}")
            continue
    
    raise Exception("经过三阶段搜索仍未找到有效作品，请尝试调整标签组合")

def replace_image_domain(url: str) -> str:
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

# ====== 原图专用处理函数 ======
async def get_remote_file_size(url: str) -> int:
    """获取远程文件大小，避免下载大文件"""
    try:
        proxy = PROXY if USE_PROXY else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.pixiv.net/"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.head(
                url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status in (200, 206):
                    content_range = response.headers.get('Content-Range', '')
                    if content_range:
                        # 从Content-Range中提取文件大小：bytes 0-0/12345678
                        return int(content_range.split('/')[-1])
                    
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        return int(content_length)
                    else:
                        # 尝试GET请求前1KB
                        headers['Range'] = 'bytes=0-1023'
                        async with session.get(
                            url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10)
                        ) as response:
                            if response.status in (200, 206):
                                content_length = response.headers.get('Content-Length')
                                if content_length:
                                    # 估算完整文件大小（1024字节是头部，总大小通常大于头部）
                                    estimated_size = int(content_length)
                                    return estimated_size * 10  # 粗略估计
                
                return 0
    except Exception as e:
        logger.warning(f"获取文件大小失败: {str(e)}")
        return 0

# ====== 添加图片压缩功能 ======
try:
    from PIL import Image
    import io
    PILLLOW_AVAILABLE = True
except ImportError:
    PILLLOW_AVAILABLE = False

async def compress_image(file_path: Path, max_size: int = 10 * 1024 * 1024) -> Path:
    """压缩图片，确保不超过指定大小（10MB）"""
    if not PILLLOW_AVAILABLE:
        logger.warning("Pillow库未安装，无法压缩图片")
        return None
    try:
        # 读取图片
        with Image.open(file_path) as img:
            # 获取原始尺寸
            width, height = img.size
            # 如果图片已经小于10MB，直接返回
            if file_path.stat().st_size <= max_size:
                return file_path
            # 尝试压缩图片
            quality = 95
            while quality > 50 and file_path.stat().st_size > max_size:
                # 保存压缩后的图片
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=quality, optimize=True)
                buffer.seek(0)
                compressed_size = buffer.tell()
                # 如果压缩后的大小符合要求，保存并返回
                if compressed_size <= max_size:
                    new_file_path = file_path.with_suffix('.jpg')
                    with open(new_file_path, 'wb') as f:
                        f.write(buffer.read())
                    return new_file_path
                quality -= 5
            # 如果压缩到最低质量仍然太大，使用预览图
            return None
    except Exception as e:
        logger.error(f"图片压缩失败: {str(e)}")
        return None

async def download_original_image(url: str) -> Path:
    """安全下载大文件到临时位置，返回文件路径（确保不超过10MB）"""
    file_size = await get_remote_file_size(url)

    # 生成唯一文件名
    timestamp = int(time.time() * 1000)
    random_str = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
    parsed_url = urllib.parse.urlparse(url)
    ext = os.path.splitext(parsed_url.path)[1] or '.jpg'

    # 确保文件扩展名兼容
    ext = ext.lower()
    if ext in ['.webp', '.avif', '.heic']:
        ext = '.jpg'
    elif ext == '.svg':
        ext = '.png'

    filename = f"pixiv_{timestamp}_{random_str}{ext}"
    temp_path = TEMP_DIR / filename

    logger.info(f"开始下载原图到: {temp_path} (预估大小: {file_size/1024/1024:.2f}MB)")

    proxy = PROXY if USE_PROXY else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.pixiv.net/"
    }

    # 创建SSL上下文（避免SSL验证问题）
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    # 重试机制
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT), ssl=ssl_context
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"下载失败，状态码: {response.status}, 响应: {error_text[:200]}")

                    # 分块写入文件，避免内存溢出
                    total_bytes = 0
                    start_time = time.time()
                    async with aiofiles.open(temp_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(MAX_DOWNLOAD_CHUNK):
                            await f.write(chunk)
                            total_bytes += len(chunk)

                    # 验证文件完整性
                    downloaded_size = temp_path.stat().st_size
                    if file_size > 0 and downloaded_size < file_size * 0.9:
                        raise Exception(f"文件不完整: 期望 {file_size} 字节, 实际 {downloaded_size} 字节")

                    # 验证图片有效性（需要Pillow）
                    try:
                        if PILLLOW_AVAILABLE:
                            with Image.open(temp_path) as img:
                                img.verify()  # 验证是否为有效的图片格式
                    except ImportError:
                        logger.warning("未安装Pillow库，跳过图片验证。建议安装: pip install Pillow")
                    except Exception as e:
                        logger.warning(f"图片验证失败，尝试修复: {str(e)}")
                        # 尝试修复：重命名扩展名
                        if not str(temp_path).endswith(('.jpg', '.jpeg', '.png')):
                            new_path = temp_path.with_suffix('.jpg')
                            temp_path.rename(new_path)
                            temp_path = new_path

                    # 检查文件大小并压缩（如果需要）
                    if downloaded_size > 10 * 1024 * 1024:  # 超过10MB
                        logger.warning(f"⚠️ 图片过大 ({downloaded_size/1024/1024:.1f}MB)，尝试压缩...")
                        compressed_path = await compress_image(temp_path)
                        if compressed_path:
                            temp_path = compressed_path
                            logger.info(f"✅ 图片已压缩至 {temp_path.stat().st_size/1024/1024:.2f}MB")
                        else:
                            logger.warning("⚠️ 图片压缩失败，将使用预览图")
                            return None  # 返回None表示需要使用预览图

                    logger.info(f"✅ 原图下载成功: {downloaded_size/1024/1024:.2f}MB, 耗时: {time.time()-start_time:.1f}s")
                    return temp_path
        except Exception as e:
            logger.error(f"下载尝试 {attempt+1}/{MAX_ATTEMPTS} 失败: {str(e)}")
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(2)
    
    return temp_path  # 如果没有返回，返回临时路径

async def cleanup_temp_files():
    """清理12小时以上的临时文件"""
    try:
        now = time.time()
        for file_path in TEMP_DIR.glob("*"):
            if file_path.is_file():
                file_age = now - file_path.stat().st_mtime
                if file_age > 12 * 3600:  # 12小时
                    try:
                        file_path.unlink()
                        logger.debug(f"清理旧临时文件: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"清理文件失败 {file_path.name}: {str(e)}")
    except Exception as e:
        logger.warning(f"清理临时文件时出错: {str(e)}")
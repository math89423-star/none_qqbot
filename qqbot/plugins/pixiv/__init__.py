import aiohttp
import json
import urllib.parse
import random
import time
import os
import tempfile
import aiofiles
from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import MessageSegment, Bot, Event
from typing import Dict, Any
import asyncio
import ssl
import traceback
from pathlib import Path

# ====== 重要配置（必须修改） ======
PROXY = "http://127.0.0.1:7890"  # 本地代理地址
USE_PROXY = True

PROXY_URL = "https://quiet-hill-31f3.math89423.workers.dev/"  # Cloudflare Workers地址

PIXIV_COOKIE = "PHPSESSID=14916444_EuNtNE3Yd2ZZ50A7UzivUlxP7O2hLP7s; device_token=ccd49454e972c3b547f1db56a3560575; p_ab_id=1; p_ab_id_2=1"  # ← 必须修改！

# 原图发送专用配置
MAX_DOWNLOAD_CHUNK = 8192  # 8KB分块下载
DOWNLOAD_TIMEOUT = 60  # 60秒超时
MAX_ATTEMPTS = 2  # 重试次数
TEMP_DIR = tempfile.gettempdir()  # 系统临时目录

# 创建专用临时目录
os.makedirs(os.path.join(TEMP_DIR, "pixiv_bot"), exist_ok=True)

# ====== 核心函数 ======
async def search_pixiv_by_tag(tags: list, max_results=10) -> dict:
    """
    通过角色标签搜索Pixiv图片（优化重复率，添加R-18过滤，按热度排序且限制近一周）
    """
    search_tag = " ".join(tags)
    encoded_tag = urllib.parse.quote(search_tag)
    
    # ===== 关键修改1：检查是否明确请求R-18内容 =====
    is_explicit_r18_request = any(tag.lower() in ["r-18", "r18", "r-18g", "r18g"] for tag in tags)
    
    # ===== 关键修改2：设置安全模式参数 =====
    search_mode = "all" if is_explicit_r18_request else "safe"
    
    # ===== 关键优化：按热度排序 + 近一周时间范围 =====
    # 计算近一周的日期范围 (格式: YYYY-MM-DD)
    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    # 随机偏移起始位置
    offset = random.randint(10, 50)  # 热门作品集中在前几页，减少偏移量
    page = offset // 60 + 1
    start_index = offset % 60
    
    url = f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}"
    params = {
        "word": search_tag,
        "order": "popular_d",  # 改为按热度降序排列
        "mode": search_mode,   # 使用安全模式参数
        "p": page,
        "s_mode": "s_tag",
        "type": "all",
        "lang": "zh",
        "scd": start_date,     # 开始日期 (近一周)
        "ecd": end_date,       # 结束日期 (今天)
        "blt": "200"           # 最低收藏数 (过滤低质量作品)
    }
    
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
    
    try:
        proxy = PROXY if USE_PROXY else None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, proxy=proxy, timeout=30) as response:
                if response.status != 200:
                    error_text = await response.text()
                    try:
                        error_json = json.loads(error_text)
                        error_msg = error_json.get("error", {}).get("message", error_text[:200])
                    except:
                        error_msg = error_text[:200]
                    raise Exception(f"搜索API失败，状态码: {response.status}, 详情: {error_msg}")
                
                data = await response.json()
                
                if not data.get("body") or not data["body"].get("illustManga"):
                    raise Exception("API返回空数据，可能标签无效或Cookie失效")
                
                # 获取整页作品（60张）
                all_results = [
                    item for item in data["body"]["illustManga"]["data"] 
                    if item and isinstance(item, dict) and "id" in item and item.get("isAdContainer", 0) == 0
                ]
                
                # ===== R-18内容过滤 =====
                filtered_results = []
                for item in all_results:
                    # 获取作品标签（安全访问）
                    tags_info = item.get("tags", [])
                    if isinstance(tags_info, dict):
                        tags_info = tags_info.get("tags", [])
                    
                    # 提取标签名称
                    tag_names = [tag.get("tag", "").lower() for tag in tags_info if isinstance(tag, dict)]
                    
                    # 检查R-18/R-18G标签
                    is_r18 = any("r-18" in tag or "r18" in tag for tag in tag_names)
                    is_r18g = any("r-18g" in tag or "r18g" in tag for tag in tag_names)
                    
                    # 仅保留符合条件的作品
                    if is_explicit_r18_request or (not is_r18 and not is_r18g):
                        filtered_results.append(item)
                
                # 结果不足时的处理
                if not filtered_results:
                    if not is_explicit_r18_request:
                        raise Exception("未找到适合的内容。如果您想搜索成人内容，请在标签中包含'R-18'或'R-18G'")
                    else:
                        raise Exception("未找到匹配的作品，请尝试其他标签或检查Cookie是否有效")
                
                # 选择候选作品（只取前30个高质量作品）
                candidates = filtered_results[:30]
                
                if not candidates:
                    raise Exception("未找到有效作品，请尝试其他标签或检查Cookie是否有效")
                
                # ===== 优化：根据综合质量评分加权选择 =====
                weighted_candidates = []
                for item in candidates:
                    # 获取作品质量指标
                    bookmark_count = item.get("bookmarkCount", 0)  # 收藏数
                    like_count = item.get("likeCount", 0)          # 点赞数
                    view_count = item.get("viewCount", 0)          # 浏览数
                    
                    # 计算综合质量分数 (收藏权重最高，其次是点赞)
                    quality_score = (
                        bookmark_count * 10 + 
                        like_count * 5 + 
                        view_count * 0.1
                    )
                    
                    # 保证至少1权重
                    weight = max(1, min(100, int(quality_score ** 0.5)))  # 开方平滑
                    weighted_candidates.extend([item] * weight)
                
                # 随机选择（高分作品概率更高）
                selected = random.choice(weighted_candidates)
                illust_id = selected["id"]
                
                # 2. 获取作品详情
                illust_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
                illust_headers = {
                    **headers,
                    "Referer": f"https://www.pixiv.net/artworks/{illust_id}"
                }
                
                async with session.get(illust_url, headers=illust_headers, proxy=proxy, timeout=30) as illust_response:
                    if illust_response.status != 200:
                        error_text = await illust_response.text()
                        raise Exception(f"获取作品详情失败，状态码: {illust_response.status}, 响应: {error_text[:200]}")
                    
                    illust_data = await illust_response.json()
                    if illust_data.get("error"):
                        raise Exception(f"作品详情API错误: {illust_data['message']}")
                    
                    illust_body = illust_data["body"]
                    
                    # 再次检查R-18标签
                    work_tags = [tag.get("tag", "") for tag in illust_body.get("tags", {}).get("tags", [])]
                    is_work_r18 = any(tag.lower() in ["r-18", "r18"] for tag in work_tags)
                    is_work_r18g = any(tag.lower() in ["r-18g", "r18g"] for tag in work_tags)
                    
                    # 如果不是明确请求R-18且作品包含R-18标签，重新搜索
                    if not is_explicit_r18_request and (is_work_r18 or is_work_r18g):
                        logger.warning(f"检测到R-18内容但未明确请求，跳过作品ID: {illust_id}")
                        # 为避免无限递归，不在此处递归，而是抛出异常让用户重试
                        raise Exception("检测到不适当内容，已跳过。请尝试其他标签。")
                    
                    original_img_url = illust_body["urls"]["original"]
                    regular_img_url = illust_body["urls"]["regular"]
                    
                    # 构建代理后的图片URL
                    proxy_original_url = replace_image_domain(original_img_url)
                    proxy_preview_url = replace_image_domain(regular_img_url)
                    
                    return {
                        "image_url": proxy_original_url,
                        "pid": str(illust_id),
                        "title": illust_body["title"],
                        "author": illust_body["userName"],
                        "author_id": illust_body["userId"],
                        "work_url": f"https://www.pixiv.net/artworks/{illust_id}",
                        "preview_url": proxy_preview_url,
                        "original_url": original_img_url,
                        "stats": {
                            "bookmarks": bookmark_count,
                            "likes": like_count,
                            "views": view_count
                        }
                    }
                
    except Exception as e:
        raise Exception(f"搜索失败: {str(e)}")

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
                url, 
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10)
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
                        url,
                        headers=headers,
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=10)
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

async def download_original_image(url: str) -> str:
    """安全下载大文件到临时位置，返回文件路径"""
    file_size = await get_remote_file_size(url)
    if file_size > 50 * 1024 * 1024:  # 超过50MB警告
        logger.warning(f"⚠️ 检测到超大文件 ({file_size/1024/1024:.1f}MB)，可能发送失败")
    
    # 生成唯一文件名
    timestamp = int(time.time() * 1000)
    random_str = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or '.jpg'
    
    # 确保文件扩展名兼容
    if ext.lower() in ['.webp', '.avif', '.heic']:
        ext = '.jpg'
    elif ext.lower() == '.svg':
        ext = '.png'
    
    filename = f"pixiv_{timestamp}_{random_str}{ext}"
    temp_path = os.path.join(TEMP_DIR, "pixiv_bot", filename)
    
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
                    url,
                    headers=headers,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
                    ssl=ssl_context
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
                            # 每10MB记录一次进度
                            if total_bytes % (10 * 1024 * 1024) == 0:
                                elapsed = time.time() - start_time
                                speed = total_bytes / elapsed / 1024 / 1024  # MB/s
                                logger.info(f"下载进度: {total_bytes/1024/1024:.1f}MB, 速度: {speed:.2f}MB/s")
                    
                    # 验证文件完整性
                    downloaded_size = os.path.getsize(temp_path)
                    if file_size > 0 and downloaded_size < file_size * 0.9:  # 允许10%误差
                        raise Exception(f"文件不完整: 期望 {file_size} 字节, 实际 {downloaded_size} 字节")
                    
                    # 验证图片有效性（需要Pillow）
                    try:
                        from PIL import Image
                        with Image.open(temp_path) as img:
                            img.verify()  # 验证是否为有效的图片格式
                    except ImportError:
                        logger.warning("未安装Pillow库，跳过图片验证。建议安装: pip install Pillow")
                    except Exception as e:
                        logger.warning(f"图片验证失败，尝试修复: {str(e)}")
                        # 尝试修复：重命名扩展名
                        if not temp_path.endswith(('.jpg', '.jpeg', '.png')):
                            new_path = temp_path.rsplit('.', 1)[0] + '.jpg'
                            os.rename(temp_path, new_path)
                            temp_path = new_path
                    
                    logger.info(f"✅ 原图下载成功: {downloaded_size/1024/1024:.2f}MB, 耗时: {time.time()-start_time:.1f}s")
                    return temp_path
                    
        except Exception as e:
            logger.error(f"下载尝试 {attempt+1}/{MAX_ATTEMPTS} 失败: {str(e)}")
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(2)  # 重试前等待
    
    raise Exception("所有下载尝试均失败")

async def cleanup_temp_files():
    """清理24小时以上的临时文件"""
    try:
        now = time.time()
        temp_dir = os.path.join(TEMP_DIR, "pixiv_bot")
        
        for filename in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, filename)
            if os.path.isfile(file_path):
                file_age = now - os.path.getmtime(file_path)
                if file_age > 24 * 3600:  # 24小时
                    try:
                        os.remove(file_path)
                        logger.debug(f"清理旧临时文件: {filename}")
                    except Exception as e:
                        logger.warning(f"清理文件失败 {filename}: {str(e)}")
    except Exception as e:
        logger.warning(f"清理临时文件时出错: {str(e)}")

# ====== Nonebot2插件逻辑 ======
pixiv_cmd = on_command("pixiv", aliases={"p"}, priority=5, block=True)

@pixiv_cmd.handle()
async def handle_pixiv_command(bot: Bot, event: Event):
    """处理 /pixiv 命令 - 原图优先模式"""
    raw_message = str(event.get_message()).strip()
    command_length = len("/pixiv")
    args = raw_message[command_length:].strip()
    
    if not args:
        await bot.send(event, "请提供搜索标签，例如：\n/pixiv 鸣潮\n/p 鸣潮")
        return
    
    tags = [tag.strip() for tag in args.split() if tag.strip()]
    logger.info(f"Pixiv搜索请求: {tags}")
    
    try:
        # 1. 搜索作品
        result = await search_pixiv_by_tag(tags)
        
        # 2. 构建消息内容
        msg_content = (
            f"🎨 作品标题: {result['title']}\n"
            f"👤 作者: {result['author']} (ID: {result['author_id']})\n"
            f"🆔 作品ID: {result['pid']}\n"
            f"🔗 作品链接: {result['work_url']}\n\n"
            f"⏳ 正在下载原图 (可能需要较长时间)..."
        )
        
        # 发送初步信息
        await bot.send(event, msg_content)
        
        # 3. 安全下载原图
        try:
            # 清理旧临时文件
            await cleanup_temp_files()
            
            # 下载原图
            temp_path = await download_original_image(result['image_url'])
            
            # 4. 构建正确的CQ码 - 使用file协议
            # 确保路径格式正确（Windows需要三个斜杠，Linux/Mac需要两个）
            if os.name == 'nt':  # Windows
                file_url = f"file:///{temp_path.replace(os.sep, '/')}"
            else:  # Linux/Mac
                file_url = f"file://{temp_path}"
            
            # 5. 发送原图 - 使用MessageSegment确保正确解析
            start_time = time.time()
            await bot.send(event, MessageSegment.image(file_url))
            logger.info(f"✅ 原图发送成功! 耗时: {time.time()-start_time:.1f}s")
            
            # 6. 异步清理文件（不阻塞响应）
            async def delayed_cleanup():
                await asyncio.sleep(30)  # 等待30秒确保发送完成
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                        logger.debug(f"已清理临时文件: {temp_path}")
                except Exception as e:
                    logger.warning(f"清理文件失败 {temp_path}: {str(e)}")
            
            # 创建后台任务
            asyncio.create_task(delayed_cleanup())
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"原图发送失败: {error_msg}\n{traceback.format_exc()}")
            
            # 降级方案：发送预览图 + 原图链接
            fallback_msg = (
                f"⚠️ 原图发送失败（可能文件过大），已自动降级\n"
                f"🔗 原图下载: {result['image_url']}\n\n"
                f"🖼️ 当前显示预览图（点击链接下载原图）:"
            )
            await bot.send(event, fallback_msg)
            
            # 发送预览图
            preview_data = await download_and_process_preview(result['preview_url'])
            await bot.send(event, MessageSegment.image(preview_data))
    
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Pixiv搜索失败: {error_msg}\n{traceback.format_exc()}")
        
        # 优化错误提示
        if "Cookie" in error_msg or "cookie" in error_msg.lower():
            error_msg = (
                "⚠️ Cookie无效！请重新获取Pixiv Cookie:\n"
                "1. 登录 https://www.pixiv.net\n"
                "2. 按 F12 打开开发者工具\n"
                "3. 进入 Application → Storage → Cookies\n"
                "4. 复制整个 Cookie 内容替换代码中的 PIXIV_COOKIE"
            )
        elif "代理" in error_msg or "proxy" in error_msg.lower() or "Proxy" in error_msg:
            error_msg = (
                "⚠️ 代理配置问题！请检查:\n"
                f"- 本地代理: {PROXY}\n"
                f"- Cloudflare 代理: {PROXY_URL}\n"
                "- 确保代理软件正常运行"
            )
        elif "timeout" in error_msg.lower() or "超时" in error_msg:
            error_msg = (
                "⚠️ 请求超时！可能是网络不稳定或代理延迟过高\n"
                "建议:\n"
                "1. 检查Clash代理是否正常运行\n"
                "2. 尝试更换标签\n"
                "3. 检查Cloudflare Workers是否可用"
            )
        elif "memory access out of bounds" in error_msg or "内存" in error_msg:
            error_msg = (
                "⚠️ 内存溢出！原图过大导致\n"
                "已自动降级发送预览图\n"
                "您也可以通过作品链接下载原图"
            )
        elif "404" in error_msg or "403" in error_msg:
            error_msg = (
                "⚠️ 无法访问图片资源\n"
                "可能是代理配置有误或Pixiv限制\n"
                f"原始URL: {result.get('original_url', '未知') if 'result' in locals() else '未知'}"
            )
        else:
            error_msg = f"发生未知错误: {error_msg}"
        
        await bot.send(event, f"❌ 搜索失败: {error_msg}")

# ====== 预览图处理函数（降级用） ======
async def download_and_process_preview(image_url: str) -> bytes:
    """下载并处理预览图（小尺寸）"""
    try:
        proxy = PROXY if USE_PROXY else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.pixiv.net/"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                image_url,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    raise Exception(f"预览图下载失败，状态码: {response.status}")
                
                return await response.read()
                
    except Exception as e:
        logger.error(f"预览图处理失败: {str(e)}")
        raise Exception(f"预览图处理失败: {str(e)}")
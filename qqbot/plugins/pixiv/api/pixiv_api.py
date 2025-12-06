import urllib.parse
import logging
import aiohttp
import aiofiles
import io
import time
import random
import os
import ssl
import asyncio
from http import HTTPStatus
from pathlib import Path
from PIL import Image
from ..utils.pixiv_utils import (
    _is_r18_request,
    _is_r18_content,
    _extract_tag_names,
    _build_search_strategies,
    _execute_search_strategy,
    _select_best_image,
    _validate_and_build_response
)
from ..config.config import (
    PROXY,
    USE_PROXY, 
    MAX_ATTEMPTS,
    MAX_DOWNLOAD_CHUNK, 
    DOWNLOAD_TIMEOUT
    )
# 基础项目目录
BASE_DIR = Path(__file__).parent.parent.parent.absolute()
DATA_DIR = BASE_DIR / "data"
TEMP_DIR = DATA_DIR / "pixiv_temp"  # 专用临时目录

# 创建目录
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 近期图片缓存排除机制
RECENT_IMAGES = {}

# 创建日志
logger = logging.getLogger()
logging.basicConfig(level = logging.INFO,format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

async def search_pixiv_by_tag(tags: list, max_results=10) -> dict:
    """通过角色标签搜索Pixiv图片（智能适应新角色/冷门角色）"""
    # 1. 预处理标签和搜索模式
    search_tag = " ".join(tags)
    logger.info(f"搜索标签：{search_tag}")
    encoded_tag = urllib.parse.quote(search_tag)
    is_explicit_r18_request = _is_r18_request(tags)
    # 2. 三阶段策略配置
    strategies = _build_search_strategies()
    # 3. 三阶段搜索重试
    for strategy in strategies:
        for attempt in range(5):  # 每个策略最多尝试5次
            try:
                # 4. 执行策略搜索
                results = await _execute_search_strategy(
                    search_tag, encoded_tag, strategy
                )
                filtered_results = [r for r in results if not _is_r18_content(_extract_tag_names(r)) or is_explicit_r18_request]
                if not filtered_results and not is_explicit_r18_request:
                    # 非R-18请求但全是R-18内容，调整策略参数
                    logger.info(f"策略[{strategy['name']}]全是R-18内容，调整参数重试")
                    strategy["params"]["mode"] = "safe"  # 添加安全模式参数
                    continue  # 重试当前策略
                # 5. 处理结果并选择作品
                selected = _select_best_image(results, is_explicit_r18_request)
                # 6. 获取作品详情并验证
                return await _validate_and_build_response(
                    selected, is_explicit_r18_request, encoded_tag
                )
            except Exception as e:
                logger.warning(f"策略[{strategy['name']}]尝试#{attempt+1}失败: {str(e)}")
                # 如果是最后一次尝试或最后一个策略且第二次尝试后仍失败，则退出当前策略循环
                if attempt == 4 or (strategy is strategies[-1] and attempt >= 1):
                    break
        # 如果是因为内部循环正常结束（未触发break），则继续下一个策略
        else:
            continue
        # 因为break而退出，不再继续尝试其他策略
        break
    raise Exception("所有搜索策略均失败或搜索均命中限制级内容请重试")

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

async def compress_image(file_path: Path, max_size: int = 10 * 1024 * 1024) -> Path:
    """智能压缩图片，最大化利用10MB上限保持质量"""
    try:
        original_size = file_path.stat().st_size
        if original_size <= max_size:
            return file_path
        logger.warning(f"⚠️ 图片过大 ({original_size/1024/1024:.2f}MB)，开始智能压缩...")
        with Image.open(file_path) as img:
            # 1. 预处理：转换为RGB
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            orig_width, orig_height = img.size
            target_size_range = (max_size * 0.9, max_size * 0.98)  # 目标范围: 9-9.8MB
            # 2. 阶段1: 尺寸优化 - 找到最佳尺寸
            optimal_img = await _find_optimal_size(img, orig_width, orig_height, target_size_range)
            # 3. 阶段2: 质量微调 - 在最佳尺寸基础上调整质量
            result = await _fine_tune_quality(optimal_img, target_size_range)
            # 4. 保存最终结果
            if result:
                compressed_img, best_quality, compressed_size = result
                new_file_path = file_path.with_name(f"{file_path.stem}_compressed.jpg")
                with open(new_file_path, 'wb') as f:
                    f.write(compressed_img.getvalue())
                logger.info(
                    f"✅ 压缩成功: {original_size/1024/1024:.2f}MB → "
                    f"{compressed_size/1024/1024:.2f}MB "
                    f"(质量: {best_quality}%, 尺寸: {optimal_img.size[0]}x{optimal_img.size[1]})"
                )
                return new_file_path
            logger.warning("⚠️ 智能压缩未达目标，使用预览图替代")
            return None
    except Exception as e:
        logger.error(f"图片压缩失败: {str(e)}", exc_info=True)
        return None

async def _find_optimal_size(img, orig_width, orig_height, target_size_range):
    """找到最佳尺寸，使95%质量的JPEG接近目标大小范围"""
    min_size, max_size = target_size_range
    current_img = img.copy()
    # 1. 先测试原始尺寸
    buffer = io.BytesIO()
    current_img.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True)
    current_size = buffer.tell()
    # 2. 如果原始尺寸在目标范围内，直接返回
    if min_size <= current_size <= max_size:
        logger.info(f"🎯 原始尺寸完美匹配目标: {current_size/1024/1024:.2f}MB")
        return current_img
    # 3. 如果原始尺寸太大，缩小
    if current_size > max_size:
        scale = 0.9  # 缩小比例
        while current_size > max_size and scale > 0.5:
            new_width = int(orig_width * scale)
            new_height = int(orig_height * scale)
            resized_img = img.resize((new_width, new_height), Image.LANCZOS)
            buffer = io.BytesIO()
            resized_img.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True)
            current_size = buffer.tell()
            logger.debug(f"🔍 尺寸测试: {new_width}x{new_height} → {current_size/1024/1024:.2f}MB")
            if min_size <= current_size <= max_size:
                logger.info(f"🎯 找到完美尺寸: {new_width}x{new_height} ({current_size/1024/1024:.2f}MB)")
                return resized_img
            scale -= 0.05
        logger.info(f"📏 尺寸缩小至: {current_img.size[0]}x{current_img.size[1]} ({current_size/1024/1024:.2f}MB)")
        return current_img
    # 4. 如果原始尺寸太小，尝试增大（仅当原始尺寸小于目标时）
    if current_size < min_size and orig_width < 4096 and orig_height < 4096:
        scale = 1.1  # 增大比例
        best_img = current_img.copy()
        best_size = current_size
        while current_size < max_size and scale <= 1.5:
            new_width = min(int(orig_width * scale), 4096)
            new_height = min(int(orig_height * scale), 4096)
            resized_img = img.resize((new_width, new_height), Image.LANCZOS)
            buffer = io.BytesIO()
            resized_img.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True)
            current_size = buffer.tell()
            logger.debug(f"🔍 尺寸放大测试: {new_width}x{new_height} → {current_size/1024/1024:.2f}MB")
            if current_size <= max_size:
                best_img = resized_img
                best_size = current_size
            if min_size <= current_size <= max_size:
                logger.info(f"🎯 找到完美放大尺寸: {new_width}x{new_height} ({current_size/1024/1024:.2f}MB)")
                return resized_img
            scale += 0.1
        if best_size > current_size:  # 如果有改进
            logger.info(f"📈 尺寸优化至: {best_img.size[0]}x{best_img.size[1]} ({best_size/1024/1024:.2f}MB)")
            return best_img
    return current_img

async def _fine_tune_quality(img, target_size_range):
    """在最佳尺寸基础上微调质量，精确匹配目标大小"""
    min_size, max_size = target_size_range
    # 1. 先测试95%质量
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True)
    current_size = buffer.tell()
    # 2. 如果已经接近目标，直接返回
    if min_size <= current_size <= max_size:
        return buffer, 95, current_size
    # 3. 如果太大，降低质量
    if current_size > max_size:
        low, high = 70, 95
        best_quality = 90
        best_buffer = None
        for _ in range(8):
            mid = (low + high) // 2
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=mid, optimize=True, progressive=True)
            size = buffer.tell()
            logger.debug(f"🔍 质量微调: {mid}% → {size/1024/1024:.2f}MB")
            if size <= max_size:
                best_quality = mid
                best_buffer = buffer
                low = mid + 1
            else:
                high = mid - 1
        if best_buffer and best_buffer.tell() >= min_size:
            return best_buffer, best_quality, best_buffer.tell()
    # 4. 如果太小，尝试添加元数据增加文件大小（无损）
    elif current_size < min_size:
        # 添加EXIF元数据（无损增加文件大小）
        exif_data = b" " * int(min_size - current_size)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True, exif=exif_data)
        if buffer.tell() <= max_size:
            logger.info(f"🏷️ 通过EXIF元数据优化文件大小: {current_size/1024/1024:.2f}MB → {buffer.tell()/1024/1024:.2f}MB")
            return buffer, 95, buffer.tell()
    # 5. 返回最接近的结果
    return buffer, 95, current_size

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
            async with aiohttp.ClientSession() as session, \
                session.get(
                    url, headers=headers, proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
                    ssl=ssl_context
                ) as response:
                    if response.status != HTTPStatus.OK:
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
                    # 使用Pillow验证图片
                    try:
                        with Image.open(temp_path) as img:
                            img.verify()  # 验证是否为有效的图片格式
                    except Exception as e:
                        logger.warning(f"图片验证失败，尝试修复: {str(e)}")
                        # 尝试修复：重命名扩展名
                        if not str(temp_path).lower().endswith(('.jpg', '.jpeg', '.png')):
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
    # 如果没有返回，返回临时路径
    return temp_path

async def cleanup_temp_files():
    """清理6小时以上的临时文件"""
    try:
        now = time.time()
        for file_path in TEMP_DIR.glob("*"):
            if file_path.is_file():
                file_age = now - file_path.stat().st_mtime
                if file_age > 6 * 3600:  # 6小时
                    try:
                        file_path.unlink()
                        logger.debug(f"清理旧临时文件: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"清理文件失败 {file_path.name}: {str(e)}")
    except Exception as e:
        logger.warning(f"清理临时文件时出错: {str(e)}")

async def download_and_process_preview(image_url: str) -> bytes:
    """下载并处理预览图（小尺寸）"""
    try:
        proxy = PROXY if USE_PROXY else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.pixiv.net/"
        }
        async with aiohttp.ClientSession() as session,\
            session.get(
                image_url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    raise Exception(f"预览图下载失败，状态码: {response.status}")
                return await response.read()
    except Exception as e:
        logger.error(f"预览图处理失败: {str(e)}")
        raise Exception(f"预览图处理失败: {str(e)}")
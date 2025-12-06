import urllib.parse
import logging
from pathlib import Path
from ..utils.pixiv_utils import (
    _is_r18_request, 
    _build_search_strategies
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
    search_mode = "all" if is_explicit_r18_request else "safe"
    # 2. 三阶段策略配置
    strategies = _build_search_strategies()
    # 3. 三阶段搜索重试
    for strategy in strategies:
        try:
            # 4. 执行单次策略搜索
            results = await _execute_search_strategy(
                search_tag, encoded_tag, strategy, search_mode
            )
            # 5. 处理结果并选择作品
            selected = _select_best_image(results, is_explicit_r18_request)
            # 6. 获取作品详情并验证
            return await _validate_and_build_response(selected, is_explicit_r18_request, encoded_tag)
        except Exception as e:
            logger.warning(f"策略[{strategy['name']}]失败: {str(e)}")
            if strategy is strategies[-1]:
                raise Exception(f"所有搜索策略均失败: {str(e)}")
    raise Exception("三阶段搜索全部失败")
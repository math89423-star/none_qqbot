class PixivAPIError(Exception):
    """Pixiv API 异常"""
    def __init__(
            self,
            error_type: str,
            strategy_name: str,
            details: dict = None
    ) -> None:
        self.error_type = error_type
        self.strategy_name = strategy_name
        self.details = details or {}

        # 扩展错误类型处理
        if error_type == "api_failure":
            msg = f"Pixiv API 请求失败 [策略: {strategy_name}, 状态码: {details.get('status')}]"
        elif error_type == "empty_data":
            msg = f"Pixiv API 返回空数据 [策略: {strategy_name}]"
        else:
            msg = f"Pixiv API 错误 [策略: {strategy_name}]"

        super().__init__(msg)

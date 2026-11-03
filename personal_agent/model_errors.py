"""两种 SDK 的请求错误共用现有回滚、脱敏与部分报告处理。"""

from anthropic import APIError as AnthropicAPIError
from openai import APIError as OpenAIAPIError


MODEL_API_ERRORS = (OpenAIAPIError, AnthropicAPIError)

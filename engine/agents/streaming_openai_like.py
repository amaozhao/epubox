from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type, Union

from pydantic import BaseModel

from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse


@dataclass
class StreamingOpenAILike(OpenAILike):
    """
    针对“必须 stream=true”的 OpenAI 兼容代理做适配。

    - 对外仍暴露 invoke/ainvoke 的完整响应语义
    - 对内强制走 streaming 接口并聚合 delta
    """

    name: str = "StreamingOpenAILike"

    @staticmethod
    def _merge_stream_delta(aggregated: ModelResponse, delta: ModelResponse) -> None:
        if delta.role is not None and aggregated.role is None:
            aggregated.role = delta.role

        if delta.content is not None:
            aggregated.content = (aggregated.content or "") + str(delta.content)

        if delta.reasoning_content is not None:
            aggregated.reasoning_content = (aggregated.reasoning_content or "") + str(delta.reasoning_content)

        if delta.redacted_reasoning_content is not None:
            aggregated.redacted_reasoning_content = (aggregated.redacted_reasoning_content or "") + str(
                delta.redacted_reasoning_content
            )

        if delta.audio is not None:
            aggregated.audio = delta.audio
        if delta.images is not None:
            aggregated.images = delta.images
        if delta.videos is not None:
            aggregated.videos = delta.videos
        if delta.audios is not None:
            aggregated.audios = delta.audios
        if delta.files is not None:
            aggregated.files = delta.files
        if delta.citations is not None:
            aggregated.citations = delta.citations
        if delta.response_usage is not None:
            aggregated.response_usage = delta.response_usage
        if delta.extra is not None:
            if aggregated.extra is None:
                aggregated.extra = {}
            aggregated.extra.update(delta.extra)
        if delta.provider_data is not None:
            if aggregated.provider_data is None:
                aggregated.provider_data = {}
            aggregated.provider_data.update(delta.provider_data)

        if delta.tool_calls:
            aggregated.tool_calls.extend(delta.tool_calls)
        if delta.tool_executions:
            aggregated.tool_executions.extend(delta.tool_executions)

    def invoke(
        self,
        messages: List,
        assistant_message: Any,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Any] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        aggregated = ModelResponse()
        for delta in self.invoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            self._merge_stream_delta(aggregated, delta)
        return aggregated

    async def ainvoke(
        self,
        messages: List,
        assistant_message: Any,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Any] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        aggregated = ModelResponse()
        async for delta in self.ainvoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            self._merge_stream_delta(aggregated, delta)
        return aggregated

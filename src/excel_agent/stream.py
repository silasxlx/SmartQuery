"""流式对话 - 使用 LangChain ReAct Agent"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, Mapping, cast

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, AIMessageChunk, ToolMessage
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent  # LangGraph v1 替代 create_react_agent

from .config import get_config
from .excel_loader import get_loader
from .knowledge_base import get_knowledge_base, format_knowledge_context
from .tools import ALL_TOOLS


logger = logging.getLogger("excel_agent")


# Monkey-patch: 让 LangChain ChatOpenAI 支持 reasoning_content（qwen3 thinking 模式）
# LangChain 官方不会支持 reasoning_content（非 OpenAI 标准字段），需要手动 patch
# 需要同时 patch 流式和非流式两个函数
import langchain_openai.chat_models.base as _lc_openai_base

# patch 非流式路径: _convert_dict_to_message -> AIMessage
_original_convert_dict = _lc_openai_base._convert_dict_to_message


def _patched_convert_dict(_dict: Mapping[str, Any]):
    """patched: 提取 reasoning_content 到 AIMessage.additional_kwargs"""
    result = _original_convert_dict(_dict)
    if isinstance(result, AIMessage):
        reasoning = _dict.get("reasoning_content", "") or ""
        if reasoning:
            existing = result.additional_kwargs or {}
            existing["reasoning_content"] = reasoning
            result = AIMessage(
                content=result.content,
                additional_kwargs=existing,
                name=result.name,
                id=result.id,
                tool_calls=result.tool_calls,
                invalid_tool_calls=result.invalid_tool_calls,
            )
    return result


_lc_openai_base._convert_dict_to_message = _patched_convert_dict

# patch 流式路径: _convert_delta_to_message_chunk -> AIMessageChunk
_original_convert_delta = _lc_openai_base._convert_delta_to_message_chunk


def _patched_convert_delta(_dict: Mapping[str, Any], default_class):
    """patched: 提取 reasoning_content 到 AIMessageChunk.additional_kwargs"""
    result = _original_convert_delta(_dict, default_class)
    if isinstance(result, AIMessageChunk):
        reasoning = _dict.get("reasoning_content", "") or ""
        if reasoning:
            existing = result.additional_kwargs or {}
            existing["reasoning_content"] = reasoning
            result = AIMessageChunk(
                content=result.content,
                additional_kwargs=existing,
                id=result.id,
                tool_call_chunks=result.tool_call_chunks,
            )
    return result


_lc_openai_base._convert_delta_to_message_chunk = _patched_convert_delta


class CustomJSONEncoder(json.JSONEncoder):
    """自定义 JSON 编码器，处理 Pandas/Numpy 类型"""
    
    def default(self, obj):
        # 处理 Pandas Timestamp
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        # 处理 numpy 类型
        if hasattr(obj, 'item'):
            return obj.item()
        # 处理 numpy 数组
        if hasattr(obj, 'tolist'):
            return obj.tolist()
        # 处理 pandas NaT
        if str(obj) == 'NaT':
            return None
        # 处理 pandas NA
        if str(obj) == '<NA>':
            return None
        return super().default(obj)


def json_dumps(obj, **kwargs):
    """使用自定义编码器的 JSON 序列化函数"""
    return json.dumps(obj, cls=CustomJSONEncoder, **kwargs)


SYSTEM_PROMPT = """你是一个专业的 Excel 数据分析助手。

## 当前 Excel 信息
{excel_summary}

## 相关知识参考
{knowledge_context}

## 工作原则
1. 根据用户问题，判断是否需要使用工具
2. **每次调用工具前，先输出一段简短的中文思考**（说明为什么要做这一步），再调用工具
3. 工具调用成功后，根据结果回答用户问题
4. **最终回答直接给出结论和分析**，不要描述"我使用了xx工具"或"我进行了xx操作"等内部过程
5. 回答语气友好，使用中文，并给出自己的一些数据分析建议
6. **知识库使用规则**（关键）：
   - 知识库中的内容是**业务背景知识**，不是 Excel 数据中的内容
   - 当用户提到知识库中提到的概念（如活动名称、特殊日期等）时，应**根据知识库信息理解用户意图**，将其映射到对应的日期/分店等筛选条件
   - 例如：知识库说"2024年1月6日是新年焕新日"，用户问"新年焕新日营业额"时，应查询 1月6日的数据，而不是在 Excel 中搜索"新年焕新日"这个文字
   - 不要在 Excel 数据中搜索知识库中的业务术语，而是用知识库信息来构造筛选条件

## 工具选择规则（关键，避免用错工具）
- **涉及筛选条件后的求和/平均/最大/最小/计数/中位数** → 必须按顺序执行：**先 `filter_data` 查看明细，再 `aggregate_data` 聚合统计**（两步都要做，filters 保持一致）。
- **"按某列分组统计"**（如按分店、按月份） → 使用 `group_and_aggregate`，筛选条件放在 `filters` 参数。
- **"仅查看符合条件的数据明细"**（不涉及聚合） → 只使用 `filter_data`。
- **"对已知的纯数学表达式求值"**（如 `(100+200)*0.5`） → 使用 `calculate`。**禁止**用 `calculate` 对一列数据求和/平均/统计。
- **`get_data_preview` 仅用于了解数据结构**，不能用于求和、统计、计算总额。
- 日期/数值范围用 `>=` 和 `<=` 组合表达，不要用 between。

## 正确示例（必须遵守的工具选择模式）

**问题**：1月1日-1月10日营业额是多少？比起1月11日-1月20日怎么样？
**正确做法**（每个时间段先筛选再聚合，共 4 步工具调用）：
1. `filter_data(filters=[{"column":"统计日期","operator":">=","value":"2024-01-01"}, {"column":"统计日期","operator":"<=","value":"2024-01-10"}], select_columns=["统计日期","分店名称","日营业额(元)"], limit=100)`
2. `aggregate_data(column="日营业额(元)", agg_func="sum", filters=[{"column":"统计日期","operator":">=","value":"2024-01-01"}, {"column":"统计日期","operator":"<=","value":"2024-01-10"}])`
3. `filter_data(filters=[{"column":"统计日期","operator":">=","value":"2024-01-11"}, {"column":"统计日期","operator":"<=","value":"2024-01-20"}], select_columns=["统计日期","分店名称","日营业额(元)"], limit=100)`
4. `aggregate_data(column="日营业额(元)", agg_func="sum", filters=[{"column":"统计日期","operator":">=","value":"2024-01-11"}, {"column":"统计日期","operator":"<=","value":"2024-01-20"}])`
5. 如需计算差值/增长率，再用 `calculate` 对两个已知数值做运算

**错误做法**（禁止）：
- 禁止跳过 `filter_data` 直接 `aggregate_data`（即使 aggregate_data 支持 filters 参数）
- 禁止调用 `get_data_preview(50)` 取前 50 行再 `calculate` 手算
- 禁止调用 `filter_data` 后再 `calculate` 手算
- 禁止任何"先取数据再手算"的组合

## 通用规则
- 涉及"求和/平均/统计/最大/最小"等聚合计算 → **先 filter_data 再 aggregate_data**
- 涉及"查看明细/列出符合条件的数据" → 用 filter_data
- 涉及"已知数值的算术运算"（如两个已知的聚合结果相减） → 用 calculate
"""


def get_llm():
    """获取 LLM 实例"""
    config = get_config()
    provider = config.model.get_active_provider()
    return ChatOpenAI(
        model=provider.model_name,
        api_key=provider.api_key,
        base_url=provider.base_url if provider.base_url else None,
        temperature=provider.temperature,
        max_tokens=provider.max_tokens,
        # 启用 qwen3 thinking 模式，返回 reasoning_content 思考过程
        # 仅对 qwen 系列模型生效，其他模型会忽略此参数
        extra_body={"enable_thinking": True},
    )


async def stream_chat(message: str, history: list = None) -> AsyncGenerator[Dict[str, Any], None]:
    """执行对话 - 使用 LangChain ReAct Agent
    
    Args:
        message: 当前用户消息
        history: 历史对话列表，每项为 {"role": "user"|"assistant", "content": "..."}
    """
    loader = get_loader()

    if not loader.is_loaded:
        yield {"type": "error", "content": "请先上传 Excel 文件"}
        return

    try:
        excel_summary = loader.get_summary()
        llm = get_llm()
        
        # 检索相关知识
        knowledge_context = "暂无相关知识参考。"
        kb = get_knowledge_base()
        if kb:
            try:
                stats = kb.get_stats()
                logger.info(
                    "knowledge base status",
                    extra={"event": "knowledge_status", "outcome": "success"},
                )
                relevant_knowledge = kb.search(query=message)
                logger.info(
                    "knowledge retrieved",
                    extra={"event": "knowledge_retrieved", "outcome": "success"},
                )
                if relevant_knowledge:
                    knowledge_context = format_knowledge_context(relevant_knowledge)
                    yield {"type": "thinking", "content": f"找到 {len(relevant_knowledge)} 条相关知识参考..."}
            except Exception:
                logger.exception(
                    "knowledge retrieval failed",
                    extra={
                        "event": "knowledge_retrieval_failed",
                        "outcome": "error",
                        "error_code": "KNOWLEDGE_ERROR",
                    },
                )
        else:
            logger.info(
                "knowledge base disabled",
                extra={"event": "knowledge_disabled", "outcome": "success"},
            )
        
        # 构建系统提示
        # 注意：用 replace 而非 format，避免提示词中 JSON 示例的 {} 被误当成占位符
        system_prompt = SYSTEM_PROMPT.replace("{excel_summary}", excel_summary).replace("{knowledge_context}", knowledge_context)
        
        # 获取当前活跃表信息
        active_table_info = loader.get_active_table_info()
        current_table_name = active_table_info.filename if active_table_info else "未知表"
        
        # 创建 Agent（LangGraph v1 使用 create_agent 替代已弃用的 create_react_agent）
        agent = create_agent(llm, ALL_TOOLS)
        
        # 构建消息 - 包含历史对话
        current_message = f"[当前操作表: {current_table_name}] {message}"
        messages = [SystemMessage(content=system_prompt)]
        
        # 添加历史对话
        if history:
            for msg in history:
                if msg.get("role") == "user":
                    messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "assistant":
                    messages.append(AIMessage(content=msg.get("content", "")))
        
        # 添加当前用户消息
        messages.append(HumanMessage(content=current_message))
        
        # 使用 stream_mode="messages" 获取真正的流式输出
        thinking_content = ""
        tool_call_yielded = False
        
        # 累积工具调用信息
        tool_names_by_id = {}  # id -> name
        args_by_id = {}  # id -> args dict
        args_by_index = {}  # index -> args_str（流式 chunk 兼容）
        tool_call_order = []  # 记录工具调用的顺序 [(id, index), ...]
        pending_tool_calls = []  # 按顺序缓存完整工具调用 [{id, name, args, index}, ...]
        yielded_tool_ids = set()
        registered_tool_ids = set()
        tool_call_counter = 0
        
        def _resolve_tool_call_id(tc_id: str, tc_index: int) -> str:
            """为缺失 id 的工具调用生成稳定标识"""
            if tc_id:
                return tc_id
            nonlocal tool_call_counter
            tool_call_counter += 1
            return f"call_{tool_call_counter}_idx_{tc_index}"

        def _parse_tool_args(args_str: str) -> Dict[str, Any]:
            """解析工具参数，兼容流式拼接的 JSON"""
            if not args_str:
                return {}
            try:
                return json.loads(args_str)
            except json.JSONDecodeError:
                try:
                    last_brace = args_str.rfind('{"')
                    if last_brace >= 0:
                        return json.loads(args_str[last_brace:])
                except Exception:
                    pass
            return {"raw": args_str}

        def _get_tool_args(tool_call_id: str, tc_index: int) -> Dict[str, Any]:
            """按 id 优先获取工具参数，失败时按调用顺序回退"""
            if tool_call_id in args_by_id:
                args = args_by_id.pop(tool_call_id)
                pending_tool_calls[:] = [
                    pending for pending in pending_tool_calls
                    if pending.get("id") != tool_call_id
                ]
                return args if isinstance(args, dict) else {}

            for i, pending in enumerate(pending_tool_calls):
                if pending.get("id") == tool_call_id:
                    return pending_tool_calls.pop(i).get("args", {})

            if pending_tool_calls:
                pending = pending_tool_calls.pop(0)
                args_by_id.pop(pending.get("id"), None)
                return pending.get("args", {})

            args_str = args_by_index.pop(tc_index, "{}")
            return _parse_tool_args(args_str)

        def _register_tool_call(tc_id: str, tc_name: str, tc_args: Dict[str, Any], tc_index: int):
            """登记一次完整的工具调用"""
            tool_names_by_id[tc_id] = tc_name
            args_by_id[tc_id] = tc_args
            args_by_index[tc_index] = json.dumps(tc_args, ensure_ascii=False)

            if tc_id in registered_tool_ids:
                for pending in pending_tool_calls:
                    if pending.get("id") == tc_id:
                        pending["args"] = tc_args
                        pending["name"] = tc_name
                        break
                return

            registered_tool_ids.add(tc_id)
            tool_call_order.append((tc_id, tc_index))
            pending_tool_calls.append({
                "id": tc_id,
                "name": tc_name,
                "args": tc_args,
                "index": tc_index,
            })

        async for chunk in agent.astream(
            {"messages": messages},
            stream_mode="messages"
        ):
            # chunk 是一个 tuple: (message, metadata)
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                msg, metadata = chunk[0], chunk[1]
                
                # 处理 AIMessage / AIMessageChunk (LLM 输出)
                # LangChain v1 的 create_agent 可能返回 AIMessage（完整）或 AIMessageChunk（流式增量）
                if isinstance(msg, (AIMessage, AIMessageChunk)):
                    content = msg.content if hasattr(msg, 'content') else ""
                    tool_call_chunks = getattr(msg, 'tool_call_chunks', [])
                    tool_calls_full = getattr(msg, 'tool_calls', [])
                    additional_kwargs = getattr(msg, 'additional_kwargs', {}) or {}
                    response_metadata = getattr(msg, 'response_metadata', {}) or {}

                    # 处理 reasoning_content（qwen3 thinking 模式的思考内容）
                    reasoning_content = ""
                    if isinstance(additional_kwargs, dict):
                        reasoning_content = additional_kwargs.get("reasoning_content", "") or ""
                    if not reasoning_content and isinstance(response_metadata, dict):
                        reasoning_content = response_metadata.get("reasoning_content", "") or ""
                    if reasoning_content:
                        thinking_content += reasoning_content
                        yield {"type": "thinking", "content": thinking_content}

                    # 优先处理完整的 tool_calls
                    if tool_calls_full:
                        for tc in tool_calls_full:
                            tc_id = _resolve_tool_call_id(tc.get("id") or "", tc.get("index", 0) or 0)
                            tc_name = tc.get("name", "")
                            tc_args = tc.get("args", {}) or {}
                            tc_index = tc.get("index", 0) if tc.get("index") is not None else 0
                            if tc_name:
                                _register_tool_call(tc_id, tc_name, tc_args, tc_index)

                    # 累积工具调用的 chunks
                    if tool_call_chunks and not tool_calls_full:
                        for tcc in tool_call_chunks:
                            tc_index = tcc.get("index", 0)
                            tc_id = _resolve_tool_call_id(tcc.get("id") or "", tc_index)
                            tc_name = tcc.get("name", "")
                            tc_args = tcc.get("args", "")

                            if tc_name:
                                if tc_id not in tool_names_by_id:
                                    tool_call_order.append((tc_id, tc_index))
                                tool_names_by_id[tc_id] = tc_name

                            if tc_args:
                                if tc_index not in args_by_index:
                                    args_by_index[tc_index] = ""
                                args_by_index[tc_index] += tc_args

                    # 文本内容统一作为思考过程
                    if content:
                        thinking_content += content
                        yield {"type": "thinking", "content": thinking_content}

                # 处理 ToolMessage (工具结果)
                elif isinstance(msg, ToolMessage):
                    tool_call_id = msg.tool_call_id if hasattr(msg, 'tool_call_id') else None
                    tool_name = msg.name if hasattr(msg, 'name') else "tool"
                    tool_content = msg.content

                    # 在发送 tool_result 之前，先发送对应的 tool_call
                    if tool_call_id and tool_call_id not in yielded_tool_ids:
                        yielded_tool_ids.add(tool_call_id)
                        tool_call_yielded = True

                        tc_index = 0
                        for (tid, idx) in tool_call_order:
                            if tid == tool_call_id:
                                tc_index = idx
                                break

                        args = _get_tool_args(tool_call_id, tc_index)
                        tc_name = tool_names_by_id.get(tool_call_id, tool_name)

                        if thinking_content.strip():
                            yield {"type": "thinking_done"}
                        thinking_content = ""

                        yield {
                            "type": "tool_call",
                            "id": tool_call_id,
                            "name": tc_name,
                            "args": args,
                        }

                    # 发送工具结果
                    try:
                        result = json.loads(tool_content)
                    except Exception:
                        result = {"result": tool_content}

                    yield {
                        "type": "tool_result",
                        "id": tool_call_id,
                        "name": tool_name,
                        "result": result,
                    }

                else:
                    # 未识别的消息类型，忽略
                    pass

        # 流式结束
        if not tool_call_yielded and thinking_content:
            # 没有工具调用，thinking_content 就是最终回答
            yield {"type": "thinking_done"}
            yield {"type": "clear_thinking"}
            yield {"type": "token", "content": thinking_content}
            yield {"type": "done", "content": thinking_content}
        elif tool_call_yielded and thinking_content.strip():
            # 有工具调用，剩余文本为最终回答
            yield {"type": "thinking_done"}
            yield {"type": "clear_thinking"}
            yield {"type": "token", "content": thinking_content}
            yield {"type": "done", "content": thinking_content}
        else:
            yield {"type": "done", "content": ""}
    
    except Exception as e:
        logger.exception(
            "stream chat failed",
            extra={"event": "stream_chat_failed", "outcome": "error", "error_code": "MODEL_ERROR"},
        )
        yield {"type": "thinking_done"}
        yield {"type": "error", "content": f"处理出错: {str(e)}"}

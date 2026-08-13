"""FastAPI HTTP 服务（支持流式输出和多表管理）"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_config
from .errors import (
    AppError,
    app_error_handler,
    http_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from .excel_loader import get_loader, reset_loader
from .graph import get_graph, reset_graph
from .logging_config import configure_logging
from .runtime import AppContainer, app_lifespan
from .security import (
    cleanup_path,
    save_upload_stream,
    validate_file_signature,
    validate_upload_filename,
    validate_xlsx_zip,
)

logger = configure_logging()


def _cors_origins() -> list[str]:
    config = get_config()
    origins = list(config.server.allowed_origins)
    if "*" in origins:
        raise RuntimeError("invalid configuration field: server.allowed_origins")
    for host in ("localhost", "127.0.0.1"):
        origins.append(f"http://{host}:{config.server.port}")
    if config.server.host not in {"0.0.0.0", "::", ""}:
        origins.append(f"http://{config.server.host}:{config.server.port}")
    return list(dict.fromkeys(origins))


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


def _safe_structure(structure: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """结构信息可展示，但不向客户端泄露服务器绝对路径。"""
    if structure is None:
        return None
    safe = dict(structure)
    if "file_path" in safe:
        safe["file_path"] = "[managed-upload]"
    return safe


def _require_model_credentials() -> None:
    """模型调用前只检查是否配置密钥，不回显密钥值或 Endpoint。"""
    provider = get_config().model.get_active_provider()
    if not provider.api_key:
        raise AppError(
            "MODEL_CREDENTIAL_MISSING",
            "模型服务未配置密钥",
            503,
            {"field": "model.api_key"},
        )

# 创建 FastAPI 应用
app = FastAPI(
    title="Excel 智能问数 Agent",
    description="基于 LangGraph 的 Excel 数据分析助手 API（支持多表）",
    version="0.2.0",
    lifespan=app_lifespan,
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_vendor_dir = Path(__file__).parent / "frontend" / "vendor"
if _vendor_dir.exists():
    app.mount("/vendor", StaticFiles(directory=_vendor_dir), name="vendor")

_frontend_dir = Path(__file__).parent / "frontend"
for _static_name in ("styles", "js"):
    _static_path = _frontend_dir / _static_name
    if _static_path.exists():
        app.mount(
            f"/{_static_name}",
            StaticFiles(directory=_static_path),
            name=f"frontend-{_static_name}",
        )


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request completed",
            extra={
                "event": "request_completed",
                "request_id": request_id,
                "duration_ms": duration_ms,
                "error_code": getattr(request.state, "error_code", None),
                "outcome": (
                    "success"
                    if response is not None and response.status_code < 400
                    else "error"
                ),
            },
        )
        if response is not None:
            response.headers["X-Request-ID"] = request_id


app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unexpected_error_handler)


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise AppError("RUNTIME_NOT_READY", "服务尚未完成初始化", 503)
    return container


# 阶段1 v2任务/Dataset/语义绑定路由。旧路由继续保留到迁移回归完成。
from .api_v2 import router as stage1_router  # noqa: E402
from .api_v2_analysis import router as stage2_router  # noqa: E402

app.include_router(stage1_router)
app.include_router(stage2_router)


@app.get("/api/v2/health")
async def health(container: AppContainer = Depends(get_container)):
    """不暴露密钥、路径或数据内容的健康检查。"""
    return {
        "status": "ok" if not container.shutting_down else "draining",
        "service": "excel_agent",
        "version": "0.2.0",
        "executor_workers": 2,
    }


@app.get("/favicon.ico")
async def favicon():
    """返回空 favicon 避免 404"""
    return Response(status_code=204)


class LoadExcelRequest(BaseModel):
    """加载 Excel 请求"""
    file_path: str
    sheet_name: Optional[str] = None


class LoadExcelResponse(BaseModel):
    """加载 Excel 响应"""
    success: bool
    message: str
    table_id: Optional[str] = None
    structure: Optional[Dict[str, Any]] = None
    preview: Optional[Dict[str, Any]] = None
    tables: Optional[List[Dict[str, Any]]] = None  # 所有表列表


class ChatRequest(BaseModel):
    """聊天请求"""
    message: str
    history: Optional[list] = None  # 历史对话列表


class ChatResponse(BaseModel):
    """聊天响应"""
    success: bool
    response: str
    tool_calls: Optional[list] = None


class TableInfo(BaseModel):
    """表信息"""
    id: str
    filename: str
    sheet_name: str
    total_rows: int
    total_columns: int
    loaded_at: str
    is_active: bool


class StatusResponse(BaseModel):
    """状态响应"""
    excel_loaded: bool
    tables: Optional[List[Dict[str, Any]]] = None  # 所有表信息
    active_table_id: Optional[str] = None
    active_table: Optional[Dict[str, Any]] = None  # 当前活跃表详情


class SetActiveTableRequest(BaseModel):
    """设置活跃表请求"""
    table_id: str


@app.get("/", response_class=HTMLResponse)
async def root():
    """返回前端页面"""
    frontend_path = Path(__file__).parent / "frontend" / "index.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="""
    <html>
        <head><title>Excel Agent</title></head>
        <body>
            <h1>Excel 智能问数 Agent</h1>
            <p>API 文档: <a href="/docs">/docs</a></p>
        </body>
    </html>
    """)


@app.get("/v2", response_class=HTMLResponse)
async def stage1_workbench():
    """阶段1任务/Dataset最小工作台。"""
    frontend_path = Path(__file__).parent / "frontend" / "stage1.html"
    if not frontend_path.exists():
        raise AppError("FRONTEND_NOT_FOUND", "工作台资源不存在", 500)
    return HTMLResponse(content=frontend_path.read_text(encoding="utf-8"))


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """获取当前状态（包含多表信息）"""
    loader = get_loader()
    
    if not loader.is_loaded:
        return StatusResponse(excel_loaded=False)
    
    # 获取所有表信息
    tables = loader.list_tables()
    
    # 获取活跃表详情
    active_loader = loader.get_active_loader()
    active_table = None
    if active_loader:
        structure = _safe_structure(active_loader.get_structure()) or {}
        active_table = {
            "file_path": structure["file_path"],
            "sheet_name": structure["sheet_name"],
            "total_rows": structure["total_rows"],
            "total_columns": structure["total_columns"],
            "columns": structure["columns"],
        }
    
    return StatusResponse(
        excel_loaded=True,
        tables=tables,
        active_table_id=loader.active_table_id,
        active_table=active_table,
    )


@app.get("/tables")
async def list_tables():
    """获取所有已加载的表列表"""
    loader = get_loader()
    return {
        "success": True,
        "tables": loader.list_tables(),
        "active_table_id": loader.active_table_id,
    }


@app.put("/tables/active")
async def set_active_table(request: SetActiveTableRequest):
    """设置当前活跃表"""
    loader = get_loader()
    
    if not loader.set_active_table(request.table_id):
        raise HTTPException(status_code=404, detail=f"表不存在: {request.table_id}")
    
    # 重置图以使用新的活跃表数据
    reset_graph()
    
    # 获取新活跃表的信息
    active_loader = loader.get_active_loader()
    structure = _safe_structure(active_loader.get_structure()) if active_loader else None
    preview = active_loader.get_preview() if active_loader else None
    
    return {
        "success": True,
        "message": f"已切换到表: {request.table_id}",
        "structure": structure,
        "preview": preview,
        "tables": loader.list_tables(),
    }


@app.delete("/tables/{table_id}")
async def delete_table(table_id: str):
    """删除指定表"""
    loader = get_loader()
    
    if not loader.remove_table(table_id):
        raise HTTPException(status_code=404, detail=f"表不存在: {table_id}")
    
    # 重置图
    reset_graph()
    
    return {
        "success": True,
        "message": f"已删除表: {table_id}",
        "tables": loader.list_tables(),
        "active_table_id": loader.active_table_id,
    }


@app.get("/tables/{table_id}/columns")
async def get_table_columns(table_id: str):
    """获取指定表的列名列表"""
    loader = get_loader()
    columns = loader.get_table_columns(table_id)
    
    if not columns:
        raise HTTPException(status_code=404, detail=f"表不存在或无数据: {table_id}")
    
    return {
        "success": True,
        "table_id": table_id,
        "columns": columns,
    }


class JoinTablesRequest(BaseModel):
    """连接表请求（支持多字段连接）"""
    table1_id: str
    table2_id: str
    keys1: List[str]  # 表1的连接字段列表
    keys2: List[str]  # 表2的连接字段列表
    join_type: str = "inner"
    new_name: str


@app.post("/tables/join")
async def join_tables(request: JoinTablesRequest):
    """连接两张表（支持多字段连接）"""
    loader = get_loader()
    
    try:
        table_id, structure = loader.join_tables(
            table1_id=request.table1_id,
            table2_id=request.table2_id,
            keys1=request.keys1,
            keys2=request.keys2,
            join_type=request.join_type,
            new_name=request.new_name,
        )
        
        # 重置图
        reset_graph()
        
        return {
            "success": True,
            "message": f"成功创建连接表: {request.new_name}",
            "table_id": table_id,
            "structure": structure,
            "tables": loader.list_tables(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接失败: {str(e)}")


class SuggestJoinRequest(BaseModel):
    """AI联表建议请求"""
    table1_id: str
    table2_id: str


@app.post("/tables/suggest-join")
async def suggest_join(request: SuggestJoinRequest):
    """使用AI分析两表结构并建议联表配置"""
    from .join_service import suggest_join_config
    
    loader = get_loader()
    
    # 获取两表的loader
    loader1 = loader.get_table(request.table1_id)
    loader2 = loader.get_table(request.table2_id)
    
    if not loader1 or not loader2:
        raise HTTPException(status_code=404, detail="指定的表不存在")
    
    # 获取两表的summary
    table1_summary = loader1.get_summary()
    table2_summary = loader2.get_summary()
    
    try:
        suggestion = suggest_join_config(table1_summary, table2_summary)
        return {
            "success": True,
            "suggestion": suggestion,
        }
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception(
            "join suggestion failed",
            extra={
                "event": "join_suggestion_failed",
                "outcome": "error",
                "error_code": "MODEL_ERROR",
            },
        )
        raise HTTPException(status_code=500, detail=f"AI分析失败: {str(e)}")


# Removed in stage 0: loading arbitrary server-side paths is unsafe.  The
# legacy function remains unregistered only to keep internal regression code
# import-compatible until the v2 Dataset service is introduced.
async def load_excel(request: LoadExcelRequest):
    """通过文件路径加载 Excel 文件（追加模式）"""
    try:
        loader = get_loader()
        table_id, structure = loader.add_table(request.file_path, request.sheet_name)
        
        # 获取预览
        active_loader = loader.get_active_loader()
        preview = active_loader.get_preview() if active_loader else None
        
        # 重置图以使用新的 Excel 数据
        reset_graph()
        
        return LoadExcelResponse(
            success=True,
            message=f"成功加载 Excel 文件: {request.file_path}",
            table_id=table_id,
            structure=structure,
            preview=preview,
            tables=loader.list_tables(),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"加载失败: {str(e)}")


@app.post("/upload", response_model=LoadExcelResponse)
async def upload_excel(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = None,
    container: AppContainer = Depends(get_container),
):
    """上传 Excel 文件（追加模式，会添加到多表管理器）"""
    suffix = validate_upload_filename(file.filename)
    tmp_path = container.temp_resources.create_path(suffix)
    
    try:
        await save_upload_stream(
            file,
            tmp_path,
            max_bytes=container.config.runtime.max_upload_bytes,
            chunk_size=container.config.runtime.upload_chunk_bytes,
        )
        validate_file_signature(tmp_path, suffix)
        validate_xlsx_zip(tmp_path)
        
        # 加载 Excel（追加模式）
        loader = get_loader()
        loop = asyncio.get_running_loop()
        table_id, structure = await loop.run_in_executor(
            container.executor,
            lambda: loader.add_table(str(tmp_path), sheet_name),
        )
        
        # 更新文件名（临时文件路径替换为原始文件名）
        table_info = loader.get_table_info(table_id)
        if table_info:
            table_info.filename = file.filename
        structure = _safe_structure(structure)
        
        # 获取预览
        active_loader = loader.get_active_loader()
        preview = active_loader.get_preview() if active_loader else None
        
        # 重置图
        reset_graph()
        
        return LoadExcelResponse(
            success=True,
            message=f"成功上传并加载 Excel 文件: {file.filename}",
            table_id=table_id,
            structure=structure,
            preview=preview,
            tables=loader.list_tables(),
        )
    except asyncio.CancelledError:
        cleanup_path(tmp_path)
        raise
    except AppError:
        cleanup_path(tmp_path)
        raise
    except (ValueError, FileNotFoundError) as exc:
        cleanup_path(tmp_path)
        raise AppError("UPLOAD_PARSE_FAILED", "文件解析失败", 400) from exc
    except Exception:
        cleanup_path(tmp_path)
        raise


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """与 Agent 对话（非流式）"""
    loader = get_loader()
    
    if not loader.is_loaded:
        raise HTTPException(
            status_code=400, 
            detail="请先上传 Excel 文件（使用 /upload 接口）"
        )

    _require_model_credentials()
    
    try:
        graph = get_graph()
        
        # 构建输入
        inputs = {
            "messages": [HumanMessage(content=request.message)],
            "is_relevant": True,
        }
        
        # 执行图
        result = graph.invoke(inputs)
        
        # 提取响应
        messages = result.get("messages", [])
        response_text = ""
        tool_calls = []
        
        for msg in messages:
            if isinstance(msg, AIMessage):
                if msg.content:
                    response_text = msg.content
                if msg.tool_calls:
                    tool_calls.extend([
                        {"name": tc["name"], "args": tc["args"]}
                        for tc in msg.tool_calls
                    ])
        
        return ChatResponse(
            success=True,
            response=response_text,
            tool_calls=tool_calls if tool_calls else None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    from .stream import stream_chat

    """与 Agent 对话（流式输出）"""
    loader = get_loader()
    
    if not loader.is_loaded:
        raise HTTPException(
            status_code=400, 
            detail="请先加载 Excel 文件"
        )

    _require_model_credentials()
    
    # 获取当前活跃表ID，用于前端标记消息
    active_table_id = loader.active_table_id
    
    async def generate():
        # 先发送表ID信息
        table_event = {"type": "table_info", "table_id": active_table_id}
        yield f"data: {json_dumps(table_event, ensure_ascii=False)}\n\n"
        
        async for event in stream_chat(request.message, request.history):
            yield f"data: {json_dumps(event, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.post("/reset")
async def reset():
    """重置 Agent 状态（清空所有表）"""
    reset_loader()
    reset_graph()
    return {"success": True, "message": "已重置 Agent 状态，所有表已清空"}


# ============ 知识库管理 API ============

def _knowledge_deps():
    """按请求延迟加载 Chroma，使阶段0健康检查不依赖外部向量库。"""
    try:
        from .knowledge_base import KnowledgeItem, get_knowledge_base, reset_knowledge_base
    except ModuleNotFoundError as exc:
        raise AppError("KNOWLEDGE_UNAVAILABLE", "知识库依赖未安装", 503) from exc
    return get_knowledge_base, KnowledgeItem, reset_knowledge_base


def get_knowledge_base():
    return _knowledge_deps()[0]()


def _knowledge_item(*args, **kwargs):
    return _knowledge_deps()[1](*args, **kwargs)


def reset_knowledge_base():
    return _knowledge_deps()[2]()


class KnowledgeEntryCreate(BaseModel):
    """创建知识条目请求"""
    content: str
    title: Optional[str] = None
    category: Optional[str] = "general"
    tags: Optional[List[str]] = []
    related_columns: Optional[List[str]] = []
    priority: Optional[str] = "normal"


class KnowledgeEntryUpdate(BaseModel):
    """更新知识条目请求"""
    content: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None


class KnowledgeSearchRequest(BaseModel):
    """知识检索请求"""
    query: str
    top_k: Optional[int] = 3


@app.get("/knowledge")
async def list_knowledge(limit: int = 100, offset: int = 0):
    """获取知识条目列表"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用", "entries": []}
    
    entries = kb.list_entries(limit=limit, offset=offset)
    stats = kb.get_stats()
    return {
        "entries": entries,
        "total": stats["total_entries"],
        "limit": limit,
        "offset": offset
    }


@app.get("/knowledge/stats")
async def get_knowledge_stats():
    """获取知识库统计信息"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    return kb.get_stats()


@app.get("/knowledge/{item_id}")
async def get_knowledge_entry(item_id: str):
    """获取单个知识条目"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    entry = kb.get_entry(item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    
    return {
        "id": entry.id,
        "title": entry.title,
        "content": entry.content,
        "category": entry.category,
        "tags": entry.tags,
        "related_columns": entry.related_columns,
        "priority": entry.priority,
        "source_file": entry.source_file
    }


@app.post("/knowledge")
async def create_knowledge_entry(entry: KnowledgeEntryCreate):
    """创建知识条目"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    # 生成 ID
    import hashlib
    import time
    content_hash = hashlib.md5(entry.content.encode()).hexdigest()[:8]
    item_id = f"kb_api_{int(time.time())}_{content_hash}"
    
    # 自动提取标题
    title = entry.title
    if not title:
        lines = entry.content.strip().split("\n")
        title = lines[0].lstrip("#").strip()[:50] if lines else "未命名"
    
    item = _knowledge_item(
        id=item_id,
        content=entry.content,
        title=title,
        category=entry.category or "general",
        tags=entry.tags or [],
        related_columns=entry.related_columns or [],
        priority=entry.priority or "normal"
    )
    
    kb.add_entry(item)
    
    return {"success": True, "id": item_id, "title": title}


@app.put("/knowledge/{item_id}")
async def update_knowledge_entry(item_id: str, entry: KnowledgeEntryUpdate):
    """更新知识条目"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    success = kb.update_entry(
        item_id=item_id,
        content=entry.content,
        title=entry.title,
        category=entry.category,
        tags=entry.tags
    )
    
    if not success:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    
    return {"success": True, "id": item_id}


@app.delete("/knowledge/{item_id}")
async def delete_knowledge_entry(item_id: str):
    """删除知识条目"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    success = kb.delete_entry(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="知识条目不存在或删除失败")
    
    return {"success": True, "id": item_id}


@app.post("/knowledge/search")
async def search_knowledge(request: KnowledgeSearchRequest):
    """检索相关知识"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用", "results": []}
    
    items = kb.search(query=request.query, top_k=request.top_k)
    
    return {
        "query": request.query,
        "results": [
            {
                "id": item.id,
                "title": item.title,
                "content": item.content[:200] + "..." if len(item.content) > 200 else item.content,
                "category": item.category,
                "tags": item.tags
            }
            for item in items
        ]
    }


@app.post("/knowledge/upload")
async def upload_knowledge_file(
    file: UploadFile = File(...),
    container: AppContainer = Depends(get_container),
):
    """上传知识文件（支持 .md, .txt）"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    suffix = validate_upload_filename(
        file.filename,
        allowed_extensions={".md", ".txt", ".markdown"},
    )
    
    # 保存文件到 knowledge 目录
    knowledge_dir = Path(kb.kb_config.knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    
    # 持久化文件使用 UUID，用户文件名只用于展示，避免路径穿越。
    file_path = knowledge_dir / f"{uuid.uuid4()}{suffix}"
    try:
        await save_upload_stream(
            file,
            file_path,
            max_bytes=container.config.runtime.max_upload_bytes,
            chunk_size=container.config.runtime.upload_chunk_bytes,
        )
    except Exception:
        cleanup_path(file_path)
        raise
    
    # 加载并索引
    item = kb.load_from_file(file_path)
    kb.add_entry(item)
    
    return {
        "success": True,
        "id": item.id,
        "title": item.title,
        "file": file.filename
    }


@app.post("/knowledge/index")
async def index_knowledge_directory():
    """索引 knowledge 目录下的所有知识文件"""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "知识库未启用"}
    
    count = kb.index_directory()
    
    return {
        "success": True,
        "indexed_count": count,
        "message": f"成功索引 {count} 个知识文件"
    }


@app.post("/knowledge/reset")
async def reset_knowledge():
    """重置知识库"""
    reset_knowledge_base()
    return {"success": True, "message": "知识库已重置"}


def run_server():
    """运行服务器"""
    import uvicorn
    config = get_config()
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
    )

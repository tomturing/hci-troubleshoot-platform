# Pydantic AI RAG 系统设计指南

## 概述

Pydantic AI 提供了多种实现 RAG（Retrieval-Augmented Generation）的方式，从简单的自定义工具到 Provider 原生的文件搜索能力。本文档详细分析这些方式的设计模式和最佳实践。

---

## 一、RAG 实现方式概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Pydantic AI RAG 实现方式                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐ │
│  │  自定义工具模式  │    │  Embedder +     │    │  原生工具模式   │ │
│  │  (最灵活)       │    │  向量数据库      │    │  (最简单)       │ │
│  │                 │    │                 │    │                 │ │
│  │  自定义检索逻辑  │    │  统一 Embedding │    │  FileSearchTool │ │
│  │  任意向量库     │    │  接口           │    │  Provider 管理  │ │
│  │                 │    │                 │    │                 │ │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘ │
│                                                                     │
│  适用场景：               适用场景：               适用场景：         │
│  - 高度定制化             - 多 Provider           - 快速原型         │
│  - 自有向量库             - 自建向量索引          - Provider 托管   │
│  - 复杂检索逻辑           - 需要可控性            - 无需管理基础设施 │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、方式一：自定义工具 + 向量数据库

### 2.1 架构设计

```
┌─────────────────────────────────────────────────────────────────────┐
│                        自定义 RAG 工具架构                            │
│                                                                     │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐       │
│   │   Agent      │────►│  Tool:       │────►│  Embedder    │       │
│   │              │     │  retrieve    │     │              │       │
│   └──────────────┘     └──────────────┘     └──────────────┘       │
│                               │                      │              │
│                               │                      ▼              │
│                               │              ┌──────────────┐       │
│                               │              │  Query Vec   │       │
│                               │              └──────────────┘       │
│                               │                      │              │
│                               ▼                      ▼              │
│                       ┌──────────────┐     ┌──────────────┐       │
│                       │  Vector DB   │◄────│  Similarity  │       │
│                       │  (pgvector)  │     │  Search      │       │
│                       └──────────────┘     └──────────────┘       │
│                               │                                     │
│                               ▼                                     │
│                       ┌──────────────┐                              │
│                       │  Documents   │                              │
│                       │  (Top K)     │                              │
│                       └──────────────┘                              │
│                               │                                     │
│                               ▼                                     │
│                       ┌──────────────┐                              │
│                       │  Context     │                              │
│                       │  Injection   │                              │
│                       └──────────────┘                              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 完整实现示例

```python
from dataclasses import dataclass
from typing import Sequence

from pydantic_ai import Agent, RunContext, Embedder
from pydantic_ai.embeddings import EmbeddingResult

import asyncpg
import pydantic_core


@dataclass
class RAGDeps:
    """RAG 系统依赖"""
    embedder: Embedder                    # Embedding 模型
    pool: asyncpg.Pool                    # PostgreSQL + pgvector
    top_k: int = 5                        # 检索文档数


agent = Agent(
    'openai:gpt-5.2',
    deps_type=RAGDeps,
    system_prompt='Use the retrieve tool to search documentation before answering.',
)


@agent.tool
async def retrieve(ctx: RunContext[RAGDeps], query: str) -> str:
    """检索相关文档
    
    Args:
        query: 用户查询文本
    Returns:
        检索到的文档内容
    """
    # 1. 生成查询向量
    embedding_result: EmbeddingResult = await ctx.deps.embedder.embed_query(query)
    embedding = embedding_result[0]
    
    # 2. 向量相似度检索 (使用 pgvector)
    embedding_json = pydantic_core.to_json(embedding).decode()
    
    rows = await ctx.deps.pool.fetch(
        '''
        SELECT id, title, content, url, 
               1 - (embedding <=> $1::vector) as similarity
        FROM documents
        ORDER BY embedding <=> $1::vector
        LIMIT $2
        ''',
        embedding_json,
        ctx.deps.top_k,
    )
    
    # 3. 格式化检索结果
    context_parts = []
    for row in rows:
        context_parts.append(
            f'# {row["title"]}\n'
            f'Source: {row["url"]}\n'
            f'Similarity: {row["similarity"]:.3f}\n\n'
            f'{row["content"]}\n'
        )
    
    return '\n---\n'.join(context_parts)


# 使用示例
async def main():
    embedder = Embedder('openai:text-embedding-3-small')
    pool = await asyncpg.create_pool('postgresql://user:pass@localhost:5432/rag_db')
    
    deps = RAGDeps(embedder=embedder, pool=pool, top_k=5)
    
    result = await agent.run(
        'How do I configure the agent?',
        deps=deps,
    )
    print(result.output)


# 运行
import asyncio
asyncio.run(main())
```

### 2.3 数据库 Schema 设计

```sql
-- PostgreSQL + pgvector Schema
CREATE EXTENSION IF NOT EXISTS vector;

-- 文档表
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    
    -- 向量列 (维度取决于 Embedding 模型)
    -- text-embedding-3-small: 1536 维
    -- text-embedding-3-large: 3072 维
    embedding vector(1536) NOT NULL
);

-- 创建 HNSW 索引 (高性能近似检索)
CREATE INDEX idx_documents_embedding 
ON documents USING hnsw (embedding vector_cosine_ops);

-- 或创建 IVFFlat 索引 (适合大数据集)
-- CREATE INDEX idx_documents_embedding 
-- ON documents USING ivfflat (embedding vector_cosine_ops) 
-- WITH (lists = 100);

-- 添加来源和标签索引
CREATE INDEX idx_documents_url ON documents(url);
CREATE INDEX idx_documents_metadata ON documents USING gin(metadata);
```

### 2.4 文档索引流程

```python
from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings
import asyncpg
import pydantic_core

async def index_documents(docs: list[dict], embedder: Embedder, pool: asyncpg.Pool):
    """批量索引文档
    
    Args:
        docs: 文档列表 [{"title": ..., "content": ..., "url": ...}]
        embedder: Embedder 实例
        pool: 数据库连接池
    """
    # 批量生成 Embedding
    texts = [f'{doc["title"]}\n{doc["content"]}' for doc in docs]
    result = await embedder.embed_documents(texts)
    
    # 插入数据库
    async with pool.acquire() as conn:
        for doc, embedding in zip(docs, result.embeddings):
            embedding_json = pydantic_core.to_json(embedding).decode()
            await conn.execute(
                '''
                INSERT INTO documents (title, content, url, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (url) DO UPDATE SET
                    title = $1,
                    content = $2,
                    embedding = $4,
                    metadata = $5
                ''',
                doc['title'],
                doc['content'],
                doc['url'],
                embedding_json,
                doc.get('metadata', {}),
            )
```

### 2.5 Embedder 选择指南

```python
from pydantic_ai import Embedder
from pydantic_ai.embeddings import EmbeddingSettings

# 方式 1: OpenAI (推荐，稳定可靠)
embedder = Embedder('openai:text-embedding-3-small')  # 1536 维，便宜
embedder = Embedder('openai:text-embedding-3-large')  # 3072 维，效果好

# 方式 2: 降维配置
embedder = Embedder(
    'openai:text-embedding-3-small',
    settings=EmbeddingSettings(dimensions=256),  # 降维到 256
)

# 方式 3: 本地模型 (免费，私密)
embedder = Embedder('sentence-transformers:all-MiniLM-L6-v2')

# 方式 4: 多语言支持
embedder = Embedder('cohere:embed-multilingual-v3.0')

# 方式 5: 专业领域
embedder = Embedder('voyageai:voyage-code-3')  # 代码
embedder = Embedder('voyageai:voyage-law-2')   # 法律
```

### 2.6 Provider 支持

| Provider | 模型示例 | 维度 | 特点 |
|----------|---------|------|------|
| **OpenAI** | `text-embedding-3-small` | 1536 (可降) | 稳定，支持降维 |
| **OpenAI** | `text-embedding-3-large` | 3072 (可降) | 高质量 |
| **Google** | `gemini-embedding-001` | 3072 (可降) | 支持 task_type |
| **Cohere** | `embed-v4.0` | 1024 | 多语言支持 |
| **Cohere** | `embed-multilingual-v3.0` | 1024 | 100+ 语言 |
| **VoyageAI** | `voyage-3.5` | 1024 | 高性价比 |
| **Bedrock** | `amazon.titan-embed-text-v2` | 1536 | AWS 原生 |
| **Sentence Transformers** | `all-MiniLM-L6-v2` | 384 | 免费，本地 |

---

## 三、方式二：原生 FileSearchTool（Provider 托管）

### 3.1 架构设计

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Provider 托管 RAG 架构                            │
│                                                                     │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐       │
│   │   Agent      │────►│ FileSearchTool│────►│   Provider   │       │
│   │              │     │ (Native)      │     │   API        │       │
│   └──────────────┘     └──────────────┘     └──────────────┘       │
│                                                     │               │
│                                                     ▼               │
│                                            ┌──────────────┐        │
│                                            │ Vector Store │        │
│                                            │ (托管)       │        │
│                                            └──────────────┘        │
│                                                     │               │
│                                                     ▼               │
│                                            ┌──────────────┐        │
│                                            │ Files API    │        │
│                                            │ 上传文件     │        │
│                                            └──────────────┘        │
│                                                                     │
│  Provider 负责：                                                    │
│  - 文件存储                                                         │
│  - 自动分块                                                         │
│  - Embedding 生成                                                   │
│  - 向量索引                                                         │
│  - 相似度检索                                                       │
│  - 结果注入                                                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 OpenAI Responses 实现

```python
import asyncio

from pydantic_ai import Agent, FileSearchTool
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings


async def setup_rag_with_openai():
    model = OpenAIResponsesModel('gpt-5.2')
    
    # 1. 上传文件
    with open('my_document.pdf', 'rb') as f:
        file = await model.client.files.create(file=f, purpose='assistants')
    
    # 2. 创建向量存储
    vector_store = await model.client.vector_stores.create(name='my-docs')
    
    # 3. 添加文件到向量存储
    await model.client.vector_stores.files.create(
        vector_store_id=vector_store.id,
        file_id=file.id,
    )
    
    # 4. 创建 Agent（使用 FileSearchTool）
    agent = Agent(
        model,
        capabilities=[
            NativeTool(FileSearchTool(file_store_ids=[vector_store.id]))
        ],
        model_settings=OpenAIResponsesModelSettings(
            openai_include_file_search_results=True,  # 包含检索结果详情
        ),
    )
    
    # 5. 运行查询
    result = await agent.run('What information is in my documents about pydantic?')
    print(result.output)
    
    # 6. 查看检索详情
    for part in result.response.native_tool_calls:
        print(f"Retrieved from: {part.tool_name}")
    
    return agent


asyncio.run(setup_rag_with_openai())
```

### 3.3 Google Gemini 实现

```python
import asyncio

from pydantic_ai import Agent, FileSearchTool
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.models.google import GoogleModel


async def setup_rag_with_gemini():
    model = GoogleModel('gemini-3-flash-preview')
    
    # 1. 创建文件搜索存储
    store = await model.client.aio.file_search_stores.create(
        config={'display_name': 'my-docs'}
    )
    
    # 2. 上传文件
    with open('my_document.txt', 'rb') as f:
        await model.client.aio.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store.name,
            file=f,
            config={'mime_type': 'text/plain'},
        )
    
    # 3. 创建 Agent
    agent = Agent(
        model,
        capabilities=[
            NativeTool(FileSearchTool(file_store_ids=[store.name]))
        ],
    )
    
    # 4. 运行查询
    result = await agent.run('Summarize the key points from my uploaded documents.')
    print(result.output)
    
    return agent


asyncio.run(setup_rag_with_gemini())
```

### 3.4 Provider 对比

| 特性 | OpenAI Responses | Google Gemini | xAI |
|------|-----------------|---------------|-----|
| **支持状态** | ✅ Full | ✅ Full | ✅ Collections |
| **文件上传** | Files API | Gemini Files API | Collections |
| **最大文件** | 512 MB | 2 GB | - |
| **存储限制** | - | 20 GB/project | - |
| **自动过期** | - | 48 小时 | - |
| **支持的格式** | PDF, TXT, MD, JSON, etc. | 多种格式 | - |

---

## 四、方式三：混合架构

### 4.1 设计思路

结合自定义工具和原生工具的优势：

```python
from pydantic_ai import Agent, RunContext, Embedder, FileSearchTool
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.models.openai import OpenAIResponsesModel


@dataclass
class HybridRAGDeps:
    """混合 RAG 依赖"""
    embedder: Embedder               # 自定义 Embedding
    custom_index: asyncpg.Pool       # 自有向量库
    native_store_ids: list[str]      # Provider 向量存储


agent = Agent(
    'openai-responses:gpt-5.2',
    deps_type=HybridRAGDeps,
    capabilities=[NativeTool(FileSearchTool(file_store_ids=[]))],  # 动态配置
)


@agent.tool
async def search_custom_kb(ctx: RunContext[HybridRAGDeps], query: str) -> str:
    """搜索自有知识库"""
    embedding = await ctx.deps.embedder.embed_query(query)
    # ... 自定义检索逻辑
    return custom_results


@agent.tool
async def search_uploaded_files(ctx: RunContext[HybridRAGDeps], query: str) -> str:
    """搜索用户上传文件 (由 Provider 处理)"""
    # 实际检索由 FileSearchTool 处理
    # 这里只是提供额外的过滤逻辑
    pass
```

---

## 五、高级特性

### 5.1 检索结果重排序

```python
@agent.tool
async def retrieve_with_rerank(ctx: RunContext[RAGDeps], query: str) -> str:
    """带重排序的检索"""
    # 1. 初步检索 (获取更多候选)
    embedding = await ctx.deps.embedder.embed_query(query)
    
    rows = await ctx.deps.pool.fetch(
        '''
        SELECT id, title, content, 
               1 - (embedding <=> $1::vector) as similarity
        FROM documents
        ORDER BY embedding <=> $1::vector
        LIMIT 20  -- 获取更多候选
        ''',
        pydantic_core.to_json(embedding[0]).decode(),
    )
    
    # 2. 重排序 (基于关键词匹配或其他特征)
    reranked = []
    for row in rows:
        score = row['similarity']
        # 关键词匹配加分
        if query.lower() in row['content'].lower():
            score += 0.1
        reranked.append((row, score))
    
    # 3. 取 Top K
    reranked.sort(key=lambda x: x[1], reverse=True)
    top_k = reranked[:ctx.deps.top_k]
    
    # 4. 格式化返回
    return '\n---\n'.join(
        f'# {r["title"]}\n{r["content"]}' 
        for r, _ in top_k
    )
```

### 5.2 分块策略

```python
from dataclasses import dataclass
from typing import Iterator


@dataclass
class ChunkConfig:
    """分块配置"""
    chunk_size: int = 500       # 每块字符数
    chunk_overlap: int = 100    # 重叠字符数
    separator: str = '\n\n'     # 分隔符


def chunk_document(content: str, config: ChunkConfig) -> Iterator[str]:
    """文档分块"""
    paragraphs = content.split(config.separator)
    
    current_chunk = []
    current_length = 0
    
    for para in paragraphs:
        para_len = len(para)
        
        if current_length + para_len > config.chunk_size:
            if current_chunk:
                yield config.separator.join(current_chunk)
            
            # 保留重叠部分
            overlap_start = max(0, len(current_chunk) - 2)
            current_chunk = current_chunk[overlap_start:]
            current_length = sum(len(p) for p in current_chunk)
        
        current_chunk.append(para)
        current_length += para_len + len(config.separator)
    
    if current_chunk:
        yield config.separator.join(current_chunk)


# 索引时使用
async def index_document(content: str, embedder: Embedder, pool: asyncpg.Pool, config: ChunkConfig):
    """索引单个文档（支持分块）"""
    chunks = list(chunk_document(content, config))
    
    # 批量 Embedding
    embeddings = await embedder.embed_documents(chunks)
    
    # 存储
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings.embeddings)):
        await pool.execute(
            '''
            INSERT INTO document_chunks (doc_id, chunk_index, content, embedding)
            VALUES ($1, $2, $3, $4)
            ''',
            doc_id,
            i,
            chunk,
            pydantic_core.to_json(emb).decode(),
        )
```

### 5.3 元数据过滤

```python
@agent.tool
async def retrieve_with_filter(
    ctx: RunContext[RAGDeps], 
    query: str,
    source: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """带元数据过滤的检索"""
    embedding = await ctx.deps.embedder.embed_query(query)
    embedding_json = pydantic_core.to_json(embedding[0]).decode()
    
    # 构建 SQL
    sql = '''
        SELECT id, title, content, 
               1 - (embedding <=> $1::vector) as similarity
        FROM documents
        WHERE 1=1
    '''
    params = [embedding_json]
    
    if source:
        sql += ' AND url LIKE $2'
        params.append(f'%{source}%')
    
    if tags:
        sql += ' AND metadata->\'tags\' @> $3::jsonb'
        params.append(pydantic_core.to_json(tags).decode())
    
    sql += ' ORDER BY embedding <=> $1::vector LIMIT $4'
    params.append(ctx.deps.top_k)
    
    rows = await ctx.deps.pool.fetch(sql, *params)
    
    return '\n---\n'.join(
        f'# {r["title"]}\n{r["content"]}' 
        for r in rows
    )
```

### 5.4 流式检索结果

```python
from pydantic_ai import Agent


async def stream_rag_response(query: str, deps: RAGDeps):
    """流式返回 RAG 结果"""
    async with agent.run_stream(query, deps=deps) as result:
        async for text in result.stream_text(delta=True):
            print(text, end='', flush=True)


# 或使用事件流
async def stream_with_events(query: str, deps: RAGDeps):
    """带事件流的 RAG"""
    async with agent.iter(query, deps=deps) as run:
        async for node in run:
            if isinstance(node, ModelRequestNode):
                # 检索发生在此节点之前
                print("Retrieving documents...")
            elif isinstance(node, CallToolsNode):
                # 模型生成响应
                response = node.model_response
                print(f"Generated: {response.text}")
```

---

## 六、最佳实践总结

### 6.1 方式选择指南

| 场景 | 推荐方式 | 原因 |
|------|---------|------|
| **快速原型** | FileSearchTool | 无需基础设施 |
| **生产环境** | 自定义工具 | 可控、可观测 |
| **多 Provider** | Embedder + 自定义 | 统一接口 |
| **私有数据** | 自定义工具 | 数据不出域 |
| **用户上传文件** | FileSearchTool | Provider 托管 |
| **专业领域** | VoyageAI + 自定义 | 专用 Embedding |

### 6.2 性能优化建议

```python
# 1. 批量 Embedding
embeddings = await embedder.embed_documents(docs)  # 而非逐个 embed_query

# 2. 连接池复用
pool = await asyncpg.create_pool(min_size=5, max_size=20)

# 3. 索引优化
# HNSW: 高性能，适合中小规模
# IVFFlat: 适合大规模 (>100万)

# 4. 缓存热门查询
@lru_cache(maxsize=1000)
def cached_retrieve(query_hash: str) -> str:
    ...

# 5. 预计算 Embedding
# 对于静态文档，预先计算并存储
```

### 6.3 可观测性设计

```python
import logfire
from pydantic_ai import Agent, RunContext

logfire.configure()
logfire.instrument_pydantic_ai()
logfire.instrument_asyncpg()


@agent.tool
async def retrieve(ctx: RunContext[RAGDeps], query: str) -> str:
    """带可观测性的检索"""
    with logfire.span('RAG retrieve', query=query):
        # Embedding
        with logfire.span('embedding'):
            embedding = await ctx.deps.embedder.embed_query(query)
            logfire.info('embedding dimensions={len(embedding[0])}')
        
        # Search
        with logfire.span('vector_search'):
            rows = await ctx.deps.pool.fetch(...)
            logfire.info('retrieved {len(rows)} documents')
        
        # Format
        with logfire.span('format_context'):
            context = '\n---\n'.join(...)
        
        return context
```

---

## 七、完整示例：生产级 RAG 系统

```python
"""
生产级 RAG 系统示例
包含：文档索引、检索、重排序、可观测性
"""

from dataclasses import dataclass
from typing import Sequence

import asyncpg
import pydantic_core
import logfire

from pydantic_ai import Agent, RunContext, Embedder
from pydantic_ai.embeddings import EmbeddingSettings, EmbeddingResult


logfire.configure()
logfire.instrument_pydantic_ai()
logfire.instrument_asyncpg()


@dataclass
class Document:
    """文档结构"""
    id: int
    title: str
    content: str
    url: str
    metadata: dict


@dataclass
class Chunk:
    """文档块"""
    doc_id: int
    chunk_index: int
    content: str


@dataclass
class RAGDeps:
    """RAG 依赖"""
    embedder: Embedder
    pool: asyncpg.Pool
    top_k: int = 5
    rerank_top_n: int = 20


@dataclass
class RetrievalResult:
    """检索结果"""
    documents: list[Document]
    chunks: list[Chunk]
    scores: list[float]


# Agent 定义
agent = Agent(
    'openai:gpt-5.2',
    deps_type=RAGDeps,
    system_prompt='''
    You are a helpful assistant with access to a knowledge base.
    Use the retrieve tool to find relevant information before answering.
    Always cite the source URL when referencing retrieved content.
    ''',
)


@agent.tool
async def retrieve(
    ctx: RunContext[RAGDeps],
    query: str,
    filters: dict | None = None,
) -> str:
    """检索知识库
    
    Args:
        query: 搜索查询
        filters: 可选过滤条件 (source, tags, date_range)
    
    Returns:
        相关文档内容，包含来源信息
    """
    with logfire.span('retrieve', query=query, filters=filters):
        # 1. 生成查询向量
        embedding_result = await ctx.deps.embedder.embed_query(query)
        embedding = embedding_result[0]
        embedding_json = pydantic_core.to_json(embedding).decode()
        
        # 2. 初步检索 (获取候选)
        sql = '''
            SELECT 
                d.id, d.title, d.url, d.metadata,
                c.chunk_index, c.content,
                1 - (c.embedding <=> $1::vector) as similarity
            FROM document_chunks c
            JOIN documents d ON c.doc_id = d.id
            WHERE 1=1
            ORDER BY c.embedding <=> $1::vector
            LIMIT $2
        '''
        params = [embedding_json, ctx.deps.rerank_top_n]
        
        # 应用过滤
        if filters:
            if 'source' in filters:
                sql = sql.replace('WHERE 1=1', 'WHERE d.url LIKE $3')
                params.append(f'%{filters["source"]}%')
            if 'tags' in filters:
                sql = sql.replace('WHERE 1=1', 
                    "WHERE d.metadata->'tags' @> $4::jsonb")
                params.append(pydantic_core.to_json(filters['tags']).decode())
        
        rows = await ctx.deps.pool.fetch(sql, *params)
        logfire.info('initial retrieval: {len(rows)} candidates')
        
        # 3. 重排序 (可选：基于 BM25 或 Cross-Encoder)
        reranked = _rerank_candidates(query, rows)
        
        # 4. 取 Top K
        top_results = reranked[:ctx.deps.top_k]
        
        # 5. 格式化输出
        context = '\n\n---\n\n'.join([
            f'## Source: {r["url"]}\n'
            f'### Title: {r["title"]}\n'
            f'Relevance: {r["similarity"]:.3f}\n\n'
            f'{r["content"]}'
            for r in top_results
        ])
        
        logfire.info('final context length: {len(context)} chars')
        return context


def _rerank_candidates(query: str, candidates: list) -> list:
    """简单的重排序逻辑"""
    # 可以替换为更复杂的重排序模型
    scored = []
    query_terms = set(query.lower().split())
    
    for row in candidates:
        score = row['similarity']
        # 关键词匹配加分
        content_terms = set(row['content'].lower().split())
        overlap = len(query_terms & content_terms) / max(len(query_terms), 1)
        score += overlap * 0.2
        scored.append({**row, 'final_score': score})
    
    return sorted(scored, key=lambda x: x['final_score'], reverse=True)


# 文档索引
async def index_document(
    doc: Document,
    embedder: Embedder,
    pool: asyncpg.Pool,
    chunk_size: int = 500,
):
    """索引单个文档"""
    with logfire.span('index_document', url=doc.url):
        # 1. 插入文档
        doc_id = await pool.fetchval(
            '''
            INSERT INTO documents (title, content, url, metadata)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (url) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata
            RETURNING id
            ''',
            doc.title, doc.content, doc.url, doc.metadata,
        )
        
        # 2. 分块
        chunks = _chunk_text(doc.content, chunk_size)
        
        # 3. 批量 Embedding
        embeddings = await embedder.embed_documents(chunks)
        
        # 4. 插入块
        await pool.execute('DELETE FROM document_chunks WHERE doc_id = $1', doc_id)
        
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings.embeddings)):
            await pool.execute(
                '''
                INSERT INTO document_chunks (doc_id, chunk_index, content, embedding)
                VALUES ($1, $2, $3, $4)
                ''',
                doc_id, i, chunk, pydantic_core.to_json(emb).decode(),
            )
        
        logfire.info('indexed {len(chunks)} chunks')


def _chunk_text(text: str, size: int) -> list[str]:
    """简单分块"""
    chunks = []
    for i in range(0, len(text), size):
        chunks.append(text[i:i+size])
    return chunks


# 使用示例
async def main():
    # 初始化
    embedder = Embedder(
        'openai:text-embedding-3-small',
        settings=EmbeddingSettings(dimensions=512),  # 降维优化
    )
    pool = await asyncpg.create_pool(
        'postgresql://localhost/rag_db',
        min_size=5,
        max_size=20,
    )
    
    deps = RAGDeps(embedder=embedder, pool=pool)
    
    # 索引文档
    doc = Document(
        id=0,
        title='Pydantic AI Guide',
        content='...',
        url='https://ai.pydantic.dev/docs/',
        metadata={'tags': ['python', 'ai', 'agent']},
    )
    await index_document(doc, embedder, pool)
    
    # 查询
    result = await agent.run(
        'How to build an agent?',
        deps=deps,
    )
    print(result.output)


import asyncio
asyncio.run(main())
```

---

## 八、总结

Pydantic AI 的 RAG 设计提供了灵活的选择：

| 方式 | 优势 | 劣势 | 适用场景 |
|------|------|------|---------|
| **自定义工具** | 完全可控、可观测、可定制 | 需自建基础设施 | 生产环境、私有数据 |
| **Embedder 统一接口** | 多 Provider、可切换 | 需自建索引 | 多模型场景 |
| **FileSearchTool** | 无需基础设施、快速上手 | Provider 依赖、数据托管 | 快速原型、用户上传 |

**设计原则**：
- 小规模/原型：用 FileSearchTool
- 生产环境：用自定义工具 + 可观测性
- 多 Provider：用统一 Embedder 接口
- 专业领域：用领域专用 Embedding 模型
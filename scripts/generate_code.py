#!/usr/bin/env python3
"""
HCI智能排障平台 - 代码生成脚本
批量生成所有必要的代码文件
"""

import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent

def write_file(file_path: str, content: str):
    """写入文件"""
    full_path = PROJECT_ROOT / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✓ 已创建: {file_path}")

def generate_shared_schemas():
    """生成shared schemas"""
    content = '''"""
Shared Pydantic Schemas
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum

class CaseStatus(str, Enum):
    """工单状态"""
    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class MessageRole(str, Enum):
    """消息角色"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"

class CaseCreate(BaseModel):
    """创建工单请求"""
    client_id: str = Field(..., description="客户端ID")
    title: str = Field(..., max_length=200, description="工单标题")
    description: Optional[str] = Field(None, description="工单描述")

class CaseResponse(BaseModel):
    """工单响应"""
    case_id: str
    client_id: str
    status: CaseStatus
    title: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime]
    trace_id: Optional[str]
    
    class Config:
        from_attributes = True

class MessageCreate(BaseModel):
    """创建消息请求"""
    case_id: str
    role: MessageRole
    content: str
    metadata: Optional[dict] = None

class MessageResponse(BaseModel):
    """消息响应"""
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    metadata: Optional[dict]
    created_at: datetime
    trace_id: Optional[str]
    
    class Config:
        from_attributes = True

class WebSocketMessage(BaseModel):
    """WebSocket消息格式"""
    type: str
    case_id: str
    content: str
    is_complete: bool = False
    metadata: Optional[dict] = None
'''
    write_file("backend/shared/models/schemas.py", content)

def generate_case_model():
    """生成Case模型"""
    content = '''"""
Case数据模型
"""

from sqlalchemy import Column, String, Text, Enum as SQLEnum, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid
import enum
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin

class CaseStatus(str, enum.Enum):
    """工单状态枚举"""
    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class Case(Base, TimestampMixin, TraceableMixin):
    """工单表"""
    
    __tablename__ = "cases"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(50), unique=True, nullable=False, index=True)
    client_id = Column(String(100), nullable=False, index=True)
    status = Column(SQLEnum(CaseStatus), default=CaseStatus.CREATED, nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    closed_at = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<Case(case_id={self.case_id}, status={self.status})>"
'''
    write_file("backend/case-service/app/models/case.py", content)

def generate_case_repository():
    """生成Case Repository"""
    content = '''"""
Case Repository
"""

from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from ..models.case import Case, CaseStatus

class CaseRepository:
    """工单数据访问层"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(self, case: Case) -> Case:
        """创建工单"""
        self.session.add(case)
        await self.session.flush()
        await self.session.refresh(case)
        return case
    
    async def get_by_id(self, case_id: str) -> Optional[Case]:
        """根据case_id查询工单"""
        result = await self.session.execute(
            select(Case).where(Case.case_id == case_id)
        )
        return result.scalar_one_or_none()
    
    async def get_by_client_id(self, client_id: str) -> List[Case]:
        """根据client_id查询工单列表"""
        result = await self.session.execute(
            select(Case)
            .where(Case.client_id == client_id)
            .order_by(Case.created_at.desc())
        )
        return list(result.scalars().all())
    
    async def update_status(
        self, 
        case_id: str, 
        status: CaseStatus,
        trace_id: Optional[str] = None
    ) -> Optional[Case]:
        """更新工单状态"""
        case = await self.get_by_id(case_id)
        if not case:
            return None
        
        case.status = status
        if status == CaseStatus.CLOSED:
            case.closed_at = datetime.utcnow()
        if trace_id:
            case.trace_id = trace_id
        
        await self.session.flush()
        await self.session.refresh(case)
        return case
    
    async def delete(self, case_id: str) -> bool:
        """删除工单"""
        case = await self.get_by_id(case_id)
        if not case:
            return False
        
        await self.session.delete(case)
        await self.session.flush()
        return True
'''
    write_file("backend/case-service/app/repositories/case_repo.py", content)

def generate_case_service():
    """生成Case Service"""
    content = '''"""
Case Service - 业务逻辑层
"""

from typing import List, Optional
from datetime import datetime

from ..models.case import Case, CaseStatus
from ..repositories.case_repo import CaseRepository

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.models.schemas import CaseCreate, CaseResponse
from shared.utils.logger import get_logger

logger = get_logger("case-service")

class CaseService:
    """工单业务服务"""
    
    def __init__(self, repository: CaseRepository):
        self.repository = repository
    
    def _generate_case_id(self) -> str:
        """生成工单ID: Q + YYYYMMDD + 5位序号"""
        from datetime import datetime
        import random
        date_str = datetime.utcnow().strftime("%Y%m%d")
        seq = str(random.randint(0, 99999)).zfill(5)
        return f"Q{date_str}{seq}"
    
    async def create_case(
        self, 
        case_create: CaseCreate,
        trace_id: Optional[str] = None
    ) -> CaseResponse:
        """创建新工单"""
        case_id = self._generate_case_id()
        
        case = Case(
            case_id=case_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.CREATED,
            trace_id=trace_id
        )
        
        created_case = await self.repository.create(case)
        
        logger.info(
            event="case_created",
            message=f"Created case {case_id}",
            case_id=case_id,
            client_id=case_create.client_id,
            trace_id=trace_id
        )
        
        return CaseResponse.model_validate(created_case)
    
    async def get_case(self, case_id: str) -> Optional[CaseResponse]:
        """获取工单详情"""
        case = await self.repository.get_by_id(case_id)
        if not case:
            return None
        return CaseResponse.model_validate(case)
    
    async def list_cases(self, client_id: str) -> List[CaseResponse]:
        """获取客户端的所有工单"""
        cases = await self.repository.get_by_client_id(client_id)
        return [CaseResponse.model_validate(case) for case in cases]
    
    async def confirm_case(
        self, 
        case_id: str,
        trace_id: Optional[str] = None
    ) -> Optional[CaseResponse]:
        """确认工单"""
        case = await self.repository.update_status(
            case_id, 
            CaseStatus.CONFIRMED,
            trace_id
        )
        if not case:
            return None
        
        logger.info(
            event="case_confirmed",
            message=f"Confirmed case {case_id}",
            case_id=case_id,
            trace_id=trace_id
        )
        
        return CaseResponse.model_validate(case)
    
    async def close_case(
        self, 
        case_id: str,
        trace_id: Optional[str] = None
    ) -> Optional[CaseResponse]:
        """关闭工单"""
        case = await self.repository.update_status(
            case_id, 
            CaseStatus.CLOSED,
            trace_id
        )
        if not case:
            return None
        
        logger.info(
            event="case_closed",
            message=f"Closed case {case_id}",
            case_id=case_id,
            trace_id=trace_id
        )
        
        return CaseResponse.model_validate(case)
'''
    write_file("backend/case-service/app/services/case_service.py", content)

def main():
    """主函数"""
    print("=" * 60)
    print("开始生成HCI智能排障平台代码...")
    print("=" * 60)
    
    # 阶段1: Shared模块
    print("\n【阶段1】生成 Shared 模块...")
    generate_shared_schemas()
    
    # 阶段2: Case Service
    print("\n【阶段2】生成 Case Service...")
    generate_case_model()
    generate_case_repository()
    generate_case_service()
    
    print("\n" + "=" * 60)
    print("代码生成完成！")
    print("=" * 60)

if __name__ == "__main__":
    main()

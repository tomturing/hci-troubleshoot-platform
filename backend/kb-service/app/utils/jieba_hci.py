"""
jieba + HCI 自定义词典初始化工具

初始化说明：
- jieba 加载 HCI 专业术语词典，提升 BM25 检索的中文分词质量
- 词典路径优先使用 data-pipeline/config/hci_dict.txt，不存在时使用内置词条
- 分词结果用于构建 tsvector（to_tsvector('simple', space_separated_tokens)）

注意事项：
- jieba 首次加载词典较慢（~1s），建议在服务启动时预热
- hci_dict.txt 格式：词语 词频 词性，如 "虚拟存储 5 n"
"""

import logging
import os

logger = logging.getLogger(__name__)

# HCI 领域内置词条（当外部词典文件不存在时作为 fallback）
_BUILTIN_HCI_TERMS = [
    # 存储相关
    "虚拟存储", "虚拟磁盘", "存储池", "数据分片", "冗余度", "快照回滚",
    "分布式存储", "数据重删", "压缩率", "IO延迟", "IOPS",
    # 网络相关
    "物理网卡", "虚拟网卡", "网络平面", "业务网络", "存储网络",
    "vxlan", "bond", "peth0", "eth0", "br_storage", "br_vm",
    # 计算相关
    "嵌套虚拟化", "CPU超配", "内存超配", "KVM", "QEMU", "libvirt",
    "kvm_intel", "kvm_amd", "pflash", "BIOS固件",
    # HCI 平台
    "超融合", "集群锁", "管理节点", "控制节点", "工作节点",
    "AOS", "AHV", "acli", "ncli", "afs", "nutanix",
    # 虚拟机操作
    "开机自检", "冷迁移", "热迁移", "快照", "克隆", "模板机",
    "挂载", "卸载", "磁盘扩容", "内存热添加",
    # 故障相关
    "服务降级", "脑裂", "节点掉线", "磁盘坏块", "IO超时", "BSOD",
]


def init_jieba() -> None:
    """初始化 jieba，加载 HCI 自定义词典

    全局调用一次，建议在 KB Service 启动时执行预热。
    """
    try:
        import jieba

        # 1. 尝试加载外部词典文件（data-pipeline/config/hci_dict.txt）
        dict_path = os.environ.get("HCI_JIEBA_DICT", "/data/config/hci_dict.txt")
        if os.path.exists(dict_path):
            jieba.load_userdict(dict_path)
            logger.info("已加载 jieba 自定义词典: %s", dict_path)
        else:
            # 2. 回退到内置词条
            for term in _BUILTIN_HCI_TERMS:
                jieba.add_word(term)
            logger.info("已加载 jieba 内置 HCI 词条 (%d 条)", len(_BUILTIN_HCI_TERMS))

        # 3. 预热（避免首次分词延迟影响请求响应时间）
        jieba.cut("超融合虚拟机开机失败")
        logger.info("jieba 预热完成")

    except ImportError:
        logger.warning("jieba 未安装，BM25 全文检索将降级为 simple 分词（英文效果）")


def segment(text: str) -> str:
    """使用 jieba 对文本分词，返回空格分隔的 token 字符串

    用途：生成 tsvector 的输入，格式: 'token1 token2 token3 ...'

    Args:
        text: 原始中文文本

    Returns:
        空格分隔的分词结果，用于传入 to_tsvector('simple', ...)
    """
    try:
        import jieba

        # 精确模式分词，过滤空白 token
        tokens = [t.strip() for t in jieba.cut(text, cut_all=False) if t.strip()]
        return " ".join(tokens)
    except ImportError:
        # jieba 不可用时直接返回原文（英文词句可直接用 simple 分词）
        return text

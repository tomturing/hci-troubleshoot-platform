"""
data-pipeline/raw_to_sop — Raw Graph JSON → SOPNode Markdown ETL 转化工具

外部 AI Pipeline 产出的 Raw Graph JSON（扁平图/状态机结构）转化为
符合 sop_parser 规范的 SOP Markdown 文档，通过 /api/sop/ingest API 入库。

用法：
    cd data-pipeline
    python -m raw_to_sop --file raw/内存ECC故障.json --dry-run
    python -m raw_to_sop --file raw/内存ECC故障.json --category-id "硬件-内存"
"""

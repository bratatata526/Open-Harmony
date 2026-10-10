"""agri_logger —— OpenHarmony 系统日志智能聚合分析工具

目录结构（对应 README §3 的三人分工）：
    parser/       B 同学：解析层（fields.py 是设备端契约的可执行版本）
    collector/    B 同学：采集层
    pipeline/     B 同学：过滤、模板挖掘、窗口聚合、存储
    detector/     C 同学：关键词库、异常打分
    clustering/   C 同学：语义聚类
    analyzer/     C 同学：LLM 调用、提示词、根因分析、因果关联
    notifier/     C 同学：告警策略与推送
    report/       C 同学：报告渲染
"""

__version__ = "0.1.0"

# -*- coding: utf-8 -*-
"""规则模型常量与词汇表（RuleSpec 2.0）。

设计要点：
- 规则域 10 个（同类合并 merge 已按需求移除，不在任何枚举中出现）；
- 每个输出属性有唯一归属域（消灭平行表的结构性保证）；
- 属性词汇表固定，拼写错误在门禁 G1/G3 层被拦截。
"""

# 规则域（顺序即 manifest 默认展示顺序；执行顺序见 engine 流水线）
DOMAINS = (
    "filter",      # 实体过滤
    "normalize",   # 名称归一化
    "gr",          # GR 分类（零件级 + 规格级）
    "spec",        # 输出规格（BOM 打印用；命中则把测量规格改写为型号）
    "material",    # 材质 / 热处理
    "remark",      # 加工备注（主备注 + 追加）
    "companion",   # 配套件
    "number",      # 零件编号
    "measure",     # 测量控制
    "purchase",    # 外购数量
)

SCOPES = ("global", "group", "part", "spec", "companion")

# 匹配算子（OPS 固定，门禁 G1 校验）
# keyword = 命名规则关键词：零件名按分割符（空格/逗号/斜杠/竖线/顿号，含全角）分词，
#           规则值须等于某个【完整词】才命中（完全命中=命名规则命中，不误伤子串）。
OPS = ("eq", "contains", "prefix", "suffix", "regex", "in", "range", "exists", "keyword")

STATUSES = ("draft", "active", "deprecated", "retired")

PRIORITY_DEFAULT = 500
PRIORITY_MIN, PRIORITY_MAX = 0, 1000

# when 可用字段（属性词汇表，固定不变）
WHEN_FIELDS = (
    "part.name",          # 零件原始名（Body 名）
    "part.workingName",   # 归一化后的工作名
    "part.material",      # 已有材质（上游写入）
    "part.group",         # 所属分组名
    "spec.value",         # 规格字符串（测量后可得）
    "spec.count",         # 实体数
    "spec.hasMeasured",   # 是否已测量
    "gr",                 # 当前 GR（gr 域之后对下游域可见）
    "quantity",           # 数量
    "input.skipBody",     # filter 中间产物
    "input.skipReason",
)

# 域 → 授权 then 属性（唯一归属）
OWNERSHIP = {
    "filter":    ("input.skipBody", "input.skipReason"),
    "normalize": ("part.workingName", "part.aliases"),
    "gr":        ("gr",),
    "spec":      ("outputSpec",),
    "material":  ("material", "heatTreatment"),
    "remark":    ("remark", "remarkAppend"),
    "companion": ("companions", "suppressCompanions", "companionGrPolicy"),
    "number":    ("numberRange", "numberPrefix"),
    "measure":   ("skipMeasurement", "skipReason"),
    "purchase":  ("purchaseFixedQty",),
}

# 编辑器类型化输入（value 编辑器分派用）
ATTR_KINDS = {
    "gr": "str", "material": "str", "heatTreatment": "str", "remark": "strtext",
    "outputSpec": "str",
    "remarkAppend": "strlist", "companions": "companions",
    "suppressCompanions": "bool", "companionGrPolicy": "enum:follow-part|warehouse",
    "input.skipBody": "bool", "input.skipReason": "str",
    "part.workingName": "str", "part.aliases": "strlist",
    "skipMeasurement": "bool", "skipReason": "str",
    "purchaseFixedQty": "int", "numberRange": "range", "numberPrefix": "str",
}

# 特异性权重（条件字段越具体权重越高）
SPECIFICITY_WEIGHTS = {
    "spec.value": 40, "spec.count": 40, "spec.hasMeasured": 40,
    "gr": 30,
    "part.name": 25, "part.workingName": 25,
    "part.material": 20, "part.group": 15,
    "quantity": 10,
}
SPECIFICITY_OTHER = 5

# 规则 id 规范：<domain>.<category>.<scope>.<seq>
ID_RE = (r"^(filter|normalize|gr|spec|material|remark|companion|number|measure|purchase)"
         r"\.[a-z0-9-]{1,24}\.(global|group|part|spec|companion)\.[0-9]{3}$")

# 配套件 GR 策略
COMPANION_GR_POLICIES = ("follow-part", "warehouse")

# 常见 GR 建议值（下拉预填，可手输）
GR_SUGGESTIONS = ("仓库备件", "模架", "自制件", "镶配件", "小零件",
                  "标准件", "外购件", "热流道", "隔水片", "油缸")

# 规则集文件目录名
RULES_DIRNAME = "rules"
CORPUS_DIRNAME = "corpus"
SNAPSHOTS_DIRNAME = "snapshots"

# -*- coding: utf-8 -*-
"""MoldBOM — 模具 BOM 导出工具（公共入口 / 兼容门面）。

功能概述：
  1. 读取 CATPart 中每个 Body 的名称作为零部件名
  2. 每个 Body 独立导出 STP，解析 MANIFOLD_SOLID_BREP 计数实际零件数量
  3. 按 V2 规则引擎推理 GR 名 / 材质 / 热处理 / 加工备注（唯一规则源）
  4. 规格测量（geometry_engine）：STP 拓扑 BFS 取顶点 → PCA/DE/NM 形状分析
  5. 配套紧固件自动补全 + 零件号自动分配
  6. 支持 GitHub 自动更新（exe 全量 + 规则热更新）

2026-08-18 重构：本文件不再是单体实现，而是「门面」——把拆分后的各
功能模块（bom_common / bom_utils / bom_infer / bom_stp / bom_measure /
bom_parser / bom_numbering / bom_companions / bom_writer / bom_split /
bom_pipeline / bom_batch / bom_catia / bom_cli）的公共 API 统一再导出，
保证 `import bom_export` 与历史 `from bom_export import ...` 完全兼容。

用法：
  python bom_export.py <CATPart路径> [输出Excel路径]
  python bom_export.py --batch <文件夹路径>       # 批量处理
"""

from bom_common import (  # noqa: F401  —— 基础设施
    __version__, log, _app_dir, _log_dir, _resolve_log_file,
    DEFAULT_GR, DEFAULT_NUM_RANGE, MOLD_NUMBER_STRIP,
)
from bom_utils import (  # noqa: F401  —— 通用工具
    extract_mold_number, _safe_name, _group_by_gr,
)
from bom_infer import (  # noqa: F401  —— GR 推理
    infer_gr_and_detail, _apply_spec_gr_v2,
)
from bom_stp import (  # noqa: F401  —— STP 导出与计数
    count_solids_in_stp, copy_body_to_new_part, export_body_to_stp_and_count,
)
from bom_measure import (  # noqa: F401  —— 规格测量
    fill_specs_from_stp, _format_spec_counts, _measure_one_spec,
    extract_aabb_from_stp, _cleanup_stp_artifacts,
)
from bom_parser import (  # noqa: F401  —— CATPart 解析
    parse_catpart, _show_all_bodies,
)
from bom_numbering import assign_part_numbers  # noqa: F401
from bom_companions import (  # noqa: F401  —— 配套补全
    add_companions, _to_int, _acc_companion, _make_companion,
)
from bom_writer import (  # noqa: F401  —— 输出
    write_bom, write_bom_excel, write_bom_csv, write_bom_by_gr, BOM_COLUMNS,
)
from bom_split import export_split_parts  # noqa: F401
from bom_pipeline import (  # noqa: F401  —— Pipeline
    register_stage, stage, run_pipeline, _PIPELINE_STAGES, DEFAULT_STAGES,
    default_ctx, process_one_part,
)
from bom_batch import batch_process  # noqa: F401
from bom_catia import (  # noqa: F401  —— CATIA 会话 / 缓存清理
    connect_catia, _setup_catia_session, _restore_catia_session, cleanup_stale_cache,
)
from bom_cli import main  # noqa: F401

import geometry_engine  # noqa: F401  —— 规格测量引擎（测试经 m.geometry_engine 访问）


if __name__ == "__main__":
    # 2026-07-31 修复: frozen exe 多进程规格测量必需——Windows spawn 的子进程
    # 会重新启动 exe（--multiprocessing-fork），freeze_support 负责拦截并进入
    # worker 模式；缺失时子进程会执行 main()（无参数→弹新 GUI），规格测量失效。
    import multiprocessing
    multiprocessing.freeze_support()
    main()

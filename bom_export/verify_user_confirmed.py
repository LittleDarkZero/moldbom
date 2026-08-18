# -*- coding: utf-8 -*-
"""规格测量引擎回归验证 —— 对照用户确认基准（TEST/规格测量基准_用户确认.json）

用法: python verify_user_confirmed.py
50 个点云全量分析（约 15-20s），任何输出与用户确认值不符即打印差异并以非零码退出。

比较口径（2026-08-13 与 format_spec 变更对齐）:
  1. 规格字符串归一化: ×→*、全角→半角、去空白、整数尾缀 .0 归一去尾
  2. 数值四舍五入取整: 测量值 69.5 → 70 与基准整数规格对齐
     （format_spec 保留一位小数是为薄片防丢厚度，基准比较按取整业务口径）
  3. 前缀型期望（如 "Φ15×"）按前缀匹配
"""
import sys, os, json, glob, re
BASE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.join(BASE, 'TEST')
sys.path.insert(0, BASE)
import geometry_engine as ge


def norm_spec(s):
    """规格字符串归一化：×→*、去空白、整数尾缀 .0 归一去尾。"""
    s = s.replace('×', '*').replace(' ', '')
    s = re.sub(r'(?<=\d)\.0(?![0-9.])', '', s)
    return s


def cmp_round(s):
    """数值四舍五入取整（与用户确认基准的整数规格对齐）。"""
    return re.sub(r'\d+(?:\.\d+)?',
                  lambda m: str(int(round(float(m.group(0))))), s)


def main():
    bench_path = os.path.join(TEST_DIR, '规格测量基准_用户确认.json')
    with open(bench_path, encoding='utf-8') as f:
        bench = json.load(f)

    # 展开基准: (folder, base_name, expected_prefix)
    expected = []
    for e in bench['expected']:
        fps = sorted(glob.glob(os.path.join(TEST_DIR, e['folder'], e['name'] + '*.json')))
        if len(fps) != e['count']:
            print('⚠️ 文件数不符: %s/%s 期望 %d 实际 %d' % (e['folder'], e['name'], e['count'], len(fps)))
        for fp in fps:
            expected.append((fp, e['spec']))

    passed = failed = 0
    diffs = []
    for fp, exp_prefix in expected:
        with open(fp, encoding='utf-8') as f:
            d = json.load(f)
        a = ge.analyze_points(d.get('data') or d.get('points'))
        spec = ge.format_spec(a) if a else '退化'
        shape = a['shape_en'] if a else '-'
        exp = norm_spec(exp_prefix)
        got = norm_spec(spec)
        ok = False
        if a is not None:
            exp_shape = 'cylinder' if exp.startswith('Φ') else 'box'
            if shape != exp_shape:
                ok = False
            elif exp.endswith('*'):   # 前缀型期望（如 Φ15*）
                ok = cmp_round(got).startswith(cmp_round(exp))
            else:
                ok = cmp_round(got) == cmp_round(exp)
        if ok:
            passed += 1
        else:
            failed += 1
            diffs.append((os.path.basename(fp), exp_prefix, spec,
                          a['shape_cn'] if a else '-', (a or {}).get('decision', '-')))

    print('=== 用户确认基准回归: %d 通过 / %d 失败 ===' % (passed, failed))
    for name, exp, spec, shape, decision in diffs:
        print('  ✗ %-28s 期望:%-12s 实测:%s %s (%s)'
              % (name, exp, spec, shape, decision))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

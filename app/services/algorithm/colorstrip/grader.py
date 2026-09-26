"""色卡比色判定：一张图里同时拍到参考色卡与试纸，判试纸是否合格。

  img = imdecode(raw_bytes)            # 解不出图抛 ValueError
  res = grade(img, cfg=config.load())  # cfg 不传即 params.yaml 的 default_profile
  res['ok'], res['code'], res['strips'][0]['passed'], res['log']

两条会静默出错的约束：

- **cv2 只许在函数体内 import**（规范 §2：L2 依赖禁止模块顶层 import）。本模块经
  `services/algorithm/service.py` 被 `routers/algorithm.py` 模块级 import，挪回顶层会让
  `app.main` 的导入预算失守。
- **别再加绝对色窗口去堵伪造**：试过两次（先 L\\* 后色相），都挡不住模仿真实色值的那一种。
  职责边界已划定为「防误操作、不防蓄意伪造」，见
  `docs/update/20260920_COLORSTRIP_API.md` 的「已知缺口」。

阈值取值与调参规矩在 [params.yaml](params.yaml) 的注释里（本文件不留默认值副本）；
判据由来、对抗用例与已知缺陷见验收工装的 REPORT.md（`app/services/temp/colorstrip/`，不在仓库）。
"""
from __future__ import annotations

import itertools
import math
import os

import numpy as np

from . import config as _config
from .types import (
    E_CARD_AMBIGUOUS,
    E_NO_CARD,
    E_STRIP_COUNT,
    E_TOO_FEW_PATCHES,
    OK,
)

__all__ = [
    'OK', 'E_TOO_FEW_PATCHES', 'E_NO_CARD', 'E_CARD_AMBIGUOUS', 'E_STRIP_COUNT',
    'imdecode', 'imread', 'imwrite', 'segment', 'find_card', 'grade', 'draw',
]


def imdecode(data: bytes):
    """内存字节 -> BGR ndarray。解不出图抛 ValueError（调用方负责翻成 HTTP 400）。"""
    import cv2

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('无法解码图片：不是受支持的图片格式，或数据已损坏')
    return img


def imread(path):
    """读图。走 imdecode 而非 cv2.imread —— 后者在非 ASCII 路径上返回 None（本仓库路径含中文）"""
    try:
        return imdecode(np.fromfile(str(path), dtype=np.uint8).tobytes())
    except ValueError as e:
        raise OSError(f'无法解码图片: {path}') from e


def imwrite(path, img):
    """落盘。同 imread，绕开非 ASCII 路径"""
    import cv2

    path = str(path)
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or '.jpg', img)
    if not ok:
        raise OSError(f'无法编码图片: {path}')
    buf.tofile(path)


def scale_of(img, cfg=None):
    """分割工作尺度：长边归一化到 cfg.target_long。返回缩放比（原图 -> 工作图）"""
    cfg = cfg or _config.load()
    return cfg.target_long / max(img.shape[:2])


def segment(img, cfg=None):
    """通道区间过滤 -> 形态学 -> 连通域。返回色块列表（坐标为原图尺度）

    长边先归一化：固定倍率降采样（旧实现 SCALE=4）在低分辨率图上会把色卡两块用闭运算
    粘成一块（case4 实测），归一化后形态学核才是分辨率无关量。
    """
    import cv2

    cfg = cfg or _config.load()
    h, w = img.shape[:2]
    s = scale_of(img, cfg)
    small = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (((H <= cfg.hue_band_low_max) | (H >= cfg.hue_band_high_min))
            & (S >= cfg.sat_min)
            & (V >= cfg.val_min) & (V <= cfg.val_max)).astype(np.uint8) * 255

    k = np.ones((cfg.morph_kernel, cfg.morph_kernel), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=cfg.morph_close_iters)

    n, cc, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
    min_area = cfg.min_area_ratio * small.shape[0] * small.shape[1]

    patches = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < min_area:
            continue
        m = cc == i
        a = float(np.median(lab[..., 1][m])) - 128
        b = float(np.median(lab[..., 2][m])) - 128
        patches.append(dict(
            cx=float(cent[i][0]) / s, cy=float(cent[i][1]) / s,
            w=round(int(bw) / s), h=round(int(bh) / s),
            bbox=(round(int(x) / s), round(int(y) / s),
                  round(int(bw) / s), round(int(bh) / s)),
            area=round(int(area) / s / s),
            L=float(np.median(lab[..., 0][m])) * 100 / 255,
            a=a, b=b,
            hue=math.degrees(math.atan2(b, a)),   # Lab 色相角：浓度越高越偏红（角度越小）
            C=math.hypot(a, b),                   # 彩度，仅供诊断
            role='unknown',
        ))
    return mask, patches


def pair_features(p, q):
    """色卡成对结构的四个判据（全为比值，与分辨率/拍摄距离解耦）

    先定"配对主轴"——两块在哪个方向上分开得更远，那就是它们的排列方向；
    再沿主轴量 gap（紧邻）、沿垂直方向量 align（对齐）。这样竖着拍和横着拍都认，
    不把判据锁死在图像的竖直方向上。
    """
    mw, mh = (p['w'] + q['w']) / 2, (p['h'] + q['h']) / 2
    px, py, pw, ph = p['bbox']
    qx, qy, qw, qh = q['bbox']
    dx, dy = abs(p['cx'] - q['cx']) / mw, abs(p['cy'] - q['cy']) / mh
    if dy >= dx:                                          # 上下排列
        axis, align = 'v', dx
        gap = max(0, max(py, qy) - min(py + ph, qy + qh)) / mh
    else:                                                 # 左右排列
        axis, align = 'h', dy
        gap = max(0, max(px, qx) - min(px + pw, qx + qw)) / mw
    return dict(
        axis=axis,
        align=align,
        gap=gap,
        area=min(p['area'], q['area']) / max(p['area'], q['area']),
        dL=abs(p['L'] - q['L']),
    )


def check_pair(p, q, cfg=None):
    """对一组色块算全部判据，返回 (deep, light, 特征, 未通过的判据列表)

    判据分两类，都与色卡/试纸的相对位置、以及整图朝向无关：
      结构 —— 沿配对主轴紧邻、垂直方向对齐、等大、ΔL*≈16；
      颜色 —— 两块的 Lab 色相角各自落在配置窗口内，且深块必须同时是色相更低的那块。
    早期还有一条"色卡整体位于其余色块左侧"的构图约束，已删除（REPORT.md §3）。

    落选原因逐条留下来而不是短路返回：拒判时要能告诉调参的人卡在哪条、实测值多少。
    """
    cfg = cfg or _config.load()
    f = pair_features(p, q)
    deep, light = min(p, q, key=lambda t: t['L']), max(p, q, key=lambda t: t['L'])
    f['hue_deep'], f['hue_light'] = deep['hue'], light['hue']
    fails = []
    if f['align'] > cfg.align_max:
        fails.append(f'align {f["align"]:.2f}>{cfg.align_max:g}')
    if f['gap'] > cfg.gap_max:
        fails.append(f'gap {f["gap"]:.2f}>{cfg.gap_max:g}')
    if f['area'] < cfg.area_min:
        fails.append(f'面积比 {f["area"]:.2f}<{cfg.area_min:g}')
    if not cfg.dl_min <= f['dL'] <= cfg.dl_max:
        fails.append(f'ΔL* {f["dL"]:.1f}∉[{cfg.dl_min:g},{cfg.dl_max:g}]')
    # 一致性：同一染料的两个浓度，深的那块必然也更偏红（色相角更小）。
    # 两轴排序矛盾说明这对不是同一条显色轴上的东西。
    if deep['hue'] >= light['hue']:
        fails.append(f'深块色相 {deep["hue"]:.1f}° 不小于浅块 {light["hue"]:.1f}°')
    if not cfg.hue_2000_min <= deep['hue'] <= cfg.hue_2000_max:
        fails.append(f'深块色相 {deep["hue"]:.1f}°∉[{cfg.hue_2000_min:g},{cfg.hue_2000_max:g}]')
    if not cfg.hue_800_min <= light['hue'] <= cfg.hue_800_max:
        fails.append(f'浅块色相 {light["hue"]:.1f}°∉[{cfg.hue_800_min:g},{cfg.hue_800_max:g}]')
    return deep, light, f, fails


def find_card(patches, cfg=None):
    """正向识别色卡对，返回 [(2000块, 800块, 特征), ...]。深者 = 2000 = 参考下限"""
    return [(d, l, f) for d, l, f, fails in
            (check_pair(p, q, cfg) for p, q in itertools.combinations(patches, 2))
            if not fails]


def near_misses(patches, cfg=None, top=3):
    """落选的候选对，按"差几条判据"排序。只在拒判时进日志，给调参提线索。"""
    rows = [(len(fails), d, l, f, fails) for d, l, f, fails in
            (check_pair(p, q, cfg) for p, q in itertools.combinations(patches, 2))
            if fails]
    rows.sort(key=lambda r: r[0])
    return rows[:top]


def _fail(code, message, patches, log, profile):
    return dict(ok=False, code=code, message=message, ref_L=None, ref_hue=None,
                card=None, strips=[], patches=patches, profile=profile,
                log=log + [f'✗ {message}'])


def _log_near_misses(patches, cfg, log):
    """把最接近的几组候选对与各自的落选原因写进日志——换场景调参主要看这几行"""
    rows = near_misses(patches, cfg)
    if not rows:
        return
    log.append(f'最接近的候选对（共 {len(patches) * (len(patches) - 1) // 2} 组组合）：')
    for _, deep, light, f, fails in rows:
        log.append(f'  深块 L*{deep["L"]:.1f}/色相{deep["hue"]:.1f}° × '
                   f'浅块 L*{light["L"]:.1f}/色相{light["hue"]:.1f}°  '
                   f'卡在 {"、".join(fails)}')


def grade(img, viz_path=None, cfg=None):
    """判定入口。返回结构化结果；viz_path 非空时落盘检测效果图。

    cfg 为 None 时用 colorstrip.yaml 的 default 档。
    """
    cfg = cfg or _config.load()
    mask, patches = segment(img, cfg)
    log = [f'参数档 {cfg.profile}',
           f'检出色块 {len(patches)} 个（mask 占比 {mask.mean() / 255:.3%}）']

    def fail(code, message):
        return _fail(code, message, patches, log, cfg.profile)

    if len(patches) < 2:
        res = fail(E_TOO_FEW_PATCHES, f'色块不足 2 个（{len(patches)}），无法比色')
    else:
        hits = find_card(patches, cfg)
        if not hits:
            _log_near_misses(patches, cfg, log)
            res = fail(E_NO_CARD,
                       '未找到符合色卡结构（对齐/紧邻/等大/ΔL*≈16）且色相落在配置窗口内的'
                       '色块对，参考下限无从确定')
        elif len(hits) > 1:
            for _, _, f in hits:
                log.append(f'  候选色卡对 深{f["hue_deep"]:.1f}°/浅{f["hue_light"]:.1f}° '
                           f'align={f["align"]:.2f} gap={f["gap"]:.2f} '
                           f'面积比={f["area"]:.2f} ΔL*={f["dL"]:.1f}')
            res = fail(E_CARD_AMBIGUOUS, f'找到 {len(hits)} 组疑似色卡对，参考下限有歧义')
        else:
            ref, ref800, f = hits[0]
            ref['role'], ref800['role'] = 'card_2000', 'card_800'
            log += [
                f'色卡识别: 主轴={"上下" if f["axis"] == "v" else "左右"} '
                f'align={f["align"]:.2f} gap={f["gap"]:.2f} '
                f'面积比={f["area"]:.2f} ΔL*={f["dL"]:.1f} '
                f'色相 深{f["hue_deep"]:.1f}°/浅{f["hue_light"]:.1f}° -> 唯一解',
                f'参考下限 = 色卡深块(2000刻度) L*={ref["L"]:.1f} 色相={ref["hue"]:.1f}°   '
                f'[浅块(800) L*={ref800["L"]:.1f} 仅用于识别，不参与判定]',
            ]
            rest = [p for p in patches if p['role'] == 'unknown']

            # 待测色块数必须恰好等于规范值：
            #   少了 -> 试纸未显色(浓度≈0)时是浅白的，压根进不了红橙 mask。没这道门禁，
            #          浓度越低越容易被静默丢掉——最该拦的样本反而最容易漏，方向是反的；
            #   多了 -> 画面混入非试纸的红橙物，不能猜哪个是试纸。
            if len(rest) != cfg.expected_strips:
                why = ('疑试纸未显色/浓度不足，或试纸不在画面内'
                       if len(rest) < cfg.expected_strips else '疑混入非试纸物体')
                for p in rest:
                    log.append(f'  待测块 L*={p["L"]:.1f} 色相={p["hue"]:.1f}° '
                               f'(cx={p["cx"]:.0f},cy={p["cy"]:.0f})')
                res = fail(E_STRIP_COUNT,
                           f'待测色块 {len(rest)} 个，规范要求 {cfg.expected_strips} 个（{why}）')
            else:
                strips = []
                for i, p in enumerate(sorted(rest, key=lambda t: (t['cy'], t['cx']))):
                    p['role'] = 'strip'
                    passed = p['L'] < ref['L']
                    # 色相轴是并行参考，不参与结论：跨样本 800 块 L* 漂 19.6 而色相只漂 5.1°，
                    # 色相对光照梯度更稳（REPORT.md §5），但两条轴都还没有临界真样本可验证。
                    hue_passed = p['hue'] < ref['hue']
                    strips.append(dict(idx=i, cx=p['cx'], cy=p['cy'], L=p['L'],
                                       margin=ref['L'] - p['L'], passed=passed,
                                       hue=p['hue'], hue_margin=ref['hue'] - p['hue'],
                                       hue_passed=hue_passed))
                    log.append(f'  试纸{i} (cx={p["cx"]:.0f},cy={p["cy"]:.0f}) '
                               f'L*={p["L"]:5.1f}  裕度 {ref["L"] - p["L"]:+6.1f}  -> '
                               f'{"合格" if passed else "不合格"}'
                               f'   [色相轴 {p["hue"]:.1f}° 裕度 {ref["hue"] - p["hue"]:+5.1f}° '
                               f'-> {"合格" if hue_passed else "不合格"}]')
                    if hue_passed != passed:
                        log.append(f'  ⚠ 试纸{i} 两轴分歧（L* 判{"合格" if passed else "不合格"}，'
                                   f'色相判{"合格" if hue_passed else "不合格"}），'
                                   f'结论以 L* 轴为准，此处仅记录')
                res = dict(ok=True, code=OK, message='判定完成', ref_L=ref['L'],
                           ref_hue=ref['hue'], card=f, strips=strips, patches=patches,
                           profile=cfg.profile, log=log)

    if viz_path:
        draw(img, mask, res, viz_path, cfg)
    return res


def draw(img, mask, res, path, cfg=None):
    """落盘检测效果图：非 mask 区域压暗，标注每块的角色与判定"""
    import cv2

    s = scale_of(img, cfg)
    vis = cv2.resize(img, (mask.shape[1], mask.shape[0]), interpolation=cv2.INTER_AREA)
    vis[mask == 0] = (vis[mask == 0] * 0.3).astype(np.uint8)

    # 标签刻意写全"CARD"：两块都属于瓶身色卡，只是 2000 那块被用作参考下限、
    # 800 那块只用于识别色卡。早期标成 REF / CARD 会被读成"REF 不是色卡的一部分"。
    style = {
        'card_2000': ('CARD/2000 =REF', (0, 165, 255)),
        'card_800': ('CARD/800 unused', (160, 160, 160)),
        'unknown': ('? unclassified', (255, 0, 255)),
    }
    for p in res['patches']:
        x, y, bw, bh = [round(v * s) for v in p['bbox']]
        if p['role'] == 'strip':
            passed = p['L'] < res['ref_L']
            tag = f'{"PASS" if passed else "FAIL"} L*{p["L"]:.0f} ({res["ref_L"] - p["L"]:+.0f})'
            col = (0, 200, 0) if passed else (0, 0, 255)
        else:
            base, col = style[p['role']]
            tag = f'{base} L*{p["L"]:.0f} h{p["hue"]:.0f}'
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), col, 2)
        cv2.putText(vis, tag, (max(0, x - 40), max(12, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)

    if res['ok']:
        nf = sum(1 for s in res['strips'] if not s['passed'])
        banner = f'OK  {len(res["strips"]) - nf}/{len(res["strips"])} PASS' + (
            f'  {nf} FAIL' if nf else '')
    else:
        banner = f'REJECTED  {res["code"]}'
    col = (0, 0, 255) if not res['ok'] or any(not s['passed'] for s in res['strips']) \
        else (0, 200, 0)
    cv2.putText(vis, banner, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2)
    cv2.putText(vis, f'profile={res.get("profile", "?")}', (12, 52),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    imwrite(path, vis)

"""admin 面板的纯序列化 / 分位数估算逻辑（不走 HTTP，绕过 GatewayMiddleware）。

只测能纯函数化的部分：_client_info（CQ→dict）、_quantile（直方图桶→分位数）、
_parse_metrics_json（REGISTRY→结构化 JSON）。WS/端点/中间件属集成范畴，不在此。
"""

from factories import make_cq
from app.routers.admin import _client_info, _parse_metrics_json, _quantile


# ---- _client_info：换键后载荷含 task_id + source_ip（前端据 source_ip 连 WS）----

def test_client_info_shape_carries_task_id_and_source_ip():
    cq = make_cq(task_id=7, step_id=2, source_ip="1.2.3.4", stage="2")
    info = _client_info(7, cq)
    assert info["client_id"] == 7          # 注册表键 = task_id(int)
    assert info["task_id"] == 7
    assert info["source_ip"] == "1.2.3.4"  # 换键回归修复的字段
    assert info["step_id"] == 2
    assert isinstance(info["queue_depths"], dict)


# ---- _quantile：从 (upper, cumulative_count) 桶插值估分位 ----

def test_quantile_empty_returns_zero():
    assert _quantile([], 0.5, 0) == 0.0


def test_quantile_zero_total_returns_zero():
    assert _quantile([(10.0, 0.0), (float("inf"), 0.0)], 0.5, 0) == 0.0


def test_quantile_interpolates_within_bucket():
    # total=4，q=0.95 → target=3.8 落在 (10,2]→(20,4] 桶：10 + 0.9*(20-10) = 19.0
    buckets = [(10.0, 2.0), (20.0, 4.0), (float("inf"), 4.0)]
    assert _quantile(buckets, 0.95, 4) == 19.0


def test_quantile_first_bucket_boundary():
    # target=2 恰在首桶累计上沿 → 返回该桶上界 10.0
    buckets = [(10.0, 2.0), (20.0, 4.0), (float("inf"), 4.0)]
    assert _quantile(buckets, 0.5, 4) == 10.0


# ---- _parse_metrics_json：真实 REGISTRY 提取（增量真计数器，验 drop 分支）----

def test_parse_metrics_json_returns_dict_and_reads_drop_counter():
    from app.utils.metrics import frame_drop_total

    frame_drop_total.labels(reason="unit_test_probe").inc()
    result = _parse_metrics_json()

    assert isinstance(result, dict)
    # 增了 drop 计数器 → frame_drop_total 家族必然出现且 total >= 1
    assert "frame_drop_total" in result
    assert result["frame_drop_total"]["total"] >= 1
    assert "unit_test_probe" in result["frame_drop_total"]["by_reason"]

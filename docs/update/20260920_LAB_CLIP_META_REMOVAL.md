# 送标去掉元数据字段（LS 从来收不到）

> **变更状态**：已实现（2026-09-20）
> **知识库**：待沉淀
> **相关**：[20260920_MEDIA_AXIS_MIGRATION.md](20260920_MEDIA_AXIS_MIGRATION.md)（同一个 `/lab-f3m8/submit` 契约，上一次改的是区间字段）

## 删的是什么

`/lab-f3m8/submit` 随 clip 上传给 Label Studio 的那份 `meta`（`label`、`task_id`、`step_id`、
`start_ms`/`end_ms`、`start_media_ms`/`end_media_ms`、`source`）**从未到达 LS**，连同前端那一列
`label` 输入框一起删掉。

不是坏了之后修——是这条路从落地起就不通，且**不报错、响应 200、task 正常创建**，所以一直没人发现。

## 为什么不通

发送端（`label_studio_client.py::_build_multipart`）把 `meta` 拼成一个 `name="data"`、
**没有 `filename`** 的 multipart part。没有 filename 的 part 在 Django 侧进 `request.POST`，
不进 `request.FILES`。

LS 的 `POST /api/projects/{id}/import` 调 `load_tasks(request, project)`，分支互斥：

```python
if len(request.FILES):          # ← 我们命中这条
    # 只遍历 request.FILES，为每个文件建 FileUpload，
    # task.data 由文件本身生成，不看 request.POST
elif 'application/x-www-form-urlencoded' in request.content_type: ...
elif 'application/json' in request.content_type: tasks = [request.data]
```

命中第一条就不再看表单字段。这是 LS 的已知限制，不是我们用错 API——
[HumanSignal/label-studio#4381](https://github.com/heartexlabs/label-studio/issues/4381)
问的正是"能不能一次调用同时传文件和元数据"，结论是 multipart 做不到，只能 import 后再 PATCH task。

**旁证**：`label_studio_client.py` 里早就有一段注释说媒体上传常返回
`{"task_count", "file_upload_ids", "import"}` 这种不含 `task_ids` 的形状 —— 那正是
"LS 把请求当纯文件导入"的响应形状，反过来印证走的是 FILES 分支。

**为什么测试没拦住**：`tests/test_lab_*.py` 全部 mock 掉 LS，断言的是"我们发了什么"，
不是"LS 存了什么"。这类 bug 只有真连 LS 看 `task.data` 才暴露。

## 决定：不补救，直接删

补救方案（import 后 `PATCH /api/tasks/{id}` 回填 `data`）需要先解决"媒体上传拿不到 task_id"——
得按 `file_upload_ids` 反查，是一段新代码。**评估后决定不做**：溯源靠人看时间戳够用，
元数据不值得为此改 LS 客户端。

保留的唯一溯源线索：clip 文件名 `clip_{start_ms}_{end_ms}.mp4` 里的绝对墙钟区间，
它会出现在 LS 的 `task.data` video 路径里。

## 改动清单

| 文件 | 改动 |
|---|---|
| `app/routers/lab.py` | 删 `LabClipRange.label` 字段；删 `_process_one` 里构造 `meta` 的 dict；`import_clip` 调用去掉 `meta=`，原地留一句注释说明为何不带 |
| `app/services/lab/label_studio_client.py` | `_build_multipart` 去掉 `meta` 形参与那段 part 拼装；`import_clip` 去掉 `meta` 形参，docstring 改为写明 LS 侧限制 |
| `app/services/lab/clip_builder.py` | 删 `ClipSpec.label` 字段及其 docstring 行 |
| `app/static/lab/index.html` | 删表头 `label（≤64）`、`el-input` 单元格、`addClip` 里的 `label: ''`、提交体里的 `label`；空态 `colspan` 6 → 5 |
| `docs/api/lab.md` | 删 `label` 字段行与 422 触发条件里的 `label>64`；补一条"不收元数据"的说明，讲清 LS 侧限制与唯一溯源线索 |

## 契约影响

`LabClipRange` 少一个**可选**字段。Pydantic 默认忽略未知字段，老调用方继续传 `label`
（哪怕超 64 字符）**不会 422**，只是被静默丢弃 —— 与改动前的实际效果一致（改动前也是丢弃，
只不过丢在 LS 那头）。故对调用方无破坏性。

## 验证

- `pytest tests/` 全绿：827 passed
- 反射核对：`_build_multipart` 签名只剩 `(file_path, file_field)`；`LabClipRange.model_fields`
  与 `ClipSpec` 字段均不含 `label`
- `grep` 确认 `app/routers/lab.py`、`app/services/lab/`、`app/static/lab/index.html` 零残留引用
- **未跑真实 LS 端到端**：本次只删发送侧死代码，不改上传行为本身，LS 上的 task 形态与改动前一致

# 任务回溯 API 文档

## 概述

任务回溯 API 提供完整的历史任务视频和关键点数据访问接口，支持原始视频和处理后视频的查询与流式传输。

## 接口列表

### 1. 获取任务的所有视频段信息

```http
GET /task/traceback/{task_id}/segments?video_type=processed
```

**参数**:
- `task_id` (路径参数): 任务 ID
- `video_type` (查询参数): 视频类型
  - `"processed"` (默认): 处理后的视频（含 AI 标注）
  - `"raw"`: 原始视频

**响应示例**:
```json
{
  "task_id": 123,
  "video_type": "processed",
  "total_segments": 5,
  "playlist_path": "/database/camera_001/task_123/hls/processed_playlist.m3u8",
  "segments": [
    {
      "segment_id": 1,
      "segment_path": "/database/camera_001/task_123/hls/processed_segment_1732273845123456.mp4",
      "start_time": "2025-11-23T10:30:45.123456",
      "end_time": "2025-11-23T10:30:50.678901",
      "client_id": "camera_001",
      "keypoints_path": "/database/camera_001/task_123/hls/keypoints_1732273845123456.json"
    }
  ]
}
```

**使用场景**: 获取任务的所有视频段路径，用于前端展示时间线或批量下载

---

### 2. 获取 HLS 播放列表

```http
GET /task/traceback/{task_id}/playlist?video_type=processed
```

**参数**:
- `task_id` (路径参数): 任务 ID
- `video_type` (查询参数): `"raw"` 或 `"processed"`

**响应**: M3U8 播放列表文件

**使用场景**: 直接在支持 HLS 的播放器中播放完整任务视频

**示例**:
```html
<video controls>
  <source src="http://localhost:8000/task/traceback/123/playlist?video_type=processed" type="application/vnd.apple.mpegurl">
</video>
```

---

### 3. 流式传输单个视频段

```http
GET /task/traceback/{task_id}/video/{segment_id}
```

**参数**:
- `task_id` (路径参数): 任务 ID
- `segment_id` (路径参数): 段 ID (从 `/segments` 接口获取)

**响应**: MP4 视频文件流

**使用场景**: 下载或播放指定时间段的视频

**示例**:
```bash
# 下载单个视频段
curl -o segment_1.mp4 "http://localhost:8000/task/traceback/123/video/1"
```

---

### 4. 获取单个视频段的关键点数据

```http
GET /task/traceback/{task_id}/keypoints/{segment_id}
```

**参数**:
- `task_id` (路径参数): 任务 ID
- `segment_id` (路径参数): 段 ID

**响应示例**:
```json
[
  {
    "timestamp": 1732273845.123456,
    "keypoints": {
      "hand": [120, 340],
      "endoscope": [450, 280],
      "brush": [380, 420]
    },
    "inference_result": {
      "keypoints": {...},
      "motion": {
        "bending_count": 5,
        "fully_submerged": true,
        "bubble_detected": false
      }
    }
  },
  ...
]
```

**使用场景**: 分析特定时间段的关键点和动作数据

---

### 5. 获取任务的所有关键点数据

```http
GET /task/traceback/{task_id}/all_keypoints
```

**参数**:
- `task_id` (路径参数): 任务 ID

**响应示例**:
```json
{
  "task_id": 123,
  "total_frames": 4500,
  "keypoints": [
    {
      "timestamp": 1732273845.123456,
      "keypoints": {...},
      "inference_result": {...}
    },
    ...
  ]
}
```

**使用场景**: 导出完整任务的关键点数据用于后续分析或可视化

---

## 使用示例

### Python 客户端示例

```python
import requests
import json

# 1. 获取任务的所有视频段信息
def get_task_segments(task_id, video_type="processed"):
    url = f"http://localhost:8000/task/traceback/{task_id}/segments"
    params = {"video_type": video_type}
    response = requests.get(url, params=params)
    return response.json()

# 2. 下载 HLS 播放列表
def download_playlist(task_id, video_type="processed"):
    url = f"http://localhost:8000/task/traceback/{task_id}/playlist"
    params = {"video_type": video_type}
    response = requests.get(url, params=params)
    
    with open(f"task_{task_id}_{video_type}.m3u8", "wb") as f:
        f.write(response.content)
    
    print(f"✓ 播放列表已下载: task_{task_id}_{video_type}.m3u8")

# 3. 下载单个视频段
def download_video_segment(task_id, segment_id, output_file):
    url = f"http://localhost:8000/task/traceback/{task_id}/video/{segment_id}"
    response = requests.get(url, stream=True)
    
    with open(output_file, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    
    print(f"✓ 视频段已下载: {output_file}")

# 4. 获取关键点数据并分析
def analyze_keypoints(task_id):
    url = f"http://localhost:8000/task/traceback/{task_id}/all_keypoints"
    response = requests.get(url)
    data = response.json()
    
    print(f"任务 {task_id} 分析:")
    print(f"  总帧数: {data['total_frames']}")
    
    # 统计弯曲次数
    bending_counts = [
        frame['inference_result']['motion']['bending_count']
        for frame in data['keypoints']
        if 'motion' in frame.get('inference_result', {})
    ]
    
    if bending_counts:
        print(f"  最大弯曲次数: {max(bending_counts)}")
        print(f"  平均弯曲次数: {sum(bending_counts) / len(bending_counts):.2f}")

# 使用示例
task_id = 123

# 获取段信息
segments_info = get_task_segments(task_id, "processed")
print(f"找到 {segments_info['total_segments']} 个视频段")

# 下载第一个视频段
if segments_info['segments']:
    first_segment = segments_info['segments'][0]
    download_video_segment(
        task_id, 
        first_segment['segment_id'], 
        f"segment_{first_segment['segment_id']}.mp4"
    )

# 下载播放列表
download_playlist(task_id, "processed")

# 分析关键点
analyze_keypoints(task_id)
```

### JavaScript/前端示例

```javascript
// 1. 获取任务段信息并显示时间线
async function loadTaskTimeline(taskId) {
  const response = await fetch(
    `http://localhost:8000/task/traceback/${taskId}/segments?video_type=processed`
  );
  const data = await response.json();
  
  console.log(`任务 ${taskId}: ${data.total_segments} 个视频段`);
  
  data.segments.forEach((segment, index) => {
    console.log(`段 ${index + 1}: ${segment.start_time} - ${segment.end_time}`);
  });
  
  return data;
}

// 2. 在 video 元素中播放 HLS 流
function playTaskVideo(taskId, videoType = 'processed') {
  const video = document.getElementById('videoPlayer');
  const playlistUrl = `http://localhost:8000/task/traceback/${taskId}/playlist?video_type=${videoType}`;
  
  if (Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(playlistUrl);
    hls.attachMedia(video);
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = playlistUrl;
  }
}

// 3. 获取并可视化关键点数据
async function visualizeKeypoints(taskId, canvasId) {
  const response = await fetch(
    `http://localhost:8000/task/traceback/${taskId}/all_keypoints`
  );
  const data = await response.json();
  
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  
  // 绘制关键点轨迹
  data.keypoints.forEach((frame, index) => {
    if (frame.keypoints && frame.keypoints.hand) {
      const [x, y] = frame.keypoints.hand;
      ctx.fillStyle = `rgba(255, 0, 0, ${index / data.total_frames})`;
      ctx.fillRect(x, y, 3, 3);
    }
  });
  
  console.log(`✓ 已绘制 ${data.total_frames} 帧关键点`);
}

// 使用示例
const taskId = 123;
loadTaskTimeline(taskId);
playTaskVideo(taskId, 'processed');
visualizeKeypoints(taskId, 'keypointsCanvas');
```

## 错误处理

所有接口在出错时返回标准 HTTP 错误码:

- `404 Not Found`: 任务或资源不存在
  ```json
  {"detail": "未找到任务 123 的视频段"}
  ```

- `400 Bad Request`: 请求参数错误
  ```json
  {"detail": "原始视频段没有关键点数据，请使用处理后的视频段"}
  ```

## 性能建议

1. **批量下载**: 使用 `/segments` 接口获取所有路径后并行下载
2. **流式播放**: 优先使用 HLS 播放列表而非直接下载
3. **关键点分析**: 对于大型任务，先获取段列表，按需获取关键点数据
4. **缓存**: 播放列表和关键点数据可以缓存，视频文件也可以本地缓存

## 测试示例

```bash
# 假设任务 ID 为 1，已经生成了视频段

# 1. 查看任务的所有视频段
curl "http://localhost:8000/task/traceback/1/segments?video_type=processed"

# 2. 下载播放列表
curl -o task_1.m3u8 "http://localhost:8000/task/traceback/1/playlist?video_type=processed"

# 3. 下载第一个视频段（假设 segment_id=1）
curl -o segment_1.mp4 "http://localhost:8000/task/traceback/1/video/1"

# 4. 查看关键点数据
curl "http://localhost:8000/task/traceback/1/keypoints/1" | jq .

# 5. 获取所有关键点（输出较大，建议重定向）
curl "http://localhost:8000/task/traceback/1/all_keypoints" > all_keypoints.json
```

## 注意事项

1. **文件大小**: 完整任务的关键点数据可能很大（数千帧），建议分段获取
2. **路径安全**: 所有文件路径都经过验证，防止目录遍历攻击
3. **视频格式**: 视频段为 MP4 格式，编解码器为 H.264
4. **时间戳**: 所有时间戳为 UTC 时间，ISO 8601 格式

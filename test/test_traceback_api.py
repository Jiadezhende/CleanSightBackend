"""
测试任务回溯 API

使用方法:
    python test/test_traceback_api.py --task_id 1
"""

import requests
import argparse
import json
from pathlib import Path


def test_get_segments(base_url: str, task_id: int, video_type: str = "processed"):
    """测试获取视频段信息"""
    print(f"\n{'='*60}")
    print(f"测试 1: 获取任务 {task_id} 的视频段信息 ({video_type})")
    print(f"{'='*60}")
    
    url = f"{base_url}/task/traceback/{task_id}/segments"
    params = {"video_type": video_type}
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        print(f"✓ 成功获取数据")
        print(f"  任务 ID: {data['task_id']}")
        print(f"  视频类型: {data['video_type']}")
        print(f"  总段数: {data['total_segments']}")
        print(f"  播放列表: {data['playlist_path']}")
        
        if data['segments']:
            print(f"\n前 3 个视频段:")
            for i, seg in enumerate(data['segments'][:3], 1):
                print(f"  段 {i}:")
                print(f"    ID: {seg['segment_id']}")
                print(f"    路径: {seg['segment_path']}")
                print(f"    开始: {seg['start_time']}")
                print(f"    结束: {seg['end_time']}")
                if 'keypoints_path' in seg:
                    print(f"    关键点: {seg['keypoints_path']}")
        
        return data
        
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP 错误: {e}")
        print(f"响应内容: {e.response.text}")
        return None
    except Exception as e:
        print(f"✗ 错误: {e}")
        return None


def test_get_playlist(base_url: str, task_id: int, video_type: str = "processed"):
    """测试获取播放列表"""
    print(f"\n{'='*60}")
    print(f"测试 2: 获取播放列表 ({video_type})")
    print(f"{'='*60}")
    
    url = f"{base_url}/task/traceback/{task_id}/playlist"
    params = {"video_type": video_type}
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        output_file = f"task_{task_id}_{video_type}.m3u8"
        with open(output_file, "wb") as f:
            f.write(response.content)
        
        print(f"✓ 播放列表已下载: {output_file}")
        print(f"  文件大小: {len(response.content)} 字节")
        
        # 显示内容前 10 行
        lines = response.content.decode('utf-8').split('\n')[:10]
        print(f"\n播放列表内容（前 10 行）:")
        for line in lines:
            if line.strip():
                print(f"  {line}")
        
        return True
        
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP 错误: {e}")
        print(f"响应内容: {e.response.text}")
        return False
    except Exception as e:
        print(f"✗ 错误: {e}")
        return False


def test_download_video(base_url: str, task_id: int, segment_id: int):
    """测试下载视频段"""
    print(f"\n{'='*60}")
    print(f"测试 3: 下载视频段 {segment_id}")
    print(f"{'='*60}")
    
    url = f"{base_url}/task/traceback/{task_id}/video/{segment_id}"
    
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        output_file = f"segment_{segment_id}.mp4"
        
        # 流式下载
        with open(output_file, "wb") as f:
            downloaded = 0
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
        
        print(f"✓ 视频段已下载: {output_file}")
        print(f"  文件大小: {downloaded} 字节 ({downloaded / 1024:.2f} KB)")
        
        return True
        
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP 错误: {e}")
        print(f"响应内容: {e.response.text}")
        return False
    except Exception as e:
        print(f"✗ 错误: {e}")
        return False


def test_get_keypoints(base_url: str, task_id: int, segment_id: int):
    """测试获取关键点数据"""
    print(f"\n{'='*60}")
    print(f"测试 4: 获取关键点数据（段 {segment_id}）")
    print(f"{'='*60}")
    
    url = f"{base_url}/task/traceback/{task_id}/keypoints/{segment_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        
        data = response.json()
        print(f"✓ 成功获取关键点数据")
        print(f"  总帧数: {len(data)}")
        
        if data:
            print(f"\n第一帧数据:")
            first_frame = data[0]
            print(f"  时间戳: {first_frame.get('timestamp')}")
            print(f"  关键点: {json.dumps(first_frame.get('keypoints'), indent=4)}")
            if 'inference_result' in first_frame:
                print(f"  推理结果: {json.dumps(first_frame['inference_result'], indent=4)}")
        
        return data
        
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP 错误: {e}")
        print(f"响应内容: {e.response.text}")
        return None
    except Exception as e:
        print(f"✗ 错误: {e}")
        return None


def test_get_all_keypoints(base_url: str, task_id: int):
    """测试获取所有关键点数据"""
    print(f"\n{'='*60}")
    print(f"测试 5: 获取所有关键点数据")
    print(f"{'='*60}")
    
    url = f"{base_url}/task/traceback/{task_id}/all_keypoints"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        
        data = response.json()
        print(f"✓ 成功获取所有关键点数据")
        print(f"  任务 ID: {data['task_id']}")
        print(f"  总帧数: {data['total_frames']}")
        
        # 保存到文件
        output_file = f"all_keypoints_task_{task_id}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"  已保存到: {output_file}")
        
        # 简单统计
        if data['keypoints']:
            print(f"\n数据统计:")
            
            # 统计有推理结果的帧数
            with_inference = sum(
                1 for frame in data['keypoints'] 
                if frame.get('inference_result')
            )
            print(f"  含推理结果: {with_inference} 帧")
        
        return data
        
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP 错误: {e}")
        print(f"响应内容: {e.response.text}")
        return None
    except Exception as e:
        print(f"✗ 错误: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="测试任务回溯 API")
    parser.add_argument("--task_id", type=int, required=True, help="任务 ID")
    parser.add_argument("--base_url", type=str, default="http://localhost:8000", help="API 基础 URL")
    parser.add_argument("--video_type", type=str, default="processed", choices=["raw", "processed"], help="视频类型")
    
    args = parser.parse_args()
    
    print("=" * 60)
    print(f"任务回溯 API 测试")
    print("=" * 60)
    print(f"任务 ID: {args.task_id}")
    print(f"视频类型: {args.video_type}")
    print(f"API URL: {args.base_url}")
    
    # 测试 1: 获取视频段信息
    segments_data = test_get_segments(args.base_url, args.task_id, args.video_type)
    
    if not segments_data:
        print("\n✗ 无法获取视频段信息，测试终止")
        return
    
    # 测试 2: 获取播放列表
    test_get_playlist(args.base_url, args.task_id, args.video_type)
    
    # 测试 3-4: 如果有视频段，下载第一个段和关键点
    if segments_data['segments']:
        first_segment_id = segments_data['segments'][0]['segment_id']
        test_download_video(args.base_url, args.task_id, first_segment_id)
        
        # 只有处理后的视频才有关键点
        if args.video_type == "processed":
            test_get_keypoints(args.base_url, args.task_id, first_segment_id)
    
    # 测试 5: 获取所有关键点（只对处理后的视频）
    if args.video_type == "processed":
        test_get_all_keypoints(args.base_url, args.task_id)
    
    print(f"\n{'='*60}")
    print("测试完成！")
    print(f"{'='*60}")
    print("\n生成的文件:")
    for file in Path('.').glob(f'*task_{args.task_id}*'):
        print(f"  {file}")
    for file in Path('.').glob(f'segment_*.mp4'):
        print(f"  {file}")


if __name__ == "__main__":
    main()

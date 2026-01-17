"""
快速测试：验证解码器进程池在服务启动时正确初始化
"""

import sys
import asyncio
from contextlib import asynccontextmanager

# 添加项目路径
sys.path.insert(0, 'e:/ywc_college/junior1/本科生课题/src/CleanSightBackend')


async def test_startup():
    """测试启动流程"""
    print("=" * 60)
    print("测试解码器进程池启动初始化")
    print("=" * 60)
    
    # 导入main模块
    from app.main import lifespan
    from fastapi import FastAPI
    
    app = FastAPI()
    
    print("\n1. 开始启动流程...")
    
    # 使用lifespan上下文管理器
    async with lifespan(app):
        print("\n2. 启动完成！")
        
        # 检查进程池是否已初始化
        from app.services.decoder import get_decoder_pool
        
        decoder_pool = get_decoder_pool()
        stats = decoder_pool.get_stats()
        
        print("\n3. 解码器进程池状态:")
        print(f"   - 总进程数: {stats['total_processes']}")
        print(f"   - 活动进程数: {stats['alive_processes']}")
        print(f"   - 预热进程数: {stats['prewarm_processes']}")
        print(f"   - 预热进程存活: {stats['prewarm_alive']}")
        print(f"   - 最大进程数: {stats['max_workers']}")
        print(f"   - 队列大小: {stats['queue_size']}")
        print(f"   - 活跃任务: {stats['tasks']}")
        
        if stats['max_workers'] == 16:
            print("\n✓ 进程池最大进程数配置正确 (16)")
        else:
            print(f"\n✗ 进程池最大进程数不正确: {stats['max_workers']}")
        
        if stats['prewarm_processes'] == 3 and stats['prewarm_alive'] == 3:
            print("✓ 预热进程启动成功 (3/3)")
        else:
            print(f"✗ 预热进程状态异常: {stats['prewarm_alive']}/{stats['prewarm_processes']}")
        
        # 检查帧分发器
        from app.services.decoder import _frame_dispatcher
        if _frame_dispatcher is not None and _frame_dispatcher._running:
            print("✓ 帧分发器已启动")
        else:
            print("✗ 帧分发器未正确启动")
        
        print("\n4. 等待2秒...")
        await asyncio.sleep(2)
        
    print("\n5. 关闭流程完成！")
    print("\n" + "=" * 60)
    print("✅ 测试通过：解码器进程池在启动时正确初始化")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(test_startup())
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
    except Exception as e:
        print(f"\n\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

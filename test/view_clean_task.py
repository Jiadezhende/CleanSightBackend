#!/usr/bin/env python3
"""
查看数据库表格 clean_task 的脚本
"""
from app.database import get_db
from app.models.task import DBTask

def view_clean_task():
    db = next(get_db())
    try:
        # 查询所有任务
        tasks = db.query(DBTask).all()
        
        if not tasks:
            print("表格 clean_task 中没有记录。")
            return
        
        print("表格 clean_task 中的记录：")
        print("-" * 80)
        print(f"{'task_id':<10} {'initiator_id':<15} {'current_step':<15} {'status':<15} {'created_at':<20}")
        print("-" * 80)
        
        for task in tasks:
            print(f"{task.task_id:<10} {task.initiator_id:<15} {task.current_step:<15} {task.status:<15} {task.created_at.strftime('%Y-%m-%d %H:%M:%S'):<20}")
        
        print("-" * 80)
        print(f"总共 {len(tasks)} 条记录。")
    
    except Exception as e:
        print(f"查询失败：{e}")
    finally:
        db.close()

if __name__ == "__main__":
    view_clean_task()
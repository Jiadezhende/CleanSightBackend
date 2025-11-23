#!/usr/bin/env python3
"""
交互式SQL执行脚本
允许用户输入SQL语句并执行操作
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # Add project root to path
from app.database import get_db
from sqlalchemy import text

def interactive_sql():
    db = next(get_db())
    try:
        print("进入交互式SQL模式。输入SQL语句执行，或输入'exit'退出。")
        print("注意：对于SELECT语句，将显示结果；对于其他操作，将显示执行状态。")
        print("-" * 60)
        
        while True:
            sql = input("SQL> ").strip()
            
            if sql.lower() in ['exit', 'quit', 'q']:
                print("退出交互式SQL模式。")
                break
            
            if not sql:
                continue
            
            try:
                result = db.execute(text(sql))
                
                rows = result.fetchall()
                if rows:
                    # SELECT语句，显示结果
                    print(f"查询到 {len(rows)} 条记录：")
                    for i, row in enumerate(rows):
                        print(f"[{i+1}] {row}")
                else:
                    # 非SELECT语句
                    print("执行成功。")
                
                # 对于修改操作，提交事务
                db.commit()
                
            except Exception as e:
                print(f"执行失败：{e}")
                db.rollback()
    
    except Exception as e:
        print(f"数据库连接失败：{e}")
    finally:
        db.close()

if __name__ == "__main__":
    interactive_sql()
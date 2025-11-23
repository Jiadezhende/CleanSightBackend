#!/usr/bin/env python3
"""
测试数据库连接
"""
from app.database import get_db
from sqlalchemy import text

def test_db_connection():
    db = next(get_db())
    try:
        # 执行简单查询测试连接
        result = db.execute(text("SELECT 1 as test"))
        row = result.fetchone()
        if row and row[0] == 1:
            print("✅ 数据库连接成功！")
        else:
            print("❌ 数据库连接失败：查询无结果")
    except Exception as e:
        print(f"❌ 数据库连接失败：{e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_db_connection()
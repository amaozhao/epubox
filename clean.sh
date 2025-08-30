#!/bin/bash
# 一键清理当前AImend项目下所有pytest和Python测试缓存目录/文件

set -e

# 删除pytest缓存
echo "清理 .pytest_cache ..."
rm -rf .pytest_cache

# 删除 ruff_cache 缓存
echo "清理 .ruff_cache ..."
rm -rf .ruff_cache

# 删除所有__pycache__目录
echo "清理所有 __pycache__ ..."
find . -type d -name "__pycache__" -exec rm -rf {} +

# 删除所有 .pyc 文件
echo "清理所有 .pyc ..."
find . -type f -name "*.pyc" -delete

# 删除所有 .pyo 文件（如有）
find . -type f -name "*.pyo" -delete

# 删除 test.db、insighter.db 等测试数据库（如有）
rm -f test.db insighter.db

echo "清理完成！"

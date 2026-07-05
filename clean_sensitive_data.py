"""清理敏感数据脚本 - 打包前运行"""
import os
import sys
import shutil
from pathlib import Path

# Windows 控制台编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = Path(__file__).parent

def clean_sensitive_data():
    """清理所有敏感数据和临时文件"""

    print("🧹 开始清理敏感数据...")

    # 1. 删除数据库文件
    db_file = ROOT / "webui" / "webui.db"
    if db_file.exists():
        try:
            db_file.unlink()
            print(f"✓ 已删除数据库: {db_file.name}")
        except PermissionError:
            print(f"⚠️ 数据库文件被占用，请先关闭 WebUI 后手动删除: {db_file.name}")

    # 2. 清空日志目录
    logs_dir = ROOT / "webui" / "logs"
    if logs_dir.exists():
        count = 0
        for log_file in logs_dir.glob("*.log"):
            log_file.unlink()
            count += 1
        print(f"✓ 已清理 {count} 个日志文件")

    # 3. 删除所有 __pycache__ 目录
    pycache_count = 0
    for pycache_dir in ROOT.rglob("__pycache__"):
        shutil.rmtree(pycache_dir)
        pycache_count += 1
    print(f"✓ 已删除 {pycache_count} 个 __pycache__ 目录")

    # 4. 删除所有 .pyc 文件（如果有漏网之鱼）
    pyc_count = 0
    for pyc_file in ROOT.rglob("*.pyc"):
        pyc_file.unlink()
        pyc_count += 1
    if pyc_count > 0:
        print(f"✓ 已删除 {pyc_count} 个 .pyc 文件")

    # 5. 删除测试脚本（包含测试邮箱）
    test_file = ROOT / "test_workspace.py"
    if test_file.exists():
        test_file.unlink()
        print(f"✓ 已删除测试脚本: {test_file.name}")

    # 6. 删除 .env 文件（如果存在）
    env_file = ROOT / ".env"
    if env_file.exists():
        env_file.unlink()
        print(f"✓ 已删除环境变量文件: {env_file.name}")

    print("\n✅ 清理完成！现在可以安全打包给他人使用了喵~")
    print("\n📦 打包建议：")
    print("  1. 排除 .git 目录（如果有）")
    print("  2. 排除 venv / .venv 虚拟环境目录")
    print("  3. 打包成 .zip 或 .tar.gz 格式")
    print("  4. 附上 README.md 使用说明")

if __name__ == "__main__":
    clean_sensitive_data()

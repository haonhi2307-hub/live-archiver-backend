#!/usr/bin/env python3
"""
Công cụ tạo License Key bán hàng cho Live Archiver.
Sử dụng:
  python generate_keys.py --days 30 --count 5 --note "Khach_Zalo"
  python generate_keys.py --days 365 --count 1
"""
import argparse
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.license import create_licenses, init_db

def main():
    parser = argparse.ArgumentParser(description="Tạo mã License Key cho Live Archiver")
    parser.add_argument("--days", type=int, default=30, help="Số ngày sử dụng (mặc định 30)")
    parser.add_argument("--count", type=int, default=1, help="Số lượng key cần tạo (mặc định 1)")
    parser.add_argument("--note", type=str, default="", help="Ghi chú (Ví dụ tên khách)")

    args = parser.parse_args()

    init_db()
    keys = create_licenses(count=args.count, duration_days=args.days, note=args.note)

    print("\n" + "="*50)
    print(f"🎉 ĐÃ TẠO THÀNH CÔNG {len(keys)} LICENSE KEY ({args.days} NGÀY)")
    print("="*50)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {k}")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()

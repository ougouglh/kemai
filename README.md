# 商品条码OCR识别自动化工具

## 简介

自动识别微信小程序"马上赢比价宝"中的商品条码信息，包括品牌、集团、商品名称、厂商、类目、售价中位数、首订单时间等。

## 主要文件

| 文件 | 说明 |
|------|------|
| `p_ocr_name_optimized.py` | 主程序 - GUI条码OCR识别工具 |
| `monitor_with_email.py` | 监控脚本 - 监控OCR进程，异常时发送邮件/企微告警 |
| `requirements.txt` | Python依赖包列表 |

## 功能特点

- 批量处理条码数据
- 分批次处理大数据集
- 缓存机制支持断点续传
- 自动保存识别结果
- 微信小程序自动重启

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

1. 准备CSV文件，包含 `barcode` 和 `item_name` 列
2. 运行程序：
   ```bash
   python p_ocr_name_optimized.py
   ```
3. 设置捕获区域和按钮位置
4. 点击开始处理

## 输出

识别结果保存到 `out/` 目录，文件名包含批次信息。

## 环境要求

- Python 3.8+
- PaddleOCR
- PyAutoGUI
- FreeSimpleGUI

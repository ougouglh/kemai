import pandas as pd
import pyautogui
import pygetwindow as gw
import time
import os
import cv2
import numpy as np
import re
import FreeSimpleGUI as sg
import configparser
from PIL import Image
import csv
from datetime import datetime
import json
import random
from paddleocr import PaddleOCR
import threading
import logging
import shutil
import pyperclip


class BarcodeOCRProcessor:
    """条码OCR处理器"""
    
    def __init__(self):
        # ====== 配置区域 ======
        self.CONFIG_FILE = "../screen_ocr_config.ini"
        self.OUTPUT_DIR = "out"
        if not os.path.exists(self.OUTPUT_DIR):
            os.makedirs(self.OUTPUT_DIR)
        batch_name = os.path.basename(self.FILE_PATH).replace('.csv', '')
        self.OUTPUT_CSV = os.path.join(self.OUTPUT_DIR, f"识别结果_{batch_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        self.CACHE_FILE = "progress_cache.json"
        self.FILE_PATH = "data/待处理20260501批次/待处理20260501_batch1.csv"
        self.BATCH_DIR = "data/待处理20260501批次"
        self.BATCH_PATTERN = "待处理20260501_batch"
        self.WINDOW_TITLE = "马上赢比价宝"

        # 小程序重启位置配置
        self.mini_program_positions = {
            'mini_program_close': None,
            'mini_program_list_entry': None,
            'target_mini_program': None,
            'mini_program_list_close': None,
        }

        # 每批次完成后暂停模式
        self.pause_after_batch = True
        
        # 默认捕获区域
        self.capture_region = (19, 475, 485, 397)  # 起点X、起点Y、区域宽度、区域高度
        
        # 截图保存目录
        self.IMAGE_DIR = "captured_images"
        if not os.path.exists(self.IMAGE_DIR):
            try:
                os.makedirs(self.IMAGE_DIR)

            except Exception as e:
                print(f"创建截图目录失败: {e}")

        # ====== 全局状态 ======
        self.current_index = 0
        self.paused = False
        self.failed_barcodes = []
        self.processing = False
        self.results = []
        self.barcodes = []
        self.original_barcodes = []
        self.button_positions = {}

        # OCR实例（延迟初始化）
        self.ocr = None

        # 缓存保存间隔（每个N个条码保存一次）
        self.cache_save_interval = 5
        self.processed_since_last_save = 0

        # 日志配置
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

        # ====== 预编译正则表达式（性能优化）======
        self._compile_regex_patterns()
    
    def init_ocr(self):
        """初始化OCR引擎"""
        if self.ocr is None:
            self.ocr = PaddleOCR(lang='ch', det=True, rec=True, cls=False)

    def _compile_regex_patterns(self):
        """预编译所有正则表达式以提高性能"""
        # 无商品模式
        self.no_product_patterns = [
            re.compile(r'未查询到该条码商品'),
            re.compile(r'无此条码'),
            re.compile(r'未找到相关商品'),
            re.compile(r'未找到商品'),
            re.compile(r'抱歉.*没有找到'),
            re.compile(r'没有找到.*商品'),
            re.compile(r'搜索结果为空'),
            re.compile(r'暂未收录该商品'),
            re.compile(r'该商品不存在'),
            re.compile(r'暂无该商品信息')
        ]

        # 商品名称模式
        self.product_name_patterns = [
            re.compile(r'^([^\n]+?)\n', re.IGNORECASE),
            re.compile(r'商品名称[:：]?\s*([^\n]*)', re.IGNORECASE),
            re.compile(r'名称[:：]?\s*([^\n]*)', re.IGNORECASE),
            re.compile(r'([^\n]+?)\s*\d+[度°]\s*\d*[a-zA-Z]+', re.IGNORECASE),
            re.compile(r'([^\n]+?)\d+[度°]\d*[a-zA-Z]+', re.IGNORECASE),
            re.compile(r'([^\n]+?)\s*\d*[a-zA-Z]+\s*\d+[度°]', re.IGNORECASE),
            re.compile(r'([^\n]+?)\d+[a-zA-Z%]+', re.IGNORECASE),
            re.compile(r'([^\n]+?)\d+[度°%]', re.IGNORECASE),
            re.compile(r'([^\n]+?[\-+*×÷=±]+[^\n]+)', re.IGNORECASE),
            re.compile(r'([^\n]+?[#@$%^&()_]+[^\n]+)', re.IGNORECASE)
        ]

        # 品牌模式
        self.brand_patterns = [
            re.compile(r'品牌[:：]?\s*([^\n]*?)(?=\n|集团|$)', re.DOTALL),
            re.compile(r'品牌\s*[:：]?\s*([^\n]*?)(?=\n|集团|$)', re.DOTALL),
            re.compile(r'品牌\s*([^\n]*?)(?=\n|集团|$)', re.DOTALL),
            re.compile(r'牌\s*[:：]?\s*([^\n]*?)(?=\n|集团|$)', re.DOTALL)
        ]

        # 集团模式
        self.group_patterns = [
            re.compile(r'集团[:：]?\s*([^\n]*?)(?=\n|品牌|$)', re.DOTALL),
            re.compile(r'集团\s*[:：]?\s*([^\n]*?)(?=\n|品牌|$)', re.DOTALL),
            re.compile(r'集团\s*([^\n]*?)(?=\n|品牌|$)', re.DOTALL),
            re.compile(r'团\s*[:：]?\s*([^\n]*?)(?=\n|品牌|$)', re.DOTALL)
        ]

        # 厂商模式
        self.manu_patterns = [
            re.compile(r'厂商[:：]?\s*([^\n]*)'),
            re.compile(r'厂商名称[:：]?\s*([^\n]*)')
        ]

        # 类目模式
        self.cat_patterns = [
            re.compile(r'类目[:：]?\s*([^\n]*)'),
            re.compile(r'所属类目[:：]?\s*([^\n]*)')
        ]

        # 日期模式
        self.date_patterns = [
            re.compile(r'首订单时间[:：]?\s*(\d{4}-\d{2}-\d{2})'),
            re.compile(r'首单时间[:：]?\s*(\d{4}-\d{2}-\d{2})'),
            re.compile(r'首订单时间\s*[\r\n]+\s*(\d{4}-\d{2}-\d{2})'),  # 跨行
            re.compile(r'首订单时间[^\d]*?(\d{4}-\d{2}-\d{2})')  # 中间有杂讯
        ]

        # 价格模式
        self.price_patterns = [
            re.compile(r'售价中位数[:：]?\s*(\d+\.?\d*)'),
            re.compile(r'售价中位数\s*\n\s*(\d+\.?\d*)'),
            re.compile(r'中位数[:：]?\s*(\d+\.?\d*)'),
            re.compile(r'售价中位数[^\d\n]*?(\d+\.?\d*)'),  # 同行，中间有非数字杂讯
            re.compile(r'售价中位数\s*[\r\n]+[^\d]*?(\d+\.?\d*)')  # 跨行，中间有非数字杂讯
        ]

        # 不可见字符过滤模式
        self.invisible_char_pattern = re.compile(r'[\x00-\x1F\x7F]')
        # 多个空格合并模式
        self.multi_space_pattern = re.compile(r'\s+')

        # 加载中检测模式
        self.loading_patterns = [
            re.compile(r'加载中'),
            re.compile(r'Loading'),
            re.compile(r'处理中'),
            re.compile(r'查询中'),
            re.compile(r'请等待'),
            re.compile(r'加载...')
        ]
    
    # ====== 缓存功能函数 ======
    def save_cache(self, force=False):
        """保存当前进度到缓存文件

        Args:
            force: 是否强制保存，忽略间隔设置
        """
        # 只有在真正需要保存时才递增计数器
        if not force:
            self.processed_since_last_save += 1

        # 检查是否需要保存
        if not force and self.processed_since_last_save < self.cache_save_interval:
            return True  # 返回True表示无需保存（但不算失败）

        cache_data = {
            'current_index': self.current_index,
            'results': self.results,
            'barcodes': self.barcodes,
            'original_barcodes': self.original_barcodes,
            'failed_barcodes': self.failed_barcodes,
            'output_csv': self.OUTPUT_CSV,
            'file_path': self.FILE_PATH,
            'button_positions': self.button_positions,
            'mini_program_positions': self.mini_program_positions,
            'pause_after_batch': self.pause_after_batch
        }
        try:
            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            self.processed_since_last_save = 0  # 只在成功保存后重置
            self.logger.info(f"缓存保存成功 (已处理{self.current_index}个条码)")
            return True
        except Exception as e:
            self.logger.error(f"保存缓存失败: {str(e)}")
            # 保存失败时不重置计数器，避免进度丢失
            return False
    
    def load_cache(self):
        """从缓存文件加载进度"""
        if not os.path.exists(self.CACHE_FILE):
            return None
        try:
            with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            if 'button_positions' in cache_data:
                current_buttons = {'search_code_button', 'input_box', 'search_button', 'back_button'}
                cleaned_positions = {k: v for k, v in cache_data['button_positions'].items()
                                   if k in current_buttons}
                cache_data['button_positions'] = cleaned_positions
            self.logger.info("缓存加载成功")
            return cache_data
        except Exception as e:
            self.logger.error(f"加载缓存失败: {str(e)}")
            return None
    
    def delete_cache(self):
        """删除缓存文件"""
        if os.path.exists(self.CACHE_FILE):
            try:
                os.remove(self.CACHE_FILE)
                self.logger.info("缓存删除成功")
                return True
            except Exception as e:
                self.logger.error(f"删除缓存失败: {str(e)}")
                return False
        return True
    
    # ====== 功能函数 ======
    def read_barcodes(self):
        """读取条码数据"""
        try:
            if self.FILE_PATH.endswith('.csv'):
                df = pd.read_csv(self.FILE_PATH, encoding='utf-8', dtype={'barcode': str, 'item_barcode': str})
            elif self.FILE_PATH.endswith('.xlsx'):
                df = pd.read_excel(self.FILE_PATH, engine='openpyxl')
            elif self.FILE_PATH.endswith('.xls'):
                df = pd.read_excel(self.FILE_PATH, engine='xlrd')
            else:
                df = pd.read_csv(self.FILE_PATH, encoding='utf-8', dtype={'barcode': str, 'item_barcode': str})

            barcode_col = 'barcode' if 'barcode' in df.columns else 'item_barcode'
            item_name_col = 'item_name' if 'item_name' in df.columns else 'item_level4_cat_name'

            if item_name_col not in df.columns:
                df[item_name_col] = ""

            barcodes_list = df[[barcode_col, item_name_col]].dropna(subset=[barcode_col]).astype(str)
            barcodes_list.columns = ['barcode', 'item_name']
            self.logger.info(f"成功读取 {len(barcodes_list)} 条条码数据")
            return barcodes_list.values.tolist()
        except Exception as e:
            self.logger.error(f"读取数据失败：{e}")
            return []
    
    def capture_custom_region(self, region):
        """捕获指定区域"""
        if region[2] <= 0 or region[3] <= 0:
            return None
        try:
            screenshot = pyautogui.screenshot(region=region)
            temp_file = f"temp_screenshot_{int(time.time())}.png"
            screenshot.save(temp_file)
            return temp_file
        except Exception as e:
            self.logger.error(f"捕获错误: {str(e)}")
            return None
    
    def enhance_ocr_accuracy(self, image_path):
        """OCR识别函数（PaddleOCR版）"""
        try:
            if not os.path.exists(image_path):
                return "错误：无法读取图像文件（临时截图不存在）"
            
            # 延迟初始化OCR
            self.init_ocr()
            
            paddle_result = self.ocr.ocr(image_path, cls=False)
            ocr_text = ""
            if paddle_result and len(paddle_result) > 0:
                for line in paddle_result:
                    if line:
                        for word_info in line:
                            if word_info and len(word_info) > 1:
                                text = word_info[1][0]
                                ocr_text += text + "\n"
            ocr_text = ocr_text.strip()
            if not ocr_text:
                return "识别错误：未识别到任何文本"
            return ocr_text
        except Exception as e:
            self.logger.error(f"OCR识别错误: {str(e)}")
            return f"识别错误: {str(e)}"
    
    def is_no_product_found(self, text):
        """判断是否为'无此条码'"""
        for pattern in self.no_product_patterns:
            if pattern.search(text):
                return True
        return False
    
    def parse_ocr_text(self, text):
        """解析OCR识别结果，提取商品信息"""
        # 初始化结果字典
        result = {
            "brand": "无法识别",
            "group": "无法识别",
            "product_name": "无法识别",
            "manufacturer": "无法识别",
            "category": "无法识别",
            "median_price": "无法识别",
            "first_order_date": "无法识别"
        }

        if self.is_no_product_found(text):
            for key in result:
                result[key] = "无此条码"
            return result
        
        # 1. 提取商品名称
        for pattern in self.product_name_patterns:
            match = pattern.search(text)
            if match:
                extracted_name = match.group(1).strip()
                extracted_name = self.multi_space_pattern.sub(' ', extracted_name)
                if extracted_name:
                    result["product_name"] = extracted_name
                    break
        
        # 2. 提取品牌
        for pattern in self.brand_patterns:
            match = pattern.search(text)
            if match:
                extracted_brand = match.group(1).strip()
                extracted_brand = self.multi_space_pattern.sub(' ', extracted_brand).strip()
                if extracted_brand:
                    result["brand"] = extracted_brand
                break
        
        # 3. 提取集团
        for pattern in self.group_patterns:
            match = pattern.search(text)
            if match:
                extracted_group = match.group(1).strip()
                extracted_group = self.multi_space_pattern.sub(' ', extracted_group).strip()
                if extracted_group:
                    result["group"] = extracted_group
                break
        
        # 4. 提取厂商
        for pattern in self.manu_patterns:
            match = pattern.search(text)
            if match:
                result["manufacturer"] = match.group(1).strip()
                break

        # 5. 提取类目
        for pattern in self.cat_patterns:
            match = pattern.search(text)
            if match:
                result["category"] = match.group(1).strip()
                break

        # 6. 提取首订单时间
        for pattern in self.date_patterns:
            match = pattern.search(text)
            if match:
                result["first_order_date"] = match.group(1).strip()
                break

        # 7. 提取售价中位数
        for pattern in self.price_patterns:
            match = pattern.search(text)
            if match:
                result["median_price"] = match.group(1).strip()
                break

        # 补充提取逻辑（品牌）
        if result["brand"] == "无法识别":
            brand_lines = []
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "品牌" in line or ("牌" in line and len(line) < 20):
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    brand_lines.extend(lines[start:end])
            if brand_lines:
                combined = ' '.join(brand_lines)
                extracted_brand = combined.replace("品牌", "").replace("牌", "").replace(":", "").replace("：", "").strip()
                extracted_brand = self.multi_space_pattern.sub(' ', extracted_brand)
                if extracted_brand:
                    result["brand"] = extracted_brand

        # 补充提取逻辑（集团）
        if result["group"] == "无法识别":
            group_lines = []
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "集团" in line or ("团" in line and len(line) < 20):
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    group_lines.extend(lines[start:end])
            if group_lines:
                combined = ' '.join(group_lines)
                extracted_group = combined.replace("集团", "").replace("团", "").replace(":", "").replace("：", "").strip()
                extracted_group = self.multi_space_pattern.sub(' ', extracted_group)
                if extracted_group:
                    result["group"] = extracted_group

        # 过滤不可见字符
        for key in result:
            if result[key] not in ["无法识别", "无此条码"]:
                result[key] = self.invisible_char_pattern.sub('', result[key]).strip()
        
        # 最终空值检查
        for key in result:
            if not result[key]:
                result[key] = "无法识别"
        
        return result
    
    def save_config(self, region):
        """保存配置"""
        config = configparser.ConfigParser()
        config['DEFAULT'] = {
            'x': str(region[0]),
            'y': str(region[1]),
            'width': str(region[2]),
            'height': str(region[3])
        }
        with open(self.CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
    
    def load_config(self):
        """加载配置"""
        if not os.path.exists(self.CONFIG_FILE):
            return None
        config = configparser.ConfigParser()
        config.read(self.CONFIG_FILE)
        try:
            x = int(config['DEFAULT']['x'])
            y = int(config['DEFAULT']['y'])
            width = int(config['DEFAULT']['width'])
            height = int(config['DEFAULT']['height'])
            return (x, y, width, height)
        except (KeyError, ValueError):
            return None
    
    def get_mouse_position(self):
        """获取鼠标位置"""
        sg.popup("请将鼠标移动到您想要捕获区域的左上角位置\n然后点击'确定'按钮",
                 title="设置左上角", auto_close=False)
        time.sleep(0.5)
        top_left = pyautogui.position()
        sg.popup("请将鼠标移动到您想要捕获区域的右下角位置\n然后点击'确定'按钮",
                 title="设置右下角", auto_close=False)
        time.sleep(0.5)
        bottom_right = pyautogui.position()
        return top_left, bottom_right
    
    def get_button_position(self, button_name):
        """获取按钮坐标"""
        sg.popup(f"请将鼠标移动到'{button_name}'按钮上\n然后点击'确定'按钮",
                 title=f"设置{button_name}位置", auto_close=False)
        time.sleep(1)
        position = pyautogui.position()
        return position
    
    def input_region_manually(self):
        """手动输入区域坐标"""
        layout = [
            [sg.Text("手动输入OCR捕获区域坐标", font=("Arial", 12), justification="center")],
            [sg.Text("说明：坐标为屏幕绝对位置，区域宽度/高度需大于10", text_color="orange")],
            [sg.Text("当前默认值：X={}, Y={}, 宽={}, 高={}".format(
                self.capture_region[0], self.capture_region[1], self.capture_region[2], self.capture_region[3]
            ), size=(50, 1))],
            [sg.Text("起点X：", size=(8, 1)), sg.InputText(str(self.capture_region[0]), key="-X-", size=(15, 1))],
            [sg.Text("起点Y：", size=(8, 1)), sg.InputText(str(self.capture_region[1]), key="-Y-", size=(15, 1))],
            [sg.Text("区域宽度：", size=(8, 1)), sg.InputText(str(self.capture_region[2]), key="-WIDTH-", size=(15, 1))],
            [sg.Text("区域高度：", size=(8, 1)), sg.InputText(str(self.capture_region[3]), key="-HEIGHT-", size=(15, 1))],
            [sg.Button("确认", size=(10, 1)), sg.Button("取消", size=(10, 1))]
        ]
        window = sg.Window("手动设置区域", layout, finalize=True, modal=True)
        while True:
            event, values = window.read()
            if event in (sg.WIN_CLOSED, "取消"):
                window.close()
                return None
            if event == "确认":
                try:
                    x = int(values["-X-"])
                    y = int(values["-Y-"])
                    width = int(values["-WIDTH-"])
                    height = int(values["-HEIGHT-"])
                except ValueError:
                    sg.popup("输入错误！请填写整数", title="错误")
                    continue
                if width <= 10 or height <= 10:
                    sg.popup("区域过小！宽度和高度需大于10", title="错误")
                    continue
                if x < 0 or y < 0:
                    sg.popup("坐标错误！X和Y不能为负数", title="错误")
                    continue
                window.close()
                return (x, y, width, height)
    
    def save_results_to_csv(self):
        """保存结果到CSV"""
        if not self.results:
            return False
        try:
            with open(self.OUTPUT_CSV, 'w', newline='', encoding='utf-8-sig') as csvfile:
                fieldnames = ['barcode', 'item_name', 'product_name', 'brand', 'group', 
                              'manufacturer', 'category', 'median_price', 'first_order_date', 'screenshot_path']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for result in self.results:
                    writer.writerow(result)
            self.logger.info(f"结果已保存到: {self.OUTPUT_CSV}")
            return True
        except Exception as e:
            self.logger.error(f"保存CSV失败: {str(e)}")
            return False
    
    def setup_button_positions(self):
        """设置按钮坐标"""
        buttons_to_setup = [
            ("搜条码", "search_code_button"),
            ("搜索输入框", "input_box"),
            ("搜索", "search_button"),
            ("返回", "back_button")  # 添加返回按钮
        ]
        for button_name, key in buttons_to_setup:
            position = self.get_button_position(button_name)
            self.button_positions[key] = (position.x, position.y)
            sg.popup(f"{button_name}位置已记录: ({position.x}, {position.y})", title="成功")
        self.save_cache()
        return True

    def find_next_batch_file(self, current_file):
        """查找下一个待处理的批次文件"""
        import re
        match = re.search(r'batch(\d+)', current_file)
        if not match:
            return None
        current_num = int(match.group(1))
        next_num = current_num + 1
        next_pattern = f"{self.BATCH_DIR}/{self.BATCH_PATTERN}{next_num}.csv"
        if os.path.exists(next_pattern):
            return next_pattern
        return None

    def switch_to_batch(self, new_file):
        """切换到新批次"""
        self.FILE_PATH = new_file
        self.barcodes = self.read_barcodes()
        self.original_barcodes = self.barcodes.copy()
        self.current_index = 0
        self.results = []
        self.processed_since_last_save = 0
        batch_name = os.path.basename(new_file).replace('.csv', '')
        self.OUTPUT_CSV = os.path.join(self.OUTPUT_DIR, f"识别结果_{batch_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

    def setup_mini_program_positions(self):
        """设置小程序相关位置"""
        buttons = [
            ("小程序关闭按钮", "mini_program_close"),
            ("小程序列表入口", "mini_program_list_entry"),
            ("目标小程序", "target_mini_program"),
            ("小程序列表关闭", "mini_program_list_close"),
        ]
        for name, key in buttons:
            position = self.get_button_position(name)
            self.mini_program_positions[key] = (position.x, position.y)
            sg.popup(f"{name}位置已记录: ({position.x}, {position.y})", title="成功")
        self.save_cache()
        return True

    def restart_mini_program(self):
        """重启小程序"""
        if self.mini_program_positions['mini_program_close']:
            pyautogui.click(
                self.mini_program_positions['mini_program_close'][0],
                self.mini_program_positions['mini_program_close'][1]
            )
            time.sleep(2)

        if self.mini_program_positions['mini_program_list_entry']:
            pyautogui.click(
                self.mini_program_positions['mini_program_list_entry'][0],
                self.mini_program_positions['mini_program_list_entry'][1]
            )
            time.sleep(3)

        if self.mini_program_positions['target_mini_program']:
            pyautogui.click(
                self.mini_program_positions['target_mini_program'][0],
                self.mini_program_positions['target_mini_program'][1]
            )
            time.sleep(5)

        if self.mini_program_positions['mini_program_list_close']:
            pyautogui.click(
                self.mini_program_positions['mini_program_list_close'][0],
                self.mini_program_positions['mini_program_list_close'][1]
            )
            time.sleep(3)
    
    # ====== 新增：加载中检测函数 ======
    def is_loading_in_progress(self, image_path):
        """检测截图中是否存在"加载中"文本，判断是否需要等待"""
        ocr_text = self.enhance_ocr_accuracy(image_path)  # 复用现有OCR函数
        for pattern in self.loading_patterns:
            if pattern.search(ocr_text):
                return True
        return False
    
    def process_single_barcode(self, barcode, item_name, window):
        """处理单个条码的核心逻辑"""
        try:
            # 获取按钮坐标
            search_code_button_pos = self.button_positions.get("search_code_button")
            input_box_pos = self.button_positions.get("input_box")
            search_button_pos = self.button_positions.get("search_button")
            back_button_pos = self.button_positions.get("back_button")  # 添加返回按钮
            
            if not all([search_code_button_pos, input_box_pos, search_button_pos]):
                raise Exception("按钮位置未完全设置")
            
            # 搜索步骤：
            # 1. 点击搜条码按钮
            pyautogui.click(search_code_button_pos[0], search_code_button_pos[1])
            time.sleep(0.5)
            # 2. 点击输入框
            pyautogui.click(input_box_pos[0], input_box_pos[1])
            time.sleep(0.3)
            # 3. 清空并输入条码（使用剪贴板避免前导零丢失）
            pyperclip.copy(barcode)
            pyautogui.hotkey('ctrl', 'a')
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.3)

            # 4. 点击搜索按钮
            pyautogui.click(search_button_pos[0], search_button_pos[1])
            time.sleep(1)  # 等待1秒确保"加载中"文本显示
            
            # 5. 加载中检测
            temp_loading_screenshot = self.capture_custom_region(self.capture_region)
            
            if temp_loading_screenshot and self.is_loading_in_progress(temp_loading_screenshot):
                window["-OUTPUT-"].update(f"条码 {barcode} 检测到加载中，等待3秒...")
                time.sleep(3)
                reload_screenshot = self.capture_custom_region(self.capture_region)
                if reload_screenshot and self.is_loading_in_progress(reload_screenshot):
                    window["-OUTPUT-"].update(f"条码 {barcode} 仍在加载中，再等3秒...")
                    time.sleep(3)
                if reload_screenshot and os.path.exists(reload_screenshot):
                    os.remove(reload_screenshot)
            else:
                time.sleep(1)
            
            # 删除临时检测截图
            if temp_loading_screenshot and os.path.exists(temp_loading_screenshot):
                os.remove(temp_loading_screenshot)
            
            # 6. OCR识别
            screenshot_path = self.capture_custom_region(self.capture_region)
            if not screenshot_path:
                raise Exception("截图失败")
            
            ocr_text = self.enhance_ocr_accuracy(screenshot_path)
            parsed_data = self.parse_ocr_text(ocr_text)
            
            # 保存截图
            saved_image_name = f"{barcode}.png"
            saved_image_path = os.path.join(self.IMAGE_DIR, saved_image_name)
            saved_image_abspath = os.path.abspath(saved_image_path) # 获取绝对路径
            
            try:
                shutil.copy(screenshot_path, saved_image_path)
            except Exception as e:
                self.logger.error(f"保存截图失败: {e}")
                saved_image_abspath = "保存失败"
            
            # 7. 保存结果
            result = {
                'barcode': barcode,
                'item_name': item_name,
                **parsed_data,
                'screenshot_path': saved_image_abspath
            }
            
            # 8. 返回上一页（点击返回按钮）
            if back_button_pos:
                pyautogui.click(back_button_pos[0], back_button_pos[1])
                #time.sleep(random.uniform(1, 1))  # 随机等待1-3秒再返回
            
            # 9. 返回结果
            # 清理临时文件
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)
                
            return result
        except Exception as e:
            self.logger.error(f"处理条码 {barcode} 时出错: {str(e)}")
            result = {
                'barcode': barcode,
                'item_name': item_name,
                'product_name': "无法识别",
                'brand': "无法识别",
                'group': "无法识别",
                'manufacturer': "无法识别",
                'category': "无法识别",
                'median_price': "无法识别",
                'first_order_date': "无法识别",
                'screenshot_path': ""
            }
            return result
    
    def run_main_loop(self):
        """运行主循环"""
        # 尝试加载保存的配置
        saved_region = self.load_config()
        if saved_region:
            self.capture_region = saved_region
        
        # 尝试加载缓存
        cache_data = self.load_cache()
        if cache_data:
            response = sg.popup_yes_no("检测到上次未完成的进度，是否恢复？", title="恢复进度")
            if response == "Yes":
                self.current_index = cache_data['current_index']
                self.results = cache_data['results']
                self.barcodes = cache_data['barcodes']
                self.original_barcodes = cache_data['original_barcodes']
                self.failed_barcodes = cache_data['failed_barcodes']
                # 不恢复OUTPUT_CSV，每次运行使用新的时间戳，避免覆盖旧文件
                # self.OUTPUT_CSV = cache_data['output_csv']  # 删除这行
                self.button_positions = cache_data.get('button_positions', {})
                self.mini_program_positions = cache_data.get('mini_program_positions', self.mini_program_positions)
                self.pause_after_batch = cache_data.get('pause_after_batch', self.pause_after_batch)
                if cache_data.get('file_path'):
                    self.FILE_PATH = cache_data['file_path']
            else:
                self.delete_cache()
                cache_data = None
        
        # 初始化条码数据
        if not cache_data:
            self.barcodes = self.read_barcodes()
            self.original_barcodes = self.barcodes.copy()
            total_count = len(self.barcodes)
        else:
            total_count = len(self.original_barcodes)
            remaining_barcodes = []
            for i in range(self.current_index, len(self.original_barcodes)):
                if i < len(self.barcodes):
                    remaining_barcodes.append(self.barcodes[i])
                elif i < len(self.original_barcodes):
                    remaining_barcodes.append(self.original_barcodes[i])
            self.barcodes = remaining_barcodes
            # 重要：重置current_index，因为self.barcodes现在是剩余的条码列表
            self.current_index = 0
        
        # 检查按钮坐标
        if not self.button_positions:
            response = sg.popup_yes_no("检测到未设置按钮坐标，是否现在设置？", title="设置按钮坐标")
            if response == "Yes":
                self.setup_button_positions()
            else:
                sg.popup("请先设置按钮坐标再继续", title="提示")
                return
        
        # 创建GUI布局
        layout = [
            [sg.Text("条码搜索与识别自动化工具", font=("Arial", 16))],
            [sg.Button("鼠标选区域", size=(12, 1)),
                sg.Button("手动输坐标", size=(12, 1)),
                sg.Button("设置按钮", size=(12, 1), key="-SETUP-BUTTONS-"),
                sg.Button("设置小程序", size=(12, 1), key="-SETUP-MINI-"),
                sg.Text(
                    f"当前区域: X={self.capture_region[0]} Y={self.capture_region[1]} 宽={self.capture_region[2]} 高={self.capture_region[3]}",
                    key="-REGION-INFO-")
            ],
            [sg.Text(f"搜条码按钮: {self.button_positions.get('search_code_button', '未设置')}, 返回按钮: {self.button_positions.get('back_button', '未设置')}", key="-BUTTON-INFO-"),
             sg.Checkbox("暂停每批次", default=self.pause_after_batch, key="-PAUSE-BATCH-", enable_events=True),
             sg.Text(f"批次: {os.path.basename(self.FILE_PATH)}", key="-BATCH-INFO-")],
            [
                sg.Button("开始/继续", size=(10, 1), key="-START-", button_color=('white', 'green')),
                sg.Button("暂停", size=(10, 1), key="-PAUSE-", disabled=True),
                sg.Button("保存缓存", size=(10, 1), key="-SAVE-CACHE-", disabled=False),
                sg.Button("删除缓存", size=(10, 1), key="-DELETE-CACHE-", button_color=('white', 'orange')),
                sg.Button("删除所选", size=(10, 1), key="-DELETE-", disabled=True, button_color=('white', 'red')),
                sg.Button("批量删除", size=(10, 1), key="-BATCH-DELETE-", button_color=('white', 'orange')),
                sg.Button("退出", size=(10, 1))
            ],
            [sg.ProgressBar(total_count, orientation='h', size=(50, 20), key='-PROGRESS-')],
            [sg.Text("当前状态: 等待开始", key="-STATUS-", size=(50, 1))],
            [sg.Text("当前条码: 无", key="-CURRENT-BARCODE-", size=(50, 1))],
            [sg.Text("识别结果预览:", font=("Arial", 10))],
            [sg.Multiline(size=(80, 10), key="-OUTPUT-", autoscroll=True, disabled=True)],
            [sg.Text("结果表格:", font=("Arial", 10))],
            [sg.Table(values=[[r['barcode'], r['item_name'], r['product_name'], r['brand'], r['group'], r.get('manufacturer',''), r.get('category',''), r.get('median_price',''), r.get('first_order_date','')] for r in self.results],
                      headings=['条码', '原始名称', '识别名称', '品牌', '集团', '厂商', '类目', '售价中位数', '首订单时间'],
                      auto_size_columns=False,
                      col_widths=[15, 20, 30, 15, 15, 20, 10, 10, 15],
                      display_row_numbers=False,
                      justification='left',
                      num_rows=10,
                      key='-TABLE-',
                      enable_events=True,
                      select_mode=sg.TABLE_SELECT_MODE_EXTENDED)],
            [sg.Button("保存结果", size=(10, 1))]
        ]
        
        # 创建窗口
        window = sg.Window("条码搜索与识别自动化工具", layout, finalize=True)
        # 使用已处理的结果数量来初始化进度条，而不是current_index
        # 因为从缓存恢复时，current_index被重置为0，但results中已经有之前的结果
        window['-PROGRESS-'].update(len(self.results), total_count)
        
        # 激活小程序窗口
        windows = gw.getWindowsWithTitle(self.WINDOW_TITLE)
        if not windows:
            sg.popup(f"未找到窗口：{self.WINDOW_TITLE}", title="错误")
        else:
            window_obj = windows[0]
            window_obj.activate()
            time.sleep(1)
        
        # 事件循环
        while True:
            event, values = window.read(timeout=100)
            
            # 退出程序
            if event in (sg.WIN_CLOSED, "退出"):
                # 退出前强制保存缓存
                self.save_cache(force=True)
                if self.save_results_to_csv():
                    sg.popup(f"结果已保存到: {self.OUTPUT_CSV}", title="保存成功")
                break
            
            # 鼠标选择区域
            if event == "鼠标选区域":
                try:
                    top_left, bottom_right = self.get_mouse_position()
                    x = min(top_left.x, bottom_right.x)
                    y = min(top_left.y, bottom_right.y)
                    width = abs(top_left.x - bottom_right.x)
                    height = abs(top_left.y - bottom_right.y)
                    if width < 10 or height < 10:
                        sg.popup("区域太小！请选择更大的区域", title="错误")
                        continue
                    self.capture_region = (x, y, width, height)
                    self.save_config(self.capture_region)
                    window["-REGION-INFO-"].update(
                        f"当前区域: X={x} Y={y} 宽={width} 高={height}"
                    )
                    sg.popup(f"区域设置成功！\n左上角: ({x}, {y})\n右下角: ({x + width}, {y + height})",
                             title="设置完成")
                except Exception as e:
                    sg.popup(f"设置区域时出错: {str(e)}", title="错误")
            
            # 手动输入区域
            if event == "手动输坐标":
                try:
                    new_region = self.input_region_manually()
                    if new_region:
                        x, y, width, height = new_region
                        self.capture_region = (x, y, width, height)
                        self.save_config(self.capture_region)
                        window["-REGION-INFO-"].update(
                            f"当前区域: X={x} Y={y} 宽={width} 高={height}"
                        )
                        sg.popup(f"手动输入区域成功！\n起点坐标: ({x}, {y})\n区域大小: {width}x{height}",
                                 title="设置完成")
                except Exception as e:
                    sg.popup(f"手动设置区域时出错: {str(e)}", title="错误")
            
            # 设置按钮坐标
            if event == "-SETUP-BUTTONS-":
                self.setup_button_positions()
                window["-BUTTON-INFO-"].update(f"搜条码按钮: {self.button_positions.get('search_code_button', '未设置')}, 返回按钮: {self.button_positions.get('back_button', '未设置')}")

            # 设置小程序位置
            if event == "-SETUP-MINI-":
                self.setup_mini_program_positions()
                sg.popup("小程序位置设置完成！", title="成功")

            # 暂停每批次模式切换
            if event == "-PAUSE-BATCH-":
                self.pause_after_batch = values["-PAUSE-BATCH-"]

            # 保存缓存
            if event == "-SAVE-CACHE-":
                if self.save_cache(force=True):  # 手动保存时强制写入
                    sg.popup("进度已保存到缓存文件", title="保存成功")
                else:
                    sg.popup("保存缓存失败，请查看日志", title="错误")
            
            # 删除缓存
            if event == "-DELETE-CACHE-":
                if self.delete_cache():
                    sg.popup("缓存文件已删除", title="成功")
                else:
                    sg.popup("删除缓存失败，请查看日志", title="错误")
            
            # 开始/继续处理
            if event == "-START-":
                if not windows:
                    sg.popup("请先打开小程序窗口", title="错误")
                    continue
                self.processing = True
                self.paused = False
                window["-START-"].update(disabled=True)
                window["-PAUSE-"].update(disabled=False)
                window["-DELETE-"].update(disabled=True)
                window["-STATUS-"].update("当前状态: 运行中")
            
            # 暂停处理
            if event == "-PAUSE-":
                self.paused = True
                self.processing = False
                window["-START-"].update(disabled=False)
                window["-PAUSE-"].update(disabled=True)
                window["-DELETE-"].update(disabled=False)
                window["-STATUS-"].update("当前状态: 已暂停")
                self.save_cache(force=True)  # 暂停时强制保存
            
            # 保存结果
            if event == "保存结果":
                if self.save_results_to_csv():
                    sg.popup(f"结果已保存到: {self.OUTPUT_CSV}", title="保存成功")
                else:
                    sg.popup("保存失败，请查看日志", title="错误")
            
            # 批量删除所选
            if event == "-DELETE-" and self.paused:
                selected_rows = values["-TABLE-"]
                if not selected_rows:
                    sg.popup("请先选择要删除的结果项", title="提示")
                    continue
                selected_rows_sorted = sorted(selected_rows, reverse=True)
                deleted_barcode_list = []
                for row_index in selected_rows_sorted:
                    if 0 <= row_index < len(self.results):
                        deleted_result = self.results[row_index]
                        deleted_barcode = deleted_result["barcode"]
                        deleted_barcode_list.append(deleted_barcode)
                        self.barcodes.append([deleted_barcode, deleted_result["item_name"]])
                        self.results.pop(row_index)
                table_data = [[r['barcode'], r['item_name'], r['product_name'], r['brand'], r['group'], r.get('manufacturer',''), r.get('category',''), r.get('median_price',''), r.get('first_order_date','')] for r in self.results]
                window['-TABLE-'].update(values=table_data)
                window['-PROGRESS-'].update(len(self.results), total_count)
                sg.popup(
                    f"批量删除成功！\n共删除 {len(deleted_barcode_list)} 项结果\n"
                    f"删除的条码：{', '.join(deleted_barcode_list)}\n"
                    "这些条码已重新加入待搜索列表",
                    title="批量删除完成"
                )
                self.save_cache(force=True)

            # 批量删除范围
            if event == "-BATCH-DELETE-" and self.paused:
                delete_from = self.current_index
                total_count = len(self.original_barcodes)
                layout = [
                    [sg.Text("批量删除指定范围的缓存", font=("Arial", 12))],
                    [sg.Text(f"当前进度: 已处理 {self.current_index} 条，总计 {total_count} 条")],
                    [sg.Text(f"当前批次: {os.path.basename(self.FILE_PATH)}")],
                    [sg.Text("从第几条开始删除（0表示全部删除）:", size=(35, 1))],
                    [sg.InputText(str(self.current_index), key="-DELETE-FROM-", size=(10, 1))],
                    [sg.Text("删除到第几条（留空表示删除到最后）:", size=(35, 1))],
                    [sg.InputText(str(len(self.results)), key="-DELETE-TO-", size=(10, 1))],
                    [sg.Button("确认删除", size=(10, 1), button_color=('white', 'red')),
                     sg.Button("取消", size=(10, 1))]
                ]
                batch_window = sg.Window("批量删除", layout, finalize=True, modal=True)
                while True:
                    batch_event, batch_values = batch_window.read()
                    if batch_event in (sg.WIN_CLOSED, "取消"):
                        batch_window.close()
                        break
                    if batch_event == "确认删除":
                        try:
                            delete_from = int(batch_values["-DELETE-FROM-"])
                            delete_to_str = batch_values["-DELETE-TO-"].strip()
                            delete_to = int(delete_to_str) if delete_to_str else len(self.results)
                        except ValueError:
                            sg.popup("输入错误！请输入数字", title="错误")
                            continue
                        if delete_from < 0 or delete_to > len(self.results) or delete_from >= delete_to:
                            sg.popup("删除范围无效！", title="错误")
                            continue
                        deleted_barcodes = []
                        for i in range(delete_to - 1, delete_from - 1, -1):
                            if 0 <= i < len(self.results):
                                deleted_result = self.results.pop(i)
                                deleted_barcodes.append((deleted_result['barcode'], deleted_result.get('item_name', '')))
                        for bc, name in reversed(deleted_barcodes):
                            self.barcodes.insert(self.current_index, [bc, name])
                        batch_window.close()
                        table_data = [[r['barcode'], r['item_name'], r['product_name'], r['brand'], r['group'], r.get('manufacturer',''), r.get('category',''), r.get('median_price',''), r.get('first_order_date','')] for r in self.results]
                        window['-TABLE-'].update(values=table_data)
                        window['-PROGRESS-'].update(len(self.results), total_count)
                        sg.popup(
                            f"批量删除完成！\n共删除 {len(deleted_barcodes)} 项结果\n"
                            "这些条码已重新加入待处理列表",
                            title="删除完成"
                        )
                        self.save_cache(force=True)
                        break
            
            # 核心自动化处理
            if self.processing and not self.paused and self.current_index < len(self.barcodes):
                barcode, item_name = self.barcodes[self.current_index]

                processed_total = len(self.results)
                window["-CURRENT-BARCODE-"].update(f"当前条码: {barcode} ({processed_total + 1}/{len(self.original_barcodes)})")
                window['-PROGRESS-'].update(processed_total + 1, len(self.original_barcodes))
                
                # 处理单个条码
                result = self.process_single_barcode(barcode, item_name, window)
                self.results.append(result)
                
                # 更新UI
                if result['brand'] == "无此条码" and result['group'] == "无此条码":
                    window["-OUTPUT-"].update(f"条码 {barcode} 未找到商品\n标记为: 无此条码")
                    self.failed_barcodes.append(barcode)
                elif (result['brand'] == "无法识别" and result['group'] == "无法识别" and 
                      result['product_name'] == "无法识别"):
                    window["-OUTPUT-"].update(f"条码 {barcode} 未识别出商品名称、品牌和集团")
                    self.failed_barcodes.append(barcode)
                else:
                    window["-OUTPUT-"].update(f"识别结果!\n商品名称: {result['product_name']}\n品牌: {result['brand']}\n集团: {result['group']}\n厂商: {result['manufacturer']}\n类目: {result['category']}\n售价中位数: {result['median_price']}\n首订单时间: {result['first_order_date']}")
                
                # 更新表格
                table_data = [[r['barcode'], r['item_name'], r['product_name'], r['brand'], r['group'], r.get('manufacturer',''), r.get('category',''), r.get('median_price',''), r.get('first_order_date','')] for r in self.results]
                window['-TABLE-'].update(values=table_data)

                # 自动滚动到最后一行，确保显示最新数据
                if table_data:
                    window['-TABLE-'].set_vscroll_position(len(table_data) - 1)
                
                # 进度更新+缓存
                self.current_index += 1
                self.save_cache()


            # 处理完成
            if self.current_index >= len(self.barcodes) and self.processing:
                self.processing = False
                window["-START-"].update(disabled=True)
                window["-PAUSE-"].update(disabled=True)
                window["-DELETE-"].update(disabled=True)
                window["-STATUS-"].update("当前状态: 已完成")

                self.save_results_to_csv()
                self.delete_cache()

                next_file = self.find_next_batch_file(self.FILE_PATH)
                if next_file:
                    if self.pause_after_batch:
                        response = sg.popup_yes_no(
                            f"批次处理完成！\n文件: {os.path.basename(self.FILE_PATH)}\n\n是否继续处理下一批？",
                            title="批次完成"
                        )
                        if response == "Yes":
                            self.restart_mini_program()
                            self.switch_to_batch(next_file)
                            window["-STATUS-"].update(f"已切换批次: {os.path.basename(next_file)}，等待开始")
                            window["-BATCH-INFO-"].update(f"批次: {os.path.basename(next_file)}")
                            window["-START-"].update(disabled=False)
                            window["-PAUSE-"].update(disabled=False)
                            continue
                        else:
                            sg.popup("已暂停。重新运行脚本可继续。", title="暂停")
                            break
                    else:
                        self.restart_mini_program()
                        self.switch_to_batch(next_file)
                        window["-STATUS-"].update(f"自动切换批次: {os.path.basename(next_file)}")
                        window["-BATCH-INFO-"].update(f"批次: {os.path.basename(next_file)}")
                        self.processing = True
                        self.paused = False
                        window["-START-"].update(disabled=True)
                        window["-PAUSE-"].update(disabled=False)
                        window["-STATUS-"].update("当前状态: 运行中")
                        self.processed_since_last_save = 0
                else:
                    sg.popup("所有批次处理完成！", title="完成")
                    break


def main():
    processor = BarcodeOCRProcessor()
    processor.run_main_loop()


if __name__ == "__main__":
    main()
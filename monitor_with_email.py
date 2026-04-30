#!/usr/bin/env python3
"""
OCR监控脚本 - 完整版（含邮件告警）
功能：监控缓存文件，检测异常情况，发送邮件告警
"""

import os
import json
import time
import psutil
import smtplib
import tempfile
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.utils import formataddr
from PIL import ImageGrab

class OCRMonitor:
    def __init__(self, email_config=None):
        self.cache_file = "progress_cache.json"
        # 优先使用配置文件中的主机别名，否则自动获取
        if email_config and email_config.get('hostname_alias'):
            self.hostname = email_config['hostname_alias']
        else:
            self.hostname = os.getenv('COMPUTERNAME', '未知主机')

        self.last_alert_time = {}  # 防止重复告警
        self.alert_cooldown = 1800  # 30分钟内不重复告警同样问题

        # 邮件配置
        self.email_config = email_config or {}
        self.email_enabled = bool(self.email_config.get('enabled'))

    def setup_email_config(self):
        """配置邮件信息"""
        print("""
📧 邮件告警配置
""" + "="*60)

        if not self.email_config.get('smtp_server'):
            print("请选择邮箱类型：")
            print("1. QQ邮箱")
            print("2. 163邮箱")
            print("3. Gmail")
            print("4. 其他/手动输入")

            choice = input("请选择 (1-4): ").strip()

            # 预设配置
            configs = {
                '1': {'smtp': 'smtp.qq.com', 'port': 587, 'name': 'QQ邮箱'},
                '2': {'smtp': 'smtp.163.com', 'port': 587, 'name': '163邮箱'},
                '3': {'smtp': 'smtp.gmail.com', 'port': 587, 'name': 'Gmail'},
            }

            if choice in configs:
                config = configs[choice]
                self.email_config['smtp_server'] = config['smtp']
                self.email_config['smtp_port'] = config['port']
                print(f"已选择: {config['name']}")
            else:
                self.email_config['smtp_server'] = input("SMTP服务器: ").strip()
                self.email_config['smtp_port'] = int(input("端口 (默认587): ").strip() or "587")

        # 获取邮箱信息
        if not self.email_config.get('from_email'):
            self.email_config['from_email'] = input("发送邮箱: ").strip()

        if not self.email_config.get('password'):
            print("注意：QQ邮箱/163邮箱请使用授权码，不是登录密码")
            self.email_config['password'] = input("邮箱密码/授权码: ").strip()

        if not self.email_config.get('to_email'):
            self.email_config['to_email'] = input("接收邮箱 (默认同发送邮箱): ").strip()
            if not self.email_config['to_email']:
                self.email_config['to_email'] = self.email_config['from_email']

        # 启用邮件
        self.email_config['enabled'] = True
        self.email_enabled = True

        # 测试邮件
        test = input("是否发送测试邮件？(y/n): ").strip().lower()
        if test == 'y':
            if self.send_test_email():
                print("✅ 邮件配置成功！")
                return True
            else:
                print("❌ 邮件发送失败，请检查配置")
                retry = input("是否重试？(y/n): ").strip().lower()
                if retry == 'y':
                    return self.setup_email_config()
                return False
        else:
            print("✅ 邮件配置已保存")
            return True

    def take_screenshot(self):
        """截取全屏并保存为临时文件"""
        try:
            screenshot = ImageGrab.grab()
            temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            screenshot.save(temp_file.name, 'PNG')
            temp_file.close()
            print(f"📸 屏幕截图已保存: {temp_file.name}")
            return temp_file.name
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    def send_email(self, subject, content, html=False, screenshot_path=None):
        """发送邮件，支持图片附件"""
        if not self.email_enabled:
            print("📧 邮件未启用，跳过发送")
            return False

        try:
            # 创建邮件容器
            msg = MIMEMultipart('related')
            msg['From'] = formataddr(["OCR监控", self.email_config['from_email']])
            msg['To'] = formataddr(["管理员", self.email_config['to_email']])
            msg['Subject'] = f"[OCR监控] {subject}"

            # 添加邮件正文
            msg.attach(MIMEText(content, 'html' if html else 'plain', 'utf-8'))

            # 添加截图附件
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, 'rb') as f:
                    img_data = f.read()
                    img = MIMEImage(img_data)
                    img.add_header('Content-ID', '<screenshot>')
                    img.add_header('Content-Disposition', 'inline', filename='screenshot.png')
                    msg.attach(img)

            # 发送邮件
            smtp = smtplib.SMTP(self.email_config['smtp_server'], self.email_config['smtp_port'])
            smtp.starttls()
            smtp.login(self.email_config['from_email'], self.email_config['password'])
            smtp.send_message(msg)
            smtp.quit()

            print(f"✅ 邮件已发送: {subject}")
            return True

        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
            return False
        finally:
            # 删除临时截图文件
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                    print(f"🗑️ 临时截图已删除")
                except Exception as e:
                    print(f"⚠️ 删除临时截图失败: {e}")

    def send_test_email(self):
        """发送测试邮件"""
        subject = "OCR监控系统测试"
        content = f"""
        <html>
        <body>
            <h2>🔔 OCR监控测试邮件</h2>
            <p><strong>主机名：</strong>{self.hostname}</p>
            <p><strong>时间：</strong>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p>恭喜！OCR监控系统邮件配置成功！</p>
            <hr>
            <p><small>收到此邮件说明邮件功能正常工作</small></p>
        </body>
        </html>
        """

        return self.send_email(subject, content, html=True)

    def send_alert_email(self, anomalies):
        """发送异常告警邮件，附带屏幕截图"""
        if not anomalies:
            return

        # 截取屏幕
        screenshot_path = self.take_screenshot()

        # 生成邮件内容
        subject = f"🚨 {self.hostname} OCR监控告警"
        content = f"""
        <html>
        <body>
            <h2>🚨 OCR监控异常告警</h2>
            <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%;">
                <tr><td><strong>主机名</strong></td><td>{self.hostname}</td></tr>
                <tr><td><strong>告警时间</strong></td><td>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
                <tr><td><strong>异常数量</strong></td><td>{len(anomalies)}项</td></tr>
            </table>

            <h3>异常详情：</h3>
            <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%;">
        """

        for i, anomaly in enumerate(anomalies, 1):
            content += f"""
                <tr>
                    <td colspan="2">
                        <strong>{i}. {anomaly['level']} {anomaly['type']}</strong><br>
                        <strong>详情：</strong>{anomaly['details']}<br>
            """
            if 'suggestion' in anomaly:
                content += f"""
                        <strong>建议：</strong>{anomaly['suggestion']}
            """
            content += """
                    </td>
                </tr>
            """

        content += """
            </table>

            <h3>处理建议：</h3>
            <ul>
                <li>检查OCR脚本是否正常运行</li>
                <li>检查"马上赢比价宝"小程序是否正常</li>
                <li>查看监控日志了解详细情况</li>
            </ul>

            <h3>当前屏幕截图：</h3>
            <img src="cid:screenshot" style="max-width: 100%; border: 1px solid #ccc;">

            <hr>
            <p><small>此邮件由OCR监控系统自动发送，请勿回复</small></p>
        </body>
        </html>
        """

        self.send_email(subject, content, html=True, screenshot_path=screenshot_path)

    def check_ocr_process(self):
        """检查OCR进程是否运行"""
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
                try:
                    if proc.info['name'] and 'python' in str(proc.info['name']).lower():
                        cmdline = proc.info.get('cmdline', [])
                        if any('p_ocr_name_optimized.py' in str(cmd) for cmd in cmdline):
                            return {
                                'running': True,
                                'pid': proc.info['pid'],
                                'status': proc.info['status']
                            }
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return {'running': False}
        except Exception as e:
            return {'running': False, 'error': str(e)}

    def read_cache_file(self):
        """读取缓存文件"""
        try:
            if not os.path.exists(self.cache_file):
                return None

            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            return cache_data
        except Exception as e:
            print(f"❌ 读取缓存文件失败: {e}")
            return None

    def analyze_recent_results(self, cache_data, check_count=10):
        """分析最近的结果"""
        try:
            results = cache_data.get('results', [])
            if not results:
                return {'has_results': False, 'reason': '暂无结果数据'}

            # 获取最近的结果
            recent = results[-check_count:] if len(results) >= check_count else results

            # 统计分析
            total = len(recent)
            all_failed_count = 0
            normal_business_count = 0
            success_count = 0

            product_names = []
            barcodes = []

            for result in recent:
                product_names.append(result.get('product_name', ''))
                barcodes.append(result.get('barcode', ''))

                # 判断识别结果类型
                if result.get('product_name') == "无法识别" and result.get('brand') == "无法识别":
                    all_failed_count += 1
                elif result.get('product_name') == "无此条码":
                    normal_business_count += 1
                else:
                    success_count += 1

            # 检查异常情况
            all_failed = (all_failed_count == total)
            unique_products = len(set(product_names))
            all_same = (unique_products == 1)
            unique_barcodes = len(set(barcodes))
            barcodes_changing = (unique_barcodes > min(3, total // 2))

            return {
                'has_results': True,
                'total_checked': total,
                'all_failed': all_failed,
                'all_same': all_same,
                'barcodes_changing': barcodes_changing,
                'statistics': {
                    'success_count': success_count,
                    'normal_business_count': normal_business_count,
                    'all_failed_count': all_failed_count,
                    'unique_products': unique_products,
                    'unique_barcodes': unique_barcodes
                },
                'recent_sample': recent[-3:] if len(recent) >= 3 else recent
            }

        except Exception as e:
            return {'has_results': False, 'error': str(e)}

    def check_cache_update_time(self, cache_data):
        """检查缓存更新时间"""
        try:
            file_mtime = os.path.getmtime(self.cache_file)
            time_diff = time.time() - file_mtime

            return {
                'last_update': file_mtime,
                'seconds_ago': int(time_diff),
                'minutes_ago': int(time_diff / 60),
                'status': 'updated' if time_diff < 3600 else 'stale'
            }
        except Exception as e:
            return {'error': str(e)}

    def detect_anomalies(self, cache_data):
        """检测异常情况"""
        anomalies = []

        # 1. 检查更新时间
        cache_status = self.check_cache_update_time(cache_data)
        if cache_status.get('minutes_ago', 0) > 30:
            anomaly_key = 'cache_not_updated'
            current_time = time.time()

            # 检查是否需要告警（防止重复）
            if anomaly_key not in self.last_alert_time or \
               current_time - self.last_alert_time[anomaly_key] > self.alert_cooldown:

                anomalies.append({
                    'type': '缓存长时间未更新',
                    'level': '🟠 警告',
                    'details': f"缓存文件{cache_status['minutes_ago']}分钟未更新",
                    'key': anomaly_key
                })

        # 2. 分析结果
        analysis = self.analyze_recent_results(cache_data)

        if not analysis.get('has_results'):
            return anomalies

        stats = analysis.get('statistics', {})

        # 检查是否全部"无法识别"
        if analysis.get('all_failed'):
            anomaly_key = 'all_failed'
            current_time = time.time()

            if anomaly_key not in self.last_alert_time or \
               current_time - self.last_alert_time[anomaly_key] > self.alert_cooldown:

                anomalies.append({
                    'type': '全部识别失败',
                    'level': '🔴 严重',
                    'details': f"最近{analysis['total_checked']}条结果全部'无法识别'",
                    'suggestion': '小程序可能崩溃，请立即检查！',
                    'key': anomaly_key
                })

        # 检查结果是否完全一样
        if analysis.get('all_same'):
            if analysis.get('barcodes_changing'):
                anomaly_key = 'stuck_same_page'
                current_time = time.time()

                if anomaly_key not in self.last_alert_time or \
                   current_time - self.last_alert_time[anomaly_key] > self.alert_cooldown:

                    anomalies.append({
                        'type': '结果完全相同',
                        'level': '🔴 严重',
                        'details': f"条码在变化但结果完全相同，可能卡在某个错误页面",
                        'suggestion': '脚本可能在重复相同错误，建议检查',
                        'key': anomaly_key
                    })

        # 检查失败比例
        if stats.get('all_failed_count', 0) >= 7:
            anomaly_key = 'high_failure_rate'
            current_time = time.time()

            if anomaly_key not in self.last_alert_time or \
               current_time - self.last_alert_time[anomaly_key] > self.alert_cooldown:

                anomalies.append({
                    'type': '失败比例过高',
                    'level': '🟡 关注',
                    'details': f"最近{analysis['total_checked']}条中有{stats['all_failed_count']}条'无法识别'",
                    'suggestion': '识别失败率较高，建议观察',
                    'key': anomaly_key
                })

        return anomalies

    def check_once(self, silent=False):
        """执行一次检查"""
        if not silent:
            print(f"\n🔍 [{datetime.now().strftime('%H:%M:%S')}] 监控检查...")

        # 1. 检查进程
        process_info = self.check_ocr_process()
        if not process_info.get('running'):
            if not silent:
                print("❌ OCR脚本未运行")
            return None

        if not silent:
            print("✅ OCR脚本运行中")

        # 2. 读取缓存
        cache_data = self.read_cache_file()
        if not cache_data:
            if not silent:
                print("❌ 无法读取缓存文件")
            return None

        # 3. 检测异常
        anomalies = self.detect_anomalies(cache_data)

        # 4. 发送告警邮件
        if anomalies and self.email_enabled:
            # 检查是否有新的严重异常需要告警
            severe_anomalies = [a for a in anomalies if '严重' in a['level']]
            if severe_anomalies:
                self.send_alert_email(severe_anomalies)

        # 5. 更新告警时间
        for anomaly in anomalies:
            if 'key' in anomaly:
                self.last_alert_time[anomaly['key']] = time.time()

        # 6. 显示报告
        if not silent:
            self.print_status_report(process_info, cache_data, anomalies)

        return anomalies

    def print_status_report(self, process_info, cache_data, anomalies):
        """打印状态报告"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(f"""
{'='*60}
🔍 OCR监控报告 - {timestamp}
{'='*60}

📊 基本信息
  主机名: {self.hostname}
  邮件告警: {'✅ 已启用' if self.email_enabled else '❌ 未启用'}

🖥️  进程状态
  运行状态: {'✅ 运行中' if process_info.get('running') else '❌ 未运行'}""")

        if process_info.get('running'):
            print(f"  进程PID: {process_info.get('pid')}")

        # 缓存信息
        if cache_data:
            cache_status = self.check_cache_update_time(cache_data)
            analysis = self.analyze_recent_results(cache_data)

            print(f"""
📁 缓存文件状态
  最后更新: {cache_status.get('minutes_ago', 0)}分钟前

📈 处理进度
  当前索引: {cache_data.get('current_index', 0)}
  结果数量: {len(cache_data.get('results', []))}""")

            if analysis.get('has_results'):
                stats = analysis.get('statistics', {})
                print(f"""
📊 最近{analysis['total_checked']}条分析
  成功识别: {stats.get('success_count', 0)}条
  正常业务失败: {stats.get('normal_business_count', 0)}条
  完全无法识别: {stats.get('all_failed_count', 0)}条""")

        # 异常情况
        if anomalies:
            print(f"\n🚨 异常情况 ({len(anomalies)}项):")
            for i, anomaly in enumerate(anomalies, 1):
                print(f"  {i}. {anomaly['level']} {anomaly['type']}")
                if self.email_enabled:
                    print(f"     📧 已发送告警邮件")
        else:
            print("\n✅ 未发现异常情况")

        print(f"{'='*60}")

    def run(self, check_interval=60, silent=False):
        """运行监控"""
        print(f"""
🚀 OCR监控系统启动
{'='*60}
检查间隔: {check_interval}秒
邮件告警: {'✅ 已启用' if self.email_enabled else '❌ 未启用'}
缓存文件: {self.cache_file}
主机名: {self.hostname}
{'='*60}
按 Ctrl+C 停止监控
        """)

        try:
            while True:
                # 执行检查
                self.check_once(silent=silent)

                # 等待下次检查
                if not silent:
                    print(f"⏰ 下次检查在{check_interval}秒后...")
                time.sleep(check_interval)

        except KeyboardInterrupt:
            print("\n\n👋 监控已停止")
        except Exception as e:
            print(f"\n❌ 监控出错: {e}")

def save_email_config(config):
    """保存邮件配置"""
    try:
        # 保存为通用配置文件名
        with open('monitor_email_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print("✅ 邮件配置已保存到 monitor_email_config.json")
        return True
    except Exception as e:
        print(f"❌ 保存配置失败: {e}")
        return False

def load_email_config():
    """加载邮件配置"""
    try:
        # 优先查找通用配置文件
        if os.path.exists('monitor_email_config.json'):
            with open('monitor_email_config.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        # 兼容旧的配置文件名
        elif os.path.exists('monitor_config.json'):
            with open('monitor_config.json', 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
    return None

def main():
    """主函数"""
    print("""
    🔍 OCR监控系统（完整版）

    功能：实时监控OCR脚本 + 邮件告警
    """)

    # 尝试加载配置
    config = load_email_config()

    if config:
        print("📧 发现已保存的邮件配置")
        use_saved = input("是否使用已保存的配置？(y/n): ").strip().lower()
        if use_saved != 'y':
            config = None

    # 创建监控实例
    monitor = OCRMonitor(email_config=config)

    # 配置邮件
    if not config:
        setup = input("是否配置邮件告警？(y/n): ").strip().lower()
        if setup == 'y':
            if monitor.setup_email_config():
                # 保存配置
                config_to_save = {
                    'enabled': True,
                    'smtp_server': monitor.email_config['smtp_server'],
                    'smtp_port': monitor.email_config['smtp_port'],
                    'from_email': monitor.email_config['from_email'],
                    'to_email': monitor.email_config['to_email'],
                    'password': monitor.email_config['password']
                }
                save_email_config(config_to_save)
            else:
                print("⚠️ 邮件配置失败，将继续运行但不发送邮件")

    # 启动监控
    print("\n选择运行模式：")
    print("1. 单次检查（测试）")
    print("2. 持续监控")

    mode = input("请选择 (1-2): ").strip()

    if mode == '1':
        monitor.check_once()
    else:
        interval = input("检查间隔（秒，默认60）: ").strip()
        check_interval = int(interval) if interval.isdigit() else 60
        monitor.run(check_interval=check_interval)

if __name__ == "__main__":
    main()
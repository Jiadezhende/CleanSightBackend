#!/usr/bin/env python3
"""
Nginx RTMP 服务的 Python 安装和管理脚本
提供安装、配置、启动、停止等功能
"""

import os
import sys
import subprocess
import logging
import argparse
from pathlib import Path
from typing import Optional, Dict, Any
import json
import time

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('nginx_rtmp_setup.log')
    ]
)
logger = logging.getLogger(__name__)

class NginxRTMPManager:
    """Nginx RTMP 服务管理器"""
    
    def __init__(self):
        self.script_dir = Path(__file__).parent
        self.project_root = self.script_dir.parent
        self.config_file = self.script_dir / "nginx.conf"
        self.install_script = self.script_dir / "install_nginx_rtmp.sh"
        
    def check_system(self) -> Dict[str, Any]:
        """检查系统环境"""
        logger.info("检查系统环境...")
        
        system_info = {
            'platform': sys.platform,
            'is_linux': sys.platform.startswith('linux'),
            'is_root': os.geteuid() == 0 if hasattr(os, 'geteuid') else False,
            'has_systemctl': self._command_exists('systemctl'),
            'has_nginx': self._command_exists('nginx'),
            'nginx_version': self._get_nginx_version()
        }
        
        logger.info(f"系统信息: {json.dumps(system_info, indent=2)}")
        return system_info
    
    def _command_exists(self, command: str) -> bool:
        """检查命令是否存在"""
        try:
            subprocess.run(['which', command], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False
    
    def _get_nginx_version(self) -> Optional[str]:
        """获取nginx版本"""
        try:
            result = subprocess.run(['nginx', '-v'], capture_output=True, text=True)
            version_line = result.stderr.strip()
            if 'nginx/' in version_line:
                return version_line.split('nginx/')[1].split(' ')[0]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return None
    
    def _run_command(self, command: list, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
        """运行系统命令"""
        logger.info(f"执行命令: {' '.join(command)}")
        try:
            result = subprocess.run(command, check=check, capture_output=capture_output, text=True)
            if result.stdout:
                logger.debug(f"命令输出: {result.stdout}")
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"命令执行失败: {e}")
            if e.stderr:
                logger.error(f"错误输出: {e.stderr}")
            raise
    
    def install_nginx_rtmp(self, force: bool = False) -> bool:
        """安装nginx rtmp服务"""
        logger.info("开始安装 Nginx RTMP 服务...")
        
        system_info = self.check_system()
        
        if not system_info['is_linux']:
            logger.error("此脚本仅支持Linux系统")
            return False
        
        if not system_info['is_root']:
            logger.error("需要root权限运行安装脚本")
            logger.info("请使用: sudo python3 nginx_rtmp_manager.py install")
            return False
        
        # 检查是否已安装nginx rtmp
        if system_info['has_nginx'] and not force:
            logger.warning("检测到已安装nginx，使用 --force 强制重新安装")
            return False
        
        # 检查安装脚本是否存在
        if not self.install_script.exists():
            logger.error(f"安装脚本不存在: {self.install_script}")
            return False
        
        # 设置脚本执行权限
        os.chmod(self.install_script, 0o755)
        
        try:
            # 运行安装脚本
            self._run_command(['bash', str(self.install_script)], capture_output=False)
            logger.info("Nginx RTMP 安装完成")
            return True
        except subprocess.CalledProcessError:
            logger.error("Nginx RTMP 安装失败")
            return False
    
    def configure_nginx(self) -> bool:
        """配置nginx"""
        logger.info("配置nginx...")
        
        if not self.config_file.exists():
            logger.error(f"配置文件不存在: {self.config_file}")
            return False
        
        try:
            # 备份原配置文件
            backup_path = Path("/etc/nginx/nginx.conf.backup")
            if Path("/etc/nginx/nginx.conf").exists() and not backup_path.exists():
                self._run_command(['cp', '/etc/nginx/nginx.conf', str(backup_path)])
                logger.info("已备份原配置文件")
            
            # 复制新配置文件
            self._run_command(['cp', str(self.config_file), '/etc/nginx/nginx.conf'])
            
            # 测试配置文件
            self._run_command(['nginx', '-t'])
            
            logger.info("nginx配置完成")
            return True
        except subprocess.CalledProcessError:
            logger.error("nginx配置失败")
            return False
    
    def start_service(self) -> bool:
        """启动nginx服务"""
        logger.info("启动nginx服务...")
        
        try:
            self._run_command(['systemctl', 'start', 'nginx'])
            
            # 检查服务状态
            if self.is_service_running():
                logger.info("nginx服务启动成功")
                return True
            else:
                logger.error("nginx服务启动失败")
                return False
        except subprocess.CalledProcessError:
            logger.error("无法启动nginx服务")
            return False
    
    def stop_service(self) -> bool:
        """停止nginx服务"""
        logger.info("停止nginx服务...")
        
        try:
            self._run_command(['systemctl', 'stop', 'nginx'])
            logger.info("nginx服务已停止")
            return True
        except subprocess.CalledProcessError:
            logger.error("无法停止nginx服务")
            return False
    
    def restart_service(self) -> bool:
        """重启nginx服务"""
        logger.info("重启nginx服务...")
        
        try:
            self._run_command(['systemctl', 'restart', 'nginx'])
            
            # 等待服务启动
            time.sleep(2)
            
            if self.is_service_running():
                logger.info("nginx服务重启成功")
                return True
            else:
                logger.error("nginx服务重启失败")
                return False
        except subprocess.CalledProcessError:
            logger.error("无法重启nginx服务")
            return False
    
    def reload_config(self) -> bool:
        """重载nginx配置"""
        logger.info("重载nginx配置...")
        
        try:
            # 先测试配置
            self._run_command(['nginx', '-t'])
            
            # 重载配置
            self._run_command(['systemctl', 'reload', 'nginx'])
            logger.info("nginx配置重载成功")
            return True
        except subprocess.CalledProcessError:
            logger.error("nginx配置重载失败")
            return False
    
    def is_service_running(self) -> bool:
        """检查nginx服务是否运行"""
        try:
            result = self._run_command(['systemctl', 'is-active', 'nginx'], check=False)
            return result.returncode == 0 and result.stdout.strip() == 'active'
        except subprocess.CalledProcessError:
            return False
    
    def get_service_status(self) -> Dict[str, Any]:
        """获取服务状态"""
        logger.info("获取服务状态...")
        
        status = {
            'running': self.is_service_running(),
            'enabled': False,
            'nginx_version': self._get_nginx_version(),
            'config_valid': False,
            'ports_open': {}
        }
        
        # 检查服务是否启用
        try:
            result = self._run_command(['systemctl', 'is-enabled', 'nginx'], check=False)
            status['enabled'] = result.returncode == 0 and result.stdout.strip() == 'enabled'
        except subprocess.CalledProcessError:
            pass
        
        # 检查配置是否有效
        try:
            self._run_command(['nginx', '-t'])
            status['config_valid'] = True
        except subprocess.CalledProcessError:
            pass
        
        # 检查端口是否开放
        for port in [80, 1935]:
            try:
                result = self._run_command(['ss', '-tlnp'], check=False)
                status['ports_open'][port] = f':{port}' in result.stdout
            except subprocess.CalledProcessError:
                status['ports_open'][port] = False
        
        return status
    
    def show_service_info(self):
        """显示服务信息"""
        print("\n" + "="*50)
        print("Nginx RTMP 服务信息")
        print("="*50)
        
        status = self.get_service_status()
        
        print(f"服务状态: {'运行中' if status['running'] else '已停止'}")
        print(f"开机启动: {'已启用' if status['enabled'] else '未启用'}")
        print(f"Nginx版本: {status['nginx_version'] or '未安装'}")
        print(f"配置有效: {'是' if status['config_valid'] else '否'}")
        
        print("\n端口状态:")
        for port, is_open in status['ports_open'].items():
            port_name = "HTTP" if port == 80 else "RTMP" if port == 1935 else str(port)
            print(f"  {port_name}({port}): {'开放' if is_open else '关闭'}")
        
        # 获取IP地址
        try:
            result = self._run_command(['hostname', '-I'])
            ip_addresses = result.stdout.strip().split()
            if ip_addresses:
                print(f"\n服务地址:")
                for ip in ip_addresses[:2]:  # 只显示前两个IP
                    print(f"  RTMP推流: rtmp://{ip}:1935/live")
                    print(f"  统计页面: http://{ip}/stat")
                    print(f"  健康检查: http://{ip}/health")
        except subprocess.CalledProcessError:
            pass
        
        print("\n常用命令:")
        print("  python3 nginx_rtmp_manager.py start   - 启动服务")
        print("  python3 nginx_rtmp_manager.py stop    - 停止服务")
        print("  python3 nginx_rtmp_manager.py restart - 重启服务")
        print("  python3 nginx_rtmp_manager.py reload  - 重载配置")
        print("  python3 nginx_rtmp_manager.py status  - 查看状态")
        print("="*50)

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='Nginx RTMP 服务管理器')
    parser.add_argument('action', choices=[
        'install', 'configure', 'start', 'stop', 'restart', 
        'reload', 'status', 'info'
    ], help='要执行的操作')
    parser.add_argument('--force', action='store_true', help='强制执行操作')
    
    args = parser.parse_args()
    
    manager = NginxRTMPManager()
    
    try:
        if args.action == 'install':
            success = manager.install_nginx_rtmp(force=args.force)
            if success:
                # 安装后自动配置
                manager.configure_nginx()
                manager.show_service_info()
        elif args.action == 'configure':
            manager.configure_nginx()
        elif args.action == 'start':
            manager.start_service()
        elif args.action == 'stop':
            manager.stop_service()
        elif args.action == 'restart':
            manager.restart_service()
        elif args.action == 'reload':
            manager.reload_config()
        elif args.action == 'status':
            status = manager.get_service_status()
            print(json.dumps(status, indent=2))
        elif args.action == 'info':
            manager.show_service_info()
        
    except KeyboardInterrupt:
        logger.info("操作被用户中断")
    except Exception as e:
        logger.error(f"操作失败: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
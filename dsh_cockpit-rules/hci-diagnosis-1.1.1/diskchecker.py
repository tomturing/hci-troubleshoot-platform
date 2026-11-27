#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import json
import traceback
import os
import sys
import re
import argparse
import subprocess
import tarfile
import time

g_package_version = False
try:
    from packaging import version
    g_package_version = True
except ImportError:
    g_package_version = False

try:
    from loguru import logger
except ImportError:
    #使用logging代替logger输出
    import logging
    logger = logging.getLogger(__name__)
    '''
    logger.error("当前没有安装loguru模块，请先安装loguru模块！\n"
          "pip install loguru\n"
          "pip3 install loguru\n")
    '''

logger_filename = "diskchecker.log"
g_args = None
g_vs_disks = None

def run_cmd(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    try:
        p = subprocess.Popen(cmd, shell=shell, stdout=stdout, stderr=stderr)
        out, err = p.communicate()
        if p.returncode == 0 or (out and len(out)):
            return out.decode("utf-8").strip()
        else:
            logger.error("run {0} error: {1}".format(cmd, err))
    except Exception as e:
        logger.error("run {0} error: {1}".format(cmd, e))

    return None

def extract_tar_gz(file_path, destination_dir, spec_file=None):
    files = []
    try:
        with tarfile.open(file_path, "r:gz") as tar:
            tar.errorlevel = 0
            if spec_file:
                tar.extract(spec_file, destination_dir)
                files = [os.path.join(destination_dir, f) for f in tar.getnames()]

            else:
                tar.extractall(path=destination_dir)
                files = [member.name for member in tar.getmembers() if member.isreg()]

            #logger.info("extraced {} to {}".format(file_path, destination_dir))        
            return files
    except Exception as e:
        traceback.print_exc()

def ensure_unicode(obj):
    if isinstance(obj, dict):
        return {ensure_unicode(k): ensure_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [ensure_unicode(item) for item in obj]
    elif isinstance(obj, str):  # 处理字节串（Python 2.x 中的 `str`）
        try:
            return obj.decode('utf-8')
        except UnicodeDecodeError:
            # 如果无法以 UTF-8 解码，可以尝试其他编码方式（如 GBK 或 GB2312）
            # 根据你的数据实际编码选择合适的解码方式
            return obj.decode('gbk', 'replace')  # 替换不可解码的字符
    elif isinstance(obj, unicode):  # 如果已经是 Unicode，直接返回
        return obj
    else:
        return obj

#从字符串中根据正则表达式查找
def search_section(log_info, search_str, mode=re.DOTALL, group=1, multi=0):
    if not search_str or not log_info:
        return None

    #if g_args and g_args.verbose:
    #    logger.info("search str:{}, mode:{}, group:{}, multi:{}".format(search_str, mode, group, multi))

    kv_pattern = re.compile(search_str, mode)
    if multi == 0:
        value = kv_pattern.search(log_info)
    else:
        value = kv_pattern.findall(log_info)
        #if g_args and g_args.verbose:
        #    logger.info(value)
        return value
    if value:
        if group == -1:
            #-1返回value
            #if g_args and g_args.verbose:
            #    logger.info(value)
            return value
        else:
            #if g_args and g_args.verbose:
            #    logger.info(value)
            return value.group(group).strip()
    else:
        return None

def get_hci_version():
    cmd = "cat /sf/version |head -n 1"
    ret = run_cmd(cmd)
    if ret:
        ver = ret.decode("utf-8").strip()
        return ver
    
    return None

def split_version_part(part):
    # 提取数字前缀
    num_match = re.match(r'^(\d+)', part)
    if num_match:
        num = int(num_match.group(1))
        suffix = part[num_match.end():]
    else:
        num = 0
        suffix = part

    known_suffix_order = {
        'a': 0, 'alpha': 0,
        'b': 1, 'beta': 1,
        'rc': 2,
        'post': 3,
        'rev': 4,
        'final': 5,
        '': 5
    }

    # 提取已知的后缀类型
    suffix_type = ''
    if suffix:
        suffix_match = re.match(r'^([a-za-z]+)', suffix)
        if suffix_match:
            suffix_type = suffix_match.group(1).lower()
            if suffix_type in known_suffix_order:
                return (num, known_suffix_order[suffix_type])

    # 如果后缀类型未知，按字符串比较
    return (num, suffix)

def compare_versions(v1, v2):
    parts1 = v1.split('.')
    parts2 = v2.split('.')

    for i in range(max(len(parts1), len(parts2))):
        part1 = parts1[i] if i < len(parts1) else ''
        part2 = parts2[i] if i < len(parts2) else ''

        num1, suffix_order1 = split_version_part(part1)
        num2, suffix_order2 = split_version_part(part2)

        # 先比较数字部分
        if num1 != num2:
            return (num1 > num2) - (num1 < num2)

        # 数字相同，再比较后缀的优先级
        if isinstance(suffix_order1, int) and isinstance(suffix_order2, int):
            if suffix_order1 != suffix_order2:
                return (suffix_order1 > suffix_order2) - (suffix_order1 < suffix_order2)
        else:
            # 如果其中一个后缀类型未知，按字符串比较方式处理
            # 将后缀部分转换为字符串进行比较
            if isinstance(suffix_order1, int):
                suffix_str1 = "{:04d}".format(suffix_order1)  # 转换为固定长度字符串方便排序
                suffix_str2 = str(suffix_order2)
            elif isinstance(suffix_order2, int):
                suffix_str1 = str(suffix_order1)
                suffix_str2 = "{:04d}".format(suffix_order2)
            else:
                suffix_str1 = str(suffix_order1)
                suffix_str2 = str(suffix_order2)

            if suffix_str1 != suffix_str2:
                # 比较字符串，按字典序排序
                if suffix_str1 > suffix_str2:
                    return 1
                else:
                    return -1

    # 所有部分都相同，则版本号相等
    return 0

# 6.7.0以上版本需要通过container执行命令
def check_use_container_version(version):
    if not version:
        return False
    
    if g_package_version:
        if version.parse(version) >= version.parse("6.7.0"):
            return True
        else:
            return False
    else:
        #去掉版本号后面的_字符
        version = version.split('_')[0]
        v1 = version
        v2 = "6.7.0"
        return compare_versions(v1, v2) >= 0
    
def load_vs_disks():
    dir = "/sf/cfg/vs/disk"
    if not os.path.exists(dir):
        return None
    
    disks = {}
    for file in os.listdir(dir):
        if file.endswith(".json"):
            filename = file[:-5]
            with open(os.path.join(dir, file), "r") as f:
                disks[ filename ] = json.load(f)
    return disks

def get_diskname_by_sn_from_vsconfig(sn):
    if not sn:
        return None

    if not g_vs_disks:
        return
    
    for diskid, value in g_vs_disks.items():
        if sn == value.get("disk_sn").strip():
            return value.get("dev")
        
    return None

def get_sn_by_diskname_from_vsconfig(diskname):
    if not diskname:
        return None

    if not g_vs_disks:
        return
    
    for diskid, value in g_vs_disks.items():
        if diskname == value.get("dev").strip():
            return value.get("disk_sn").strip()
        
    return None

#检查是否为VS使用的磁盘
def check_vs_use_disk(smartctl):
    if not smartctl or not g_vs_disks:
        return False
    
    for diskid, value in g_vs_disks.items():
        if smartctl.get("Serial Number") == value.get("disk_sn").strip():
            if g_args.verbose:
                logger.info("vs disk find: diskid:{}, disk_sn:{}, disk name:{}".format(diskid, value.get("disk_sn"), value.get("dev")))
            return True
    
    return False

def get_vs_disk_info(smartctl):
    if not smartctl or not g_vs_disks:
        return None
    
    for diskid, value in g_vs_disks.items():
        if smartctl.get("Serial Number") == value.get("disk_sn").strip():
            if g_args.verbose:
                logger.info("vs disk find: diskid:{}, disk_sn:{}, disk name:{}".format(diskid, value.get("disk_sn"), value.get("dev")))
            return value
    
    return None

def get_vs_disk_location(smartctl):
    value = get_vs_disk_info(smartctl)
    if value:
        return value.get("disk_location")
    
    return None

def parse_smartctl_info(smartinfo):
    #解析smartctl信息
    info = {}
    info["Device Model"] = search_section(smartinfo, r'Device Model:(.*?)\n')
    info["Model Number"] = search_section(smartinfo, r'Model Number:(.*?)\n')
    if not info["Device Model"]:
        info["Device Model"] = search_section(smartinfo, r'Product:(.*?)\n')
        if not info["Device Model"]:
            info["Device Model"] = info["Model Number"]
    info["Serial Number"] = search_section(smartinfo, r'Serial Number:(.*?)\n')
    if not info["Serial Number"]:
        info["Serial Number"] = search_section(smartinfo, r'Serial number:(.*?)\n')
    info["LU WWN Device Id"] = search_section(smartinfo, r'LU WWN Device Id:(.*?)\n')
    info["Firmware Version"] = search_section(smartinfo, r'Firmware Version:(.*?)\n')
    if not info["Firmware Version"]:
        info["Firmware Version"] = search_section(smartinfo, r'Revision:(.*?)\n')
    info["User Capacity"] = search_section(smartinfo, r'User Capacity:(.*?)\n')
    #NVME容量
    info["Total NVM Capacity"] = search_section(smartinfo, r"Total NVM Capacity:(.*?)\n")
    if not info["User Capacity"]:
        info["User Capacity"] = info["Total NVM Capacity"]

    #转换为可视容量
    if info.get("User Capacity"):
        info["Human Capacity"] = search_section(info["User Capacity"], r'\[(.*?)\]', group=1)

    if info.get("Total NVM Capacity"):
        info["NVME SMART Attributes"] = search_section(smartinfo, r'=== START OF SMART DATA SECTION ===\n(.*)', re.DOTALL, group=1)
        if info["NVME SMART Attributes"]:
            records = info["NVME SMART Attributes"].split('\n')
            record = {}
            for r in records:
                if r == "":
                    continue
                ks = r.split(':')
                k = ks[0].strip()
                v = ks[-1].strip()
                record[k] = v
            info["NVME SMART Attributes Values"] = record

    info["Sector Sizes"] = search_section(smartinfo, r'Sector Sizes:(.*?)\n')
    info["Rotation Rate"] = search_section(smartinfo, r'Rotation Rate:(.*?)\n')
    #SAS接口可以查接口协议
    info["Transport protocol"] = search_section(smartinfo, r'Transport protocol:(.*?)\n')
    if info["Transport protocol"]:
        #读取sas的属性
        info["SAS SMART Attributes"] = search_section(smartinfo, r'=== START OF READ SMART DATA SECTION ===\n(.*)', re.DOTALL, group=1)
        if info["SAS SMART Attributes"]:
            records = info["SAS SMART Attributes"].split('\n')
            record = {}
            for r in records:
                if r == "":
                    continue
                ks = re.split(r'[:=]', r)
                k = ks[0].strip()
                v = ks[-1].strip()
                record[k] = v
            info["SAS SMART Attributes Values"] = record

    info["Form Factor"] = search_section(smartinfo, r'Form Factor:(.*?)\n')
    info["Device is"] = search_section(smartinfo, r'Device is:(.*?)\n')
    info["ATA Version is"] = search_section(smartinfo, r'ATA Version is:(.*?)\n')
    info["SATA Version is"] = search_section(smartinfo, r'SATA Version is:(.*?)\n')
    info["Local Time is"] = search_section(smartinfo, r'Local Time is:(.*?)\n')
    info["SMART -H"] = search_section(smartinfo, r'SMART overall-health self-assessment test result:(.*?)\n')
    info["SSTR_RPT"] = search_section(smartinfo, r'Short self-test routine \nrecommended polling time:(.*?)\n')
    info["ESTR_RPT"] = search_section(smartinfo, r'Extended self-test routine\nrecommended polling time:(.*?)\n')
    info["SMART Attributes"] = search_section(smartinfo, r'ID#(.*?)\n\n', re.DOTALL, group=0)

    #解析smartctl属性
    if info["SMART Attributes"]:
        # 去除标题行并分割字符串为行列表
        lines = info["SMART Attributes"].strip().split('\n')[1:]

        # 解析数据并创建字典列表
        attributes = []
        for line in lines:
            parts = line.split()
            attribute = {
                'ID': parts[0],
                'ATTRIBUTE_NAME': parts[1],
                'FLAG': parts[2],
                'VALUE': parts[3],  # 假设VALUE是整数
                'WORST': parts[4],
                'THRESH': parts[5],
                'TYPE': parts[6],
                'UPDATED': parts[7],
                'WHEN_FAILED': parts[8],
                'RAW_VALUE': parts[9]  # 处理非数字RAW_VALUE
            }
            attributes.append(attribute)
        
        info["SMART Attributes List"] = attributes

    info["SMART Error Log"] = search_section(smartinfo, r'(SMART Error Log Version:)(\s+)(\d+)\n(.*?)\nSMART Self-test log structure revision number', re.DOTALL, group=0)

    values = search_section(smartinfo, r'SMART Self-test log structure revision number \d\n(.*?)SMART Selective self-test log data structure revision number \d\n(.*?)Selective self-test flags', re.DOTALL, group=-1, multi=1)
    if values:
        info["SMART Self-test"] = {
            "Self-test Record": parse_short_test(values[0][0]),
            "Self-test Data Struct": values[0][1]
        }

    return info

def parse_short_test(value):
    #解析short test
    value = value.strip()
    if search_section(value, "No self-tests have been logged.", re.DOTALL, group=0):
        return None
    #Num  Test_Description    Status                  Remaining  LifeTime(hours)  LBA_of_first_error
    # 1  Short offline       Self-test routine in progress 50%     40210         -
    # 2  Short offline       Completed without error       00%     40209 
    #
    lines = value.split('\n')
    return lines

#获取硬盘接口类型
def get_disk_protocol(smartctl):
    if not smartctl:
        return None

    proto_type = smartctl.get("Transport protocol")
    if proto_type:
        if proto_type.find("SAS") != -1:
            proto_type = "SAS"
    if smartctl.get("SATA Version is"):
        if smartctl.get("SATA Version is").find("SATA") != -1:
            proto_type = "SATA"
            
    if smartctl.get("Total NVM Capacity"):
        proto_type = "NVME"

    #默认SATA
    if not proto_type:
        proto_type = "SATA"

    return proto_type

def check_cmd_exists(cmd_util):
    cmd = "which {0}".format(cmd_util)
    ret = run_cmd(cmd)
    if ret:
        return True
    else:
        return False
    
def update_value(obj, key, value, ignore_exists=True, type="object", insert_pos=-1):

    if obj.get(key):
        if isinstance(obj.get(key), list):
            if isinstance(value, list):
                for v in value:
                    if ignore_exists:
                        if not v in obj[key]:
                            if insert_pos == -1:
                                obj[key].append(v)
                            else:
                                obj[key].insert(insert_pos, v)
                    else:
                        if insert_pos == -1:
                            obj[key].append(v)
                        else:
                            obj[key].insert(insert_pos, v)
            else:
                if ignore_exists:
                    if not value in obj[key]:
                        if insert_pos == -1:
                            obj[key].append(value)
                        else:
                            obj[key].insert(insert_pos, value)
                else:
                    if insert_pos == -1:
                        obj[key].append(value)
                    else:
                        obj[key].insert(insert_pos, value)
        else:
            if obj.get(key) != value:
                obj[key] = [obj.get(key), value]
    else:
        if type == "list":
            if isinstance(value, list):
                obj[key] = value
            else:
                obj[key] = [value]
        else:
            obj[key] = value

    return value

def get_disk_smartctl_info(diskname, params="-a"):
    smartctl_util = "smartctl.real"
    #如果smartctl.real存在，就用smartctl.real
    if not check_cmd_exists(smartctl_util):
        smartctl_util = "smartctl"
        if not check_cmd_exists(smartctl_util):
            logger.error("smartctl not exists.")
            #检查是不是需要在容器里执行
            if check_use_container_version(get_hci_version()):
                logger.error("smartctl not exists, mabe in container.")
                #TODO 是否需要通过容器执行，目前python执行是进VT容器的话就不用了
                return None
            #命令不存在直接返回
            return None
    
    cmd = "{0} {1} /dev/{2}".format(smartctl_util, params, diskname)
    ret = run_cmd(cmd)
    if not ret:
        logger.info("get disk {0} smartctl info error.".format(diskname))
        return None
    
    return parse_smartctl_info(ret)

def check_smartctl_H(smartctl_info, ret):
    #检测smartctl -H
    if smartctl_info.get("SMART -H"):
        if smartctl_info.get("SMART -H") != "PASSED":
            logger.error("smartctl -H check:{}.".format(smartctl_info.get("SMART -H")))
            update_value(ret, "返修建议", "SMART -H 检查结果为:{}，建议返修。".format(smartctl_info.get("SMART -H")))

def check_smartctl_5_197(smartctl_info, item, is_vs_disk, ret):
    if not smartctl_info:
        return
    
    smartctl_attrs = smartctl_info.get("SMART Attributes List")
    if smartctl_attrs:
        value = get_smartctl_attr(smartctl_attrs, item)
        capacity = smartctl_info.get("Human Capacity")
        values = search_section(capacity, r'(\S+)\s+GB', group=1)
        if values:
            #4T以下
            alert = 100
            if is_vs_disk:
                alert = 200
            if int(value) >= alert:
                update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)

        values = search_section(capacity, r'(\S+)\s+TB', group=1)
        if values:
            size = int(values[0])
            if size < 4:
                #4T以下
                alert = 100
                if is_vs_disk:
                    alert = 200
                if int(value) >= alert:
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)
            elif size == 4:
                #4T
                alert = 200
                if is_vs_disk:
                    alert = 400
                if int(value) >= alert:
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)
            elif 4 < size and size <= 6:
                #6T
                alert = 200
                if is_vs_disk:
                    alert = 400
                if int(value) >= alert:
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)
            elif 6 < size and size < 10:
                #8T
                alert = 250
                if is_vs_disk:
                    alert = 500
                if int(value) >= alert:
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)
            elif size >= 10:
                #10T
                alert = 250
                if is_vs_disk:
                    alert = 500
                if int(value) >= alert:
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，建议返修。".format(item, value), ignore_exists=False)

        #TODO
        #不包含GB和TB的情况,暂时不用处理
        pass

def check_smartctl_199(smartctl_info, ret):
    if not smartctl_info:
        return
    
    smartctl_attrs = smartctl_info.get("SMART Attributes List")
    if not smartctl_attrs:
        return
    
    #检查smartctl 199项24小时内是否有增加1以上
    if g_args.date:
        filename = "/sf/log/blackbox/{0}/LOG_vs_smartctl.txt".format(g_args.date)
        #需要确保文件存在，不存在则解压
        if not os.path.exists(filename):
            if not os.path.exists("/sf/log/blackbox/{}.tar.gz".format(g_args.date)):
                #指定日期不存在
                logger.error("指定日期不存在！")
                return
            #解压
            extract_tar_gz("/sf/log/blackbox/{}.tar.gz".format(g_args.date), "/sf/log/blackbox", "{}/LOG_vs_smartctl.txt".format(g_args.date))
    else:
        filename = "/sf/log/blackbox/today/LOG_vs_smartctl.txt"

    his_smartctl_info = get_his_smartctl(filename)
    if not his_smartctl_info:
        return
    
    for diskname, his_info in his_smartctl_info.items():
        if his_info.get("Serial Number") == smartctl_info.get("Serial Number"):
            his_smartctl_attrs = his_info.get("SMART Attributes List")
            if not his_smartctl_attrs:
                return
            
            his_199 = get_smartctl_attr(his_smartctl_attrs, "199")
            cur_199 = get_smartctl_attr(smartctl_attrs, "199")
            try:
                if int(his_199) < int(cur_199):
                    update_value(ret, "返修建议", "SMART 199项检查24小时内有增加(当前值：{})，建议更换数据线和背板".format(cur_199), ignore_exists=False)
            except Exception as e:
                traceback()

def check_smartctl_5_increase(smartctl_info, ret):
    if not smartctl_info:
        return

    smartctl_attrs = smartctl_info.get("SMART Attributes List")
    if not smartctl_attrs:
        return
    
    #列出当前日志目录最早的日期
    dir = "/sf/log/blackbox"
    if not os.path.exists(dir):
        return
    
    files = os.listdir(dir)

    blackboxday = []
    for file in files:
        if os.path.isfile(os.path.join(dir,file)) and not file.endswith(".tar.gz"):
            #忽略非日期目录或文件
            continue

        if os.path.isdir(os.path.join(dir,file)) and not file.isdigit():
            continue

        blackboxday.append(file)
    
    if len(blackboxday) == 0:
        return
    
    blackboxday = sorted(blackboxday, reverse=False)
    #取最早的日期
    for i in range(0, len(blackboxday)):
        path = os.path.join(dir, blackboxday[i])
        if os.path.isfile(path):
            #先解压LOG_vs_smartctl.txt
            extract_tar_gz(path, dir, "{}/LOG_vs_smartctl.txt".format(blackboxday[i][:-7]))
            filename = os.path.join(path[:-7], "LOG_vs_smartctl.txt")
        else:
            filename = os.path.join(path, "LOG_vs_smartctl.txt")

        #读取历史信息
        his_smartctl_info = get_his_smartctl(filename)
        if not his_smartctl_info:
            #读取不到则下一个日期
            continue

        if g_args.verbose:
            logger.info("对比日期:{}的历史值.".format(filename))

        for diskname, his_info in his_smartctl_info.items():
            if his_info.get("Serial Number") == smartctl_info.get("Serial Number"):
                his_smartctl_attrs = his_info.get("SMART Attributes List")
                if not his_smartctl_attrs:
                    if g_args.verbose:
                        logger.info("his smartctl找不到SMART Attributes List：{}".format(json.dumps(his_smartctl_info, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))
                    continue

                his_5 = get_smartctl_attr(his_smartctl_attrs, "5")
                cur_5 = get_smartctl_attr(smartctl_attrs, "5")
                try:
                    if int(cur_5) - int(his_5) >= 10:
                        update_value(ret, "返修建议", "检查SMARTCTL {}的值一个月内增加大于10个，当前值:{}，最早历史值:{}，建议返修。".format(5, cur_5, his_5), ignore_exists=False)
                except Exception as e:
                    traceback()
        #只需要对比最早的日期
        break

def check_smartctl_alert(smartctl_info, ret, type="Pre-fail"):
    if not smartctl_info:
        return
    
    smartctl_attrs = smartctl_info.get("SMART Attributes List")
    if not smartctl_attrs:
        return
    
    for attr in smartctl_attrs:
        if attr["TYPE"] == "Pre-fail":
            if attr["THRESH"] == "---":
                continue

            try:
                if int(attr["WORST"]) <= int(attr["THRESH"]):
                    update_value(ret, "返修建议", "SMART {} 检查结果为:{}，低于指导值:{}，建议返修。".format(attr["ID"], attr["WORST"], attr["THRESH"]), ignore_exists=False)
            except Exception as e:
                traceback.print_exc()

        if attr["WHEN_FAILED"] == "FAILING_NOW" or attr["WHEN_FAILED"] == "In_the_past":
            update_value(ret, "返修建议", "SMARTCTL {} 检查结果为:{}，建议返修。".format(attr["ID"], attr["WHEN_FAILED"]), ignore_exists=False)

#寿命检查
def check_disk_lifespan(smartctl_info, ret, diskname=None):
    if not smartctl_info:
        return
    #只有SSD才需要检查寿命
    if smartctl_info.get("Rotation Rate") and smartctl_info.get("Rotation Rate").find("rpm") != -1:
        return
    
    #如果是NVME接口
    if smartctl_info.get("Total NVM Capacity"):
        if diskname:
            cmd = "nvme smart-log /dev/{}".format(diskname)
            nvmelog = run_cmd(cmd)
            if not nvmelog:
                return
            lines = nvmelog.split("\n")
            nvme = {
                "dev": diskname
            }
            for v in lines:
                parts = v.split(":")
                nvme[parts[0].strip()] = parts[1].strip()
            if nvme.get("percentage_used"):
                percent = int(nvme.get("percentage_used")[:-1])
                if percent >= 90:
                    update_value(ret, "返修建议", "NVME磁盘{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, nvme.get("percentage_used")), ignore_exists=False)

    #SSD寿命检查，不同厂家检测方式不同
    smartctl_attrs = smartctl_info.get("SMART Attributes List")
    if not smartctl_attrs:
        return
    
    #Foresee
    device_model = smartctl_info.get("Device Model")
    if device_model:
        if search_section(device_model, r"^FORESEE", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "167")
            if int(liftspan) >=2700:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^INTEL", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "233", "VALUE")
            if int(liftspan) <= 10:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^SAMSUNG", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "177", "VALUE")
            if int(liftspan) <= 10:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^Micron", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "202", "VALUE")
            if int(liftspan) <= 10:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^KHK", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "173", "VALUE")
            if int(liftspan) <= 100:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^TS", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "167")
            if int(liftspan) >= 2700:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^PTS", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "233")
            if int(liftspan) >= 90:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

        if search_section(device_model, r"^STS", re.DOTALL, group=0):
            liftspan = get_smartctl_attr(smartctl_attrs, "233")
            if int(liftspan) <= 10:
                update_value(ret, "返修建议", "磁盘{}-SN:{}的寿命达到阈值，当前值:{}，建议更换。".format(diskname, smartctl_info.get("Serial Number"), liftspan), ignore_exists=False)

def check_sas_smartctl(smartctl_info, is_vs_disk, ret):
    if not smartctl_info:
        return
    
    sas_attrs = smartctl_info.get("SAS SMART Attributes Values")
    if sas_attrs:
        if sas_attrs.get("SMART Health Status") == "Failed":
            update_value(ret, "返修建议", "SAS SMART Health Status 检查结果为:{}，建议返修。".format(sas_attrs.get("SMART Health Status")), ignore_exists=False)
        
        if sas_attrs.get("Elements in grown defect list"):
            alert = 100
            if is_vs_disk:
                alert = 200
            if int(sas_attrs.get("Elements in grown defect list")) >= alert:
                update_value(ret, "返修建议", "SAS Elements in grown defect list 检查结果为:{}，建议返修。".format(sas_attrs.get("Elements in grown defect list")), ignore_exists=False)

def check_sas_smartctl_info(smartctl_info):
    is_vs_disk = check_vs_use_disk(smartctl_info)
    ret = {}

    #检查sas smartctl
    check_sas_smartctl(smartctl_info, is_vs_disk, ret)

    #检测smartctl -H
    check_smartctl_H(smartctl_info, ret)

    return ret

def check_sata_smartctl_info(smartctl_info):
    is_vs_disk = check_vs_use_disk(smartctl_info)
    ret = {}

    #检查smartct 5 / 197
    check_smartctl_5_197(smartctl_info, "5", is_vs_disk, ret)
    check_smartctl_5_197(smartctl_info, "197", is_vs_disk, ret)

    #检查smartctl 5一个月内增长是否大于10
    check_smartctl_5_increase(smartctl_info, ret)

    #检查199项增长
    check_smartctl_199(smartctl_info, ret)

    #检查smartct项pre-fail告警
    check_smartctl_alert(smartctl_info, ret, type="Pre-fail")

    #检测smartctl -H
    check_smartctl_H(smartctl_info, ret)

    check_disk_lifespan(smartctl_info, ret)
    return ret

def check_nvme_smartctl(smartctl_info, is_vs_disk, ret):
    if not smartctl_info:
        return
    
    nvme_attrs = smartctl_info.get("NVME SMART Attributes Values")
    if nvme_attrs:
        if nvme_attrs.get("Critical Warning"):
            alert = 100
            if is_vs_disk:
                alert = 200
            if int(nvme_attrs.get("Critical Warning"), base=16) >= alert:
                update_value(ret, "返修建议", "NVME Critical Warning 检查结果为:{}，建议返修。".format(nvme_attrs.get("Critical Warning")), ignore_exists=False)
        if nvme_attrs.get("Media and Data Integrity Errors"):
            alert = 100
            if is_vs_disk:
                alert = 200
            if int(nvme_attrs.get("Media and Data Integrity Errors")) >= alert: 
                update_value(ret, "返修建议", "NVME Media and Data Integrity Errors 检查结果为:{}，建议返修。".format(nvme_attrs.get("Media and Data Integrity Errors")), ignore_exists=False)

def check_nvme_smartctl_info(smartctl_info):
    ret = {}
    is_vs_disk = check_vs_use_disk(smartctl_info)
    check_nvme_smartctl(smartctl_info, is_vs_disk, ret)

    check_disk_lifespan(smartctl_info, ret)
    return ret

#获取smartctl属性指定项的值
def get_smartctl_attr(smartctl_attr, index = 5, key='RAW_VALUE'):
    if smartctl_attr:
        for attr in smartctl_attr:
            if isinstance(index, int):
                if int(attr["ID"]) == index:
                    return attr[key]
            else:
                if str(attr["ID"]) == str(index):
                    return attr[key]
        
    return None

def parse_kernel_log(kernel_log, diskname, ret):
    if not kernel_log:
        return
    
    search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Sense Key : Hardware Error)(.*?)\n'
    diskindex = 6
    values = search_section(kernel_log, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        if diskname in disk_err:
            update_value(ret, "返修建议", "磁盘{}出现Hardware Error，建议返修。".format(diskname))

    search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Sense:4/\d+/\d+)(.*?)\n'
    diskindex = 6
    values = search_section(kernel_log, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        if diskname in disk_err:
            update_value(ret, "返修建议", "磁盘{}出现Hardware Error，建议返修。".format(diskname))

    search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Sense:3/\d+/\d+)(.*?)\n'
    diskindex = 6
    values = search_section(kernel_log, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        if diskname in disk_err:
            update_value(ret, "返修建议", "磁盘{}出现Media Error，建议联系VS技术支持确认后再评估是否需要返修。".format(diskname))

#检查内核日志
def check_disk_kernel(diskname, ret):
    date = "today"
    if g_args.date:
        date = int(g_args.date[-2:])
    
    filename = "/sf/log/{}/kernel.log".format(date)
    if not os.path.exists(filename):
        #看看有没有压缩包
        tar_filename = "/sf/log/{}.tar.gz".format(date)
        if os.path.exists(tar_filename):
            #解压内核日志
            extract_tar_gz(tar_filename, "/sf/log", "kernel.txt")

    if not os.path.exists(filename):
        #找不到内核日志
        return
    
    with open(filename, "r") as f:
        content = f.read()
        parse_kernel_log(content, diskname, ret)

def test_time_format(log_time, time_format):
    log_datetime = None
    #尝试转换检查是否为时间
    try:
        log_datetime = datetime.datetime.strptime(log_time, time_format)
    except Exception as e:
        try:
            log_datetime = datetime.datetime.strptime(log_time, "%y-%m-%d,%H:%M:%S")
            time_format = "%y-%m-%d,%H:%M:%S"
        except Exception as e:
            try:
                log_datetime = datetime.datetime.strptime(log_time, "%y/%m/%d,%H:%M:%S")
                time_format = "%y/%m/%d,%H:%M:%S"
            except Exception as e:
                try:
                    log_datetime = datetime.datetime.strptime(log_time, "%m/%d/%y %H:%M:%S")
                    time_format = "%m/%d/%y %H:%M:%S"
                except Exception as e:
                    try:
                        log_datetime = datetime.datetime.strptime(log_time, "%m-%d-%y %H:%M:%S")
                        time_format = "%m-%d-%y %H:%M:%S"
                    except Exception as e:
                        try:
                            log_datetime = datetime.datetime.strptime(log_time, "%y-%m-%d %H:%M:%S")
                            time_format = "%y-%m-%d %H:%M:%S"
                        except Exception as e:
                            try:
                                log_datetime = datetime.datetime.strptime(log_time, "%y/%m/%d %H:%M:%S")
                                time_format = "%y/%m/%d %H:%M:%S"
                            except Exception as e:
                                try:
                                    log_datetime = datetime.datetime.strptime(log_time, "%a %b %d %H:%M:%S %Y")
                                    time_format = "%a %b %d %H:%M:%S %Y"
                                except Exception as e:
                                    return None
    return time_format

#比较RAID日志时间和RAID控制器时间是否在一天内
def compareRAIDTimeLogTimeIndays(control_time, error_time, log_time, time_format = "%Y-%m-%d,%H:%M:%S", days=1):
    if not control_time or not error_time or not log_time:
        return False
    
    time_format = test_time_format(log_time, time_format)
    if not time_format:
        return False

    # 将完整时间字符串转换为 datetime 对象
    given_time = datetime.datetime.strptime(log_time, time_format)

    # 对比RAID控制时间
    control_time_format = "%m/%d/%Y %H:%M:%S"
    control_datetime = datetime.datetime.strptime(control_time, control_time_format)

    # 计算时间差
    time_delta = abs((control_datetime - given_time).total_seconds())

    # 故障时间
    error_datetime = None
    try:
        error_time_format = "%Y%m%d"
        error_datetime = datetime.datetime.strptime(error_time, error_time_format)
    except Exception as e:
        try:
            error_datetime = datetime.datetime.strptime(error_time, control_datetime)
        except Exception as e:
            return False

    error_time_delta = abs((error_datetime - given_time).total_seconds())

    return time_delta <= days * 24 *60 * 60 or error_time_delta <= days * 24 * 60 * 60 

def parse_raidlog(driver, raid_log, smartctl_info, raid_disk_info, ret):
    if not smartctl_info or not raid_log:
        return
    
    err_time = datetime.datetime.now().strftime("%Y%m%d")
    if g_args.date:
        err_time = g_args.date
    
    device = get_raid_location_by_sn(raid_disk_info[driver], smartctl_info.get("Serial Number"))
    if not device:
        if g_args.verbose:
            logger.error("无法在RAID中找到磁盘:{}".format(smartctl_info.get("Serial Number")))
        return

    baseinfo_list = get_raid_baseinfo_list()
    if not driver in baseinfo_list:
        if g_args.verbose:
            logger.error("无法找到RAID基本信息。")
        return
    
    #获取RAID时间
    raidtimes = search_section(baseinfo_list[driver], r'Controller Time = (.*?)\n', re.DOTALL, group=-1, multi=1)
    if not raidtimes:
        return
    
    if g_args.verbose:
        logger.info("RAID卡当前时间为:{}".format(raidtimes))

    raid_log_list = search_section(raid_log, r'Adapter #\d\n(.*?)CLI Version', re.DOTALL, group=-1, multi=1)
    if not raid_log_list:
        return
    
    for i in range(0, len(raid_log_list)):
        raidtime = raidtimes[i]
    
        #博通RAID故障说明
        #24-07-20,03:26:15 FATAL:Uncorrectable medium error logged for VD 05/5 at 1579ec5 (on PD 0f(e0x08/s6) at 1579ec5)
        value = search_section(raid_log_list[i][0], r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (FATAL:Uncorrectable medium error logged for VD \S+ at \S+) \(on PD \S+\((\S+)\) at \S+\)\n', re.DOTALL, group=-1, multi=1)
        if value:
            for err in value:
                if (compareRAIDTimeLogTimeIndays(raidtime, err_time, err[0])):
                    if device.find(err[2]) != -1:
                        update_value(ret, "返修建议", "硬盘{}存在不可修复的介质错误。".format(smartctl_info.get("Serial Number")))

        #24-08-03,04:19:22 FATAL:Bad block table on VD 05/5 is full; unable to log block 70f2116 (on PD 0f(e0x08/s6) at 70f2116)
        value = search_section(raid_log_list[i][0], r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (FATAL:Bad block table on VD \S+ is full; unable to log block \S+) \(on PD \S+\((\S+)\) at \S+\)\n', re.DOTALL, group=-1, multi=1)
        if value:
            for err in value:
                if (compareRAIDTimeLogTimeIndays(raidtime, err_time, err[0])):
                    if device.find(err[2]) != -1:
                        update_value(ret, "返修建议", "硬盘{}坏块表已满，更换报错硬盘。".format(smartctl_info.get("Serial Number")))

        value = search_section(raid_log_list[i][0], r'(\S+) FATAL:Unable to access device PD (\S+)\((\S+)\)\n', re.DOTALL, group=-1, multi=1)
        if value:
            for err in value:
                if (compareRAIDTimeLogTimeIndays(raidtime, err_time, err[0])):
                    if device.find(err[2]) != -1:
                        update_value(ret, "返修建议", "硬盘{}发生故障，不可访问。".format(smartctl_info.get("Serial Number")))

        value = search_section(raid_log_list[i][0], r'(\S+) (WARNING:Predictive failure: PD )(.*?)\n', re.DOTALL, group=-1, multi=1)
        if value:
            for err in value:
                if (compareRAIDTimeLogTimeIndays(raidtime, err_time, err[0])):
                    if device.find(err[2]) != -1:
                        update_value(ret, "返修建议", "硬盘{}发生预失效判断。".format(smartctl_info.get("Serial Number")))

        value = search_section(raid_log_list[i][0], r'(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) ([0-9a-fA-Z]+), (CDB:) ([0-9a-fA-Z\s]+), (Sense: 70 00 04) ([0-9a-fA-Z\s]+)\n', re.DOTALL, group=-1, multi=1)
        if value:
            for err in value:
                if (compareRAIDTimeLogTimeIndays(raidtime, err_time, err[0])):
                    if device.find(err[3]) != -1:
                        update_value(ret, "返修建议", "硬盘{}故障".format(smartctl_info.get("Serial Number")))

def check_raid_log(smartctl_info, raid_disk_info, ret, raid_log=None):
    if not smartctl_info:
        return
    
    if not raid_log:
        raid_log = get_raidlog_list()
        if not raid_log:
            return
    for driver, raidlog in raid_log.items():
        parse_raidlog(driver, raidlog, smartctl_info, raid_disk_info, ret)

def check_raid_status(smartctl_info, raid_disk_info, ret):
    if not smartctl_info or not raid_disk_info:
        return
    
    for drvier, disk_info in raid_disk_info.items():
        #根据sn获取raid中的信息
        device_state = get_raid_device_state_by_sn(disk_info, smartctl_info.get("Serial Number"))
        if not device_state:
            continue
        if device_state.get("Media Error Count") and device_state.get("Media Error Count") > 0:
            update_value(ret, "返修建议", "磁盘{}存在Media Error，当前值：{}，建议联系VS技术支持确认后再评估是否需要返修。".format(smartctl_info.get("Serial Number"), device_state["Media Error Count"]))

def get_raid_driver():
    cmd = "lspci | grep -iE 'sas|lsi'|awk -F ' ' '{print $1}'"
    ret = run_cmd(cmd)
    if not ret:
        return
    pcis = ret.split('\n')
    pcis = list(set(pcis))
    drivers = []
    for pci in pcis:
        cmd = "lspci -v -s {}".format(pci) + "|grep driver|awk -F ':' '{print $2}'|sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'"
        driver = run_cmd(cmd)
        if driver:
            drivers.append(driver.strip())

    drivers = list(set(drivers))
    return drivers

def get_raid_tools():
    server_info = get_dmidecode()
    drivers = get_raid_driver()
    if not drivers or len(drivers) == 0:
        return None

    if g_args.verbose:
        logger.info("raid driver list:{}".format(drivers))
    
    raid_tools = []
    for driver in drivers:
        info = {}
        info["driver"] = driver
        if driver == "megaraid_sas":
            raid_tool = "/sf/bin/raidtools/bin/storcli64"
            if server_info and server_info.get("Manufacturer"):
                if search_section(server_info.get("Manufacturer"), r'DELL', re.DOTALL|re.IGNORECASE, group=0):
                    raid_tool = "/sf/bin/raidtools/bin/perccli64"
            
            if not os.path.exists(raid_tool):
                raid_tool = "/sf/vs/bin/storcli64.dat"
                if not os.path.exists(raid_tool):
                    raid_tool = "/sf/vs/bin/storcli64"
                    if not os.path.exists(raid_tool):
                        raid_tool = "storcli64"
            info["disk_list_params"] = "/call/eall/sall show all"
            info["get_raid_log_params"] = "/call show alilog"
            info["raid_base_params"] = "/call show"
        elif driver == "mpt3sas":
            raid_tool = "/sf/bin/raidtools/bin/sas3ircu"
            if not os.path.exists(raid_tool):
                raid_tool = "sas3ircu"
            info["disk_list_params"] = "/call show"
        elif driver == "smartpqi":
            raid_tool = "/sf/bin/raidtools/bin/arcconf_pm822"
            if not os.path.exists(raid_tool):
                raid_tool = "arcconf"
            info["disk_list_params"] = "list"
        else:
            continue
        
        info["raid_tool"] = raid_tool
        raid_tools.append(info)

    return raid_tools

def get_raid_baseinfo_list():
    raid_tools = get_raid_tools()
    if len(raid_tools) == 0:
        return {}
    baseinfo_list = {}
    for raidtool in raid_tools:
        cmd = "{} {}".format(raidtool["raid_tool"], raidtool.get("raid_base_params"))
        if g_args.verbose:
            logger.info("running cmd: {}".format(cmd))

        ret = run_cmd(cmd)
        if ret:
            baseinfo_list[raidtool["driver"]] = ret

    return baseinfo_list

def get_raidlog_list():
    raid_tools = get_raid_tools()
    if not raid_tools or len(raid_tools) == 0:
        return None
    raidlog_list = {}
    for raidtool in raid_tools:
        cmd = "{} {}".format(raidtool["raid_tool"], raidtool.get("get_raid_log_params"))
        if g_args.verbose:
            logger.info("running cmd: {}".format(cmd))

        ret = run_cmd(cmd)
        if ret:
            raidlog_list[raidtool["driver"]] = ret

    return raidlog_list

def get_raid_disk_list():
    raid_tools = get_raid_tools()
    if not raid_tools or len(raid_tools) == 0:
        return None
    disk_list = {}
    for raidtool in raid_tools:
        cmd = "{} {}".format(raidtool["raid_tool"], raidtool["disk_list_params"])
        if g_args.verbose:
            logger.info("running cmd: {}".format(cmd))

        ret = run_cmd(cmd)
        if ret:
            disk_list[raidtool["driver"]] = ret

    return disk_list

#解析raid日志中的磁盘位置:(e0x45/s1)
def parse_raid_disk(diskstr):
    #去掉()并转十六进制为十进制
    if not diskstr:
        return None
    disk = diskstr.replace('(', "").replace(')', "")
    num, slot = disk.split('/')
    num = int(num[1:], 16)
    return (num, slot)

def get_raid_disk_slot(diskstr):
    part_disk = parse_raid_disk(diskstr)
    if part_disk:
        (num, slot) = part_disk
        return "/e{}/{}".format(num, slot)
    else:
        return None

def check_raid_state_single(raid_info, ret):
    if not raid_info:
        return None
    
    values = search_section(raid_info, r'Drive (/c\d/e\d+/s\d+) State :\n======================\n(.*?)\n\n\n', re.DOTALL, group=-1, multi=1)
    if values:
        for v in values:
            stateinfo = parse_state_info(v[1])
            if not stateinfo:
                continue

            if stateinfo["Media Error Count"] > 0 or stateinfo["Predictive Failure Count"] > 0:
                update_value(ret, "RAID中异常硬盘列表", v[0], ignore_exists=True, type="list")

    values = search_section(raid_info, r'Description = Show Drive Information Failed.\n\nDetailed Status :\n\={10,20}\n\n\-{20,}\n(.*?)\n\-{20,}\n(.*?)\n\-{20,}', re.DOTALL, group=-1, multi=1)
    if values:
        for v in values:
            drive_status = parse_controller_drive_status(v)
            if not drive_status:
                continue
            
            for drive in drive_status:
                if drive.get("Status") == "Failure":
                    update_value(ret, "RAID中异常硬盘列表", drive.get("Drive"), ignore_exists=True, type="list")
                    update_value(ret, "RAID硬盘状态异常", drive.get("Drive"))
            
#检查raid中的磁盘是否存在错误
def check_raid_state(raid_info, ret):
    if not raid_info:
        return False
    
    if isinstance(raid_info, list):
        for rv in raid_info:
            check_raid_state_single(rv, ret)
    else:
        check_raid_state_single(raid_info, ret)
    
    return True

def parse_controller_drive_status(value):
    if not value or len(value) < 2:
        return None
    title = value[0].split()
    lines = value[1].split('\n')
    drives = []
    for line in lines:
        item = {}
        v = line.split()
        for i in range(0, len(title)):
            item[title[i]] = v[i]
        drives.append(item)
    
    return drives
    
def parse_state_info(stateinfo):
    info = {}
    try:
        info["Shield Counter"] = int(search_section(stateinfo, r'Shield Counter =(.*?)\n'))
        info["Media Error Count"] = int(search_section(stateinfo, r'Media Error Count =(.*?)\n'))
        info["Other Error Count"] = int(search_section(stateinfo, r'Other Error Count =(.*?)\n'))
        info["Drive Temperature"] = search_section(stateinfo, r'Drive Temperature =(.*?)\n')
        info["Predictive Failure Count"] = int(search_section(stateinfo, r'Predictive Failure Count =(.*?)\n'))
        info["Smart alert flag"] = search_section(stateinfo, r'S.M.A.R.T alert flagged by drive =(.*?)\n')
    except Exception as e:
        traceback.print_stack()
    return info

def get_raid_device_state_imp(raid_info, device):
    value = search_section(raid_info, device+" State :\n={20,25}\n(.*?)\n\n\n", re.DOTALL, group=-1)
    if not value:
        return None
        
    return parse_state_info(value.group(1))

#从raid信息中获取device state
def get_raid_device_state(raid_info, device):
    if not raid_info or not device:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            stat = get_raid_device_state_imp(rv, device)
            if stat:
                return stat
        return None
    else:
        return get_raid_device_state_imp(raid_info, device)

#解析控制器编号
def parse_controller_index(device):
    if not device:
        return
    value = search_section(device, r"/c(\d+)/", re.DOTALL, group=1)
    if value:
        return value
    return None

def parse_slot_number(device):
    if not device:
        return
    value = search_section(device, r"/s(\d+)", re.DOTALL, group=1)
    if value:
        return value
    return None

#根据SN号获取raid信息中的磁盘位置
def get_raid_location_by_sn(raid_info, sn):
    if not raid_info or not sn:
        return None
    
    values = search_section(raid_info, r'Drive (\S+) State :(.*?)\n\nDrive (\S+) Device attributes :(.*?)\n(SN =\s+\S+)', re.DOTALL, group=-1, multi=1)
    if not values:
        return None
    for value in values:
        if value[4].find(sn) != -1:
            return value[0]

#根据SN号获取raid信息中的device state信息
def get_raid_device_state_by_sn_imp(raid_info, sn):
    values = search_section(raid_info, r'Drive (\S+) State :(.*?)\n\nDrive (\S+) Device attributes :(.*?)\n(SN =\s+\S+)', re.DOTALL, group=-1, multi=1)
    if not values:
        return None
    for value in values:
        if value[4].find(sn) != -1:
            return parse_state_info(value[1])

def get_raid_device_state_by_sn(raid_info, sn):
    if not raid_info or not sn:
        return None
    
    if isinstance(raid_info, list):
        for rv in raid_info:
            info = get_raid_device_state_by_sn_imp(rv, sn)
            if info:
                return info
        return None
    else:
        return get_raid_device_state_by_sn_imp(raid_info, sn)

def get_raid_sas_address(raid_info, device):
    if not raid_info:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            sas_address = search_section(rv, device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=2)
            if sas_address:
                return sas_address
        return None
    else:
        return search_section(raid_info, device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=2)

def parse_device_attributes(attrs):
    if not attrs:
        return None
    
    device_attrs = {}
    lines = attrs.split('\n')
    for line in lines:
        parts = line.split('=')
        if len(parts) > 1:
            device_attrs[parts[0].strip()] = parts[1].strip()

    return device_attrs

def get_raid_device_attributes(raid_info, device):
    if not raid_info:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            info = search_section(rv, device + ' Device attributes :\n(.*?)\n={20,40}\n(.*?)\n\n\n', re.DOTALL, group=1)
            if info:
                return info
        return None
    else:
        return search_section(raid_info, device + ' Device attributes :\n(.*?)\n={20,40}\n(.*?)\n\n\n', re.DOTALL, group=1)


#从raid信息中获取Port information
def get_raid_device_port_infomation(raid_info, device):
    value = search_section(raid_info, 'Drive ' + device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=-1)
    if not value:
        return None
    info = {}
    settings = {}
    settings_value = value.group(1)
    settings["Enclosure position"] = search_section(settings_value, r'Enclosure position =(.*?)\n')
    settings["Connected Port Number"] = search_section(settings_value, r'Connected Port Number =(.*?)\n')
    settings["Sequence Number"] = search_section(settings_value, r'Sequence Number =(.*?)\n')
    settings["Commissioned Spare"] = search_section(settings_value, r'Commissioned Spare =(.*?)\n')
    settings["Emergency Spare"] = search_section(settings_value, r'Emergency Spare =(.*?)\n')
    settings["Last Predictive Failure Event Sequence Number"] = search_section(settings_value, r'Last Predictive Failure Event Sequence Number =(.*?)\n')
    settings["Successful diagnostics completion on"] = search_section(settings_value, r'Successful diagnostics completion on =(.*?)\n')
    settings["FDE Type"] = search_section(settings_value, r'FDE Type =(.*?)\n')
    settings["SED Capable"] = search_section(settings_value, r'SED Capable =(.*?)\n')
    settings["SED Enabled"] = search_section(settings_value, r'SED Enabled =(.*?)\n')
    settings["Secured"] = search_section(settings_value, r'Secured =(.*?)\n')
    settings["Cryptographic Erase Capable"] = search_section(settings_value, r'Cryptographic Erase Capable =(.*?)\n')
    settings["Sanitize Support"] = search_section(settings_value, r'Sanitize Support =(.*?)\n')
    settings["Locked"] = search_section(settings_value, r'Locked =(.*?)\n')
    settings["Needs EKM Attention"] = search_section(settings_value, r'Needs EKM Attention =(.*?)\n')
    settings["PI Eligible"] = search_section(settings_value, r'PI Eligible =(.*?)\n')
    settings["Certified"] = search_section(settings_value, r'Certified =(.*?)\n')
    settings["Wide Port Capable"] = search_section(settings_value, r'Wide Port Capable =(.*?)\n')
    settings["Multipath"] = search_section(settings_value, r'Multipath =(.*?)\n')

    info["Settings"] = settings
    info["Port information"] = value.group(2)
    
    return info

def get_dmidecode(params = '-t 1'):
    cmd = "dmidecode {}".format(params)
    ret = run_cmd(cmd)
    if ret:
        lines = ret.split('\n')
        info = {}
        for line in lines:
            if line.find(':') == -1:
                continue
            data = line.split(":")
            info[data[0].strip()] = data[1].strip()
        
        return info
    
    return None
    
def check_short_test_result(smartctl_info, ret):
    if not smartctl_info:
        return
    #检查结果
    if not smartctl_info.get("SMART Self-test"):
        return
    
    short_test = smartctl_info.get("SMART Self-test")
    test_record = short_test.get("Self-test Record")
    if not test_record or len(test_record) < 2:
        return
    
    last_test_record = test_record[1]
    if last_test_record.find("00%") != -1:
        #已完成
        if last_test_record.find("Completed without error") != -1 or last_test_record.find("Self-test routine in progress") != -1:
            pass
        else:
            update_value(ret, "返修建议", "SMARTCTL short-test结果异常：{}，建议返修".format(last_test_record), ignore_exists=False)

def check_short_test(diskname, smartctl_info, ret):
    if not g_args.shorttest:
        #不指定重新测试，则根据现有结果判断。
        check_short_test_result(smartctl_info, ret)
        return
    
    if not os.path.exists("/dev/{}".format(diskname)):
        #磁盘不在系统中，无法测试
        if g_args.verbose:
            logger.info("磁盘{}不在系统中，无法测试".format(diskname))
        return
    
    smartctl_util = "smartctl.real"
    #如果smartctl.real存在，就用smartctl.real
    if not check_cmd_exists(smartctl_util):
        smartctl_util = "smartctl"
        if not check_cmd_exists(smartctl_util):
            logger.error("smartctl not exists.")
            #检查是不是需要在容器里执行
            if check_use_container_version(get_hci_version()):
                logger.error("smartctl not exists, mabe in container.")
                #TODO 是否需要通过容器执行，目前python执行是进VT容器的话就不用了
                return None
            #命令不存在直接返回
            return None
        
    params = "--test=short"
    
    cmd = "{0} {1} /dev/{2}".format(smartctl_util, params, diskname)
    
    #先执行一次short test
    rt = run_cmd(cmd)
    if not rt:
        return 

    logger.info("执行smartctl测试中，请等待2分钟。")
    #等待2分钟
    time.sleep(120)

    #重新执行smartctl -a命令获取最新结果
    new_smartctl_info = get_disk_smartctl_info(diskname)
    if not new_smartctl_info:
        return

    #检查SN是否一致
    if smartctl_info.get("Serial Number") != new_smartctl_info.get("Serial Number"):
        logger.info("{}磁盘盘符已经发生变化，请确认后重新检查。".format(diskname))
        return
    
    check_short_test_result(new_smartctl_info, ret)
    
def check_disk_health_by_name(diskname, raid_disk_info, raid_log=None):
    if not diskname:
        return
    
    #if not isinstance(diskname, str):
    #    logger.error("diskname must be string.")
    #    return
    
    smartctl_info = None
    if not os.path.exists("/dev/{}".format(diskname)):
        if g_args.verbose:
            logger.error("diskname {0} not exists.".format(diskname))
        #可能已经被踢盘，尝试从VS配置获取SN
        sn = get_sn_by_diskname_from_vsconfig(diskname)
        if not sn:
            #sn也找不到，说明已经不存在（拔盘）
            if g_args.verbose:
                logger.error("磁盘{}已经无法找到。".format(diskname))
            return

        smartctl_info = get_his_smartctl_by_sn(sn)
        if not smartctl_info:
            if g_args.verbose:
                logger.error("当前无法找到磁盘:{}".format(sn))
            return
    else:
        smartctl_info = get_disk_smartctl_info(diskname)

    if not smartctl_info:
        logger.error("get disk {0} smartctl info error.".format(diskname))
        return
    
    if g_args.verbose:
        logger.info("disk {0} smartctl info is:\n {1}".format(diskname, json.dumps(smartctl_info, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))
    
    protype = get_disk_protocol(smartctl_info)
    if g_args.verbose:
        logger.info("disk {0} protocol is {1}.".format(diskname, protype))

    #根据接口类型检查磁盘是否存在返修标准的问题
    ret = {}
    if "SAS" == protype:
        ret = check_sas_smartctl_info(smartctl_info)
        check_short_test(diskname, smartctl_info, ret)
    elif "SATA" == protype:
        ret = check_sata_smartctl_info(smartctl_info)
        check_short_test(diskname, smartctl_info, ret)
    elif "NVME" == protype:
        ret = check_nvme_smartctl_info(smartctl_info)
    else:
        logger.error("unknow disk type.")

    #检查内核日志
    check_disk_kernel(diskname, ret)

    #检查RAID卡中的磁盘状态
    check_raid_status(smartctl_info, raid_disk_info, ret)

    #检查RAID卡日志
    check_raid_log(smartctl_info, raid_disk_info, ret, raid_log=raid_log)

    if not ret.get("返修建议"):
        ret["检查结果"] = "检查磁盘[{}-SN:{}]正常，不符合返修标准，可以继续使用。".format(diskname, smartctl_info.get("Serial Number"))
    else:
        ret["检查结果"] = "检查磁盘[{}-SN:{}]异常，异常事项：{}，符合返修标准，建议返修。".format(diskname, ret.get("返修建议"), smartctl_info.get("Serial Number"))

    #补充磁盘信息
    location = get_vs_disk_location(smartctl_info)
    ret["磁盘信息"] = {}
    ret["磁盘信息"]["是否VS使用"] = "是" if check_vs_use_disk(smartctl_info) else "否"
    ret["磁盘信息"]["盘位编号"] = "{} 号盘".format(location) if location else "未知"
    ret["磁盘信息"]["磁盘型号"] = smartctl_info.get("Device Model")
    ret["磁盘信息"]["固件版本"] = smartctl_info.get("Firmware Version")
    
    if raid_disk_info:

        for driver, raid_info in raid_disk_info.items():
            if not raid_info:
                continue

            raid_disklocation = get_raid_location_by_sn(raid_info, smartctl_info.get("Serial Number"))
            if raid_disklocation:
                if g_args.verbose:
                    logger.info("位置信息：{}".format(raid_disklocation))
                ret["磁盘在RAID中的信息"] = {}
                ret["磁盘在RAID中的信息"]["控制器编号"] = parse_controller_index(raid_disklocation)
                ret["磁盘在RAID中的信息"]["槽位"] = parse_slot_number(raid_disklocation)

    return ret

def check_disks_by_name(disknames, raid_disk_info = None, raid_log = None):
    if not disknames:
        return
    
    if g_args.verbose:
        logger.info("check disks {0} health.".format(disknames))

    if not isinstance(disknames, list):
        disknames = [disknames]
    
    if not raid_disk_info:
        raid_disk_info = get_raid_disk_list()
        if g_args.verbose:
            logger.info("raid disk list:\n{}".format(json.dumps(raid_disk_info, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))

    results = []
    for disk in disknames:
        ret = check_disk_health_by_name(disk, raid_disk_info, raid_log)
        if g_args.verbose:
            logger.info("磁盘 {0} 健康检查结果:\n {1}.".format(disk, json.dumps(ret, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))
        results.append(ret)

    json_ret = json.dumps(ensure_unicode(results), sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')
    print(json_ret.encode('utf-8'))

def get_his_smartctl(filename):
    smartctl_info = {}
    if not os.path.exists(filename):
        return None
    with open(filename, "r") as f:
        data = f.read()
        data += "\n\n######"
        values = search_section(data, r"/dev/([sd|nvme].+?)######\n(smartctl.*?)\n\n(.*?)(?:\n\n)?######", re.DOTALL, group=-1, multi=1)
        if values:
            for value in values:
                smartctl_info[value[0]] = parse_smartctl_info(value[2]+"\n\n")
    return smartctl_info

def get_his_smartctl_by_sn(sn):
    if not sn:
        return None
    
    #先从当天的日志中获取
    if g_args.date:
        filename = "/sf/log/blackbox/{0}/LOG_vs_smartctl.txt".format(g_args.date)
    else:
        filename = "/sf/log/blackbox/today/LOG_vs_smartctl.txt"

    his_smartctl_info = get_his_smartctl(filename)
    if not his_smartctl_info:
        return None
    for diskname, smartctl_info in his_smartctl_info.items():
        if smartctl_info.get("Serial Number") == sn:
            #获取出来调用smartctl确认如果一致才认为找到
            smartctl_info = get_disk_smartctl_info(diskname, params="-i")
            if smartctl_info and smartctl_info.get("Serial Number") == sn:
                if g_args.verbose:
                    logger.info("find disk {0} by sn {1}.".format(diskname, sn))

                return smartctl_info

def get_diskname_by_sn(sn):
    if not sn:
        return None
    
    if not isinstance(sn, str):
        logger.error("sn must be string.")
        return None

    #尝试从VS配置查找
    disk = get_diskname_by_sn_from_vsconfig(sn)
    if disk:
        return disk
    
    #先从当天的日志中获取
    if g_args.date:
        filename = "/sf/log/blackbox/{0}/LOG_vs_smartctl.txt".format(g_args.date)
    else:
        filename = "/sf/log/blackbox/today/LOG_vs_smartctl.txt"

    his_smartctl_info = get_his_smartctl(filename)
    if his_smartctl_info:
        for diskname, smartctl_info in his_smartctl_info.items():
            if smartctl_info.get("Serial Number") == sn:
                #获取出来调用smartctl确认如果一致才认为找到
                smartctl_info = get_disk_smartctl_info(diskname, params="-i")
                if smartctl_info and smartctl_info.get("Serial Number") == sn:
                    if g_args.verbose:
                        logger.info("find disk {0} by sn {1}.".format(diskname, sn))
                    return diskname
                
    #通过smartctl遍历查询
    disk_list = get_disk_list()
    if disk_list:
        for disk in disk_list:
            smartctl_info = get_disk_smartctl_info(disk, params="-i")
            if smartctl_info and smartctl_info.get("Serial Number") == sn:
                if g_args.verbose:
                    logger.info("find disk {0} by sn {1}.".format(disk, sn))
                
                return disk
    
    return None
            
def check_disks_by_sn(sns):
    if not sns:
        return
    
    if g_args.verbose:
        logger.info("check disks {0} health.".format(sns))

    if not isinstance(sns, list):
        sns = [sns]

    raid_disk_info = get_raid_disk_list()
    if g_args.verbose:
        logger.info("raid disk list:\n{}".format(json.dumps(raid_disk_info, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))

    raid_log = get_raidlog_list()
    results = []
    for sn in sns:
        ret = None
        disk = get_diskname_by_sn(sn)
        if disk:
            ret = check_disk_health_by_name(disk, raid_disk_info, raid_log = raid_log)
        else:
            ret = {}
            #可能组了RAID，需要单独从RAID中查询
            if g_args.verbose:
                logger.info("系统中无法找到该磁盘{}，尝试从RAID卡获取信息。".format(sn))

            if not raid_disk_info:
                continue

            for driver, raid_info in raid_disk_info.items():
                #检查RAID中是否有异常磁盘
                check_raid_state(raid_info, ret)

                if ret.get("RAID中异常硬盘列表"):
                    for device in ret.get("RAID中异常硬盘列表"):
                        attrs = get_raid_device_attributes(raid_info, device)
                        device_attrs = parse_device_attributes(attrs)
                        if device_attrs and device_attrs.get("SN") == sn:
                            update_value(ret, "返修建议", "磁盘{}存在异常，符合返修标准，建议返修。".format(sn))

        if g_args.verbose:
            logger.info("磁盘 {0} 健康检查结果:\n {1}.".format(disk, json.dumps(ret, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))
        
        results.append(ret)
    
    print(json.dumps(results, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8'))

# 获取系统盘
def get_sys_disk():
    cmd = "mount -l|grep /boot|awk -F ' ' '{print $1}'|grep dev"
    try:
        output = subprocess.check_output(cmd, shell=True).decode('utf-8')
        if output:
            if output.find("nvme") != -1:
                return re.sub(r'p\d+$', '', output.strip())
            else:
                return re.sub(r'\d+$', '', output.strip())
    except Exception as e:
        logger.error("get sys disk failed:{}".format(e))

    return None

# 获取系统中所有磁盘的列表
def get_disk_list():
    real_disk_list = []
    
    #系统盘
    sys_disk = get_sys_disk()
    if g_args.verbose:
        logger.info("系统盘:{}".format(sys_disk))

    lsblk_cmd = "lsblk -o NAME -n -p -d|grep -vE 'loop|^dm|sr|nbd'"
    lsblk_output = subprocess.check_output(lsblk_cmd, shell=True).decode('utf-8')
    disk_list = lsblk_output.strip().split('\n')
    if g_args.verbose:
        logger.info("current disk list:{}".format(disk_list))

    multipath_cmd = "multipath -ll|grep sd|awk -F ' ' '{print $3}'"
    multipath_output = subprocess.check_output(multipath_cmd, shell=True).decode('utf-8')
    multipath_disk_list = multipath_output.strip().split('\n')
    if g_args.verbose:
        logger.info("current multipath disk list:{}".format(multipath_disk_list))
    
    #去掉iscsi/fc存储
    extent_cmd = "lsblk -S -P|grep -E 'iscsi|fc'|awk -F ' ' '{print $1}'|awk -F '=' '{print $2}'|tr -d '\"'"
    extent_output = subprocess.check_output(extent_cmd, shell=True).decode('utf-8')
    extent_disk_list = extent_output.strip().split('\n')
    if g_args.verbose:
        logger.info("current iscsi/fc disk list:{}".format(extent_disk_list))

    #去掉外置存储
    for disk in disk_list:
        diskname = disk.split('/')[-1]

        if diskname in multipath_disk_list or diskname in extent_disk_list:
            continue
        else:
            real_disk_list.append(diskname)

    if g_args.verbose:
        logger.info("current disk list:{}".format(real_disk_list))

    return real_disk_list

if __name__ == "__main__":
    
    try:
        logger.add(
            logger_filename,
            rotation="1 week",
            compression="zip",
            level="INFO",
            encoding="utf-8"
        )
    except Exception as e:
        #print(e)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    

    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", help="是否输出详细日志信息", action='store_true')
    parser.add_argument('--version', action='version', version='%(prog)s by hlc v2025.07.08')

    parser.add_argument("-d", "--disks", help="需要检查的磁盘路径名称，多个以逗号分隔。")
    parser.add_argument("-s", "--sn", help="需要检查的磁盘SN号，多个以逗号分隔。")
    parser.add_argument("-D", "--date", help="需要检查的故障日期(不指定默认为当天)，格式为：YYYYMMDD。")
    parser.add_argument("-t", "--shorttest", help="是否执行short test检查", action='store_true')
    parser.add_argument("-a", "--all", help="执行检查所有磁盘", action='store_true')

    args = parser.parse_args()
    g_args = args

    if g_args.verbose:
        logger.info("diskchecker start. {}".format(args))

    if g_args.date:
        if len(g_args.date) != 8 or not g_args.date.isdigit():
            logger.error("指定的日期格式不正确，请输入格式为：YYYYMMDD。")
            sys.exit()

    g_vs_disks = load_vs_disks()
    if args.verbose:
        logger.info("vs disks is:\n {0}".format(json.dumps(g_vs_disks, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False, encoding='utf-8')))

    if not args.disks and not args.sn and not args.all:
        logger.error("请指定需要检查的磁盘路径名称或SN号。")
        sys.exit()

    if args.all:
        disks = get_disk_list()
        raid_log = get_raidlog_list()
        check_disks_by_name(disks, raid_log=raid_log)
        sys.exit()

    if args.disks:
        disks = args.disks.split(",")
        raid_log = get_raidlog_list()
        check_disks_by_name(disks, raid_log=raid_log)
        sys.exit()

    if args.sn:
        sn = args.sn.split(",")
        check_disks_by_sn(sn)
        sys.exit()

    

    

    
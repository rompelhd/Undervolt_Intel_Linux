#!/usr/bin/env python3
import os, struct, glob, argparse, logging
from struct import pack, unpack
from collections import namedtuple
import subprocess
import multiprocessing
import threading
import re
import statistics
import time
from time import sleep

RESET = "\033[0m"
BOLD = "\033[1m"
UNDERLINE = "\033[4m"

BLACK = "\033[30m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"

BG_BLACK = "\033[40m"
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
BG_YELLOW = "\033[43m"
BG_BLUE = "\033[44m"
BG_MAGENTA = "\033[45m"
BG_CYAN = "\033[46m"
BG_WHITE = "\033[47m"

try:
    subprocess.run(["modprobe", "msr"], check=True)
    print("MSR loaded")
except subprocess.CalledProcessError:
    print("Error Loading MSR")

MSR = namedtuple('MSR', ['addr_voltage_offsets', 'addr_units', 'addr_power_limits', 'addr_temp'])
msr = MSR(0x150, 0x606, 0x610, 0x1a2)

def sensors():
    try:
        output = subprocess.check_output(["sensors"]).decode()
        lines = output.splitlines()
        for line in lines:
            if "Core" in line or "Package" in line:
                print(f"🌡️ {line.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"{RED}⚠️ Error executing 'sensors': {e}{RESET}")
    except Exception as e:
        print(f"{RED}⚠️ Could not retrieve temperature: {e}{RESET}")

def read_turbo_status(msr):
    turbo_address = 0x1a0
    raw_value = read_msr(turbo_address)
    turbo_disabled = (raw_value & (1 << 38)) != 0
    return "disable" if turbo_disabled else "enable"

def valid_cpus():
    return [i for i in range(os.cpu_count()) if os.path.isdir(f"/dev/cpu/{i}")]

def read_msr(addr, cpu=0):
    path = f'/dev/cpu/{cpu}/msr'
    if not os.path.exists(path):
        raise OSError("MSR module is not loaded (run 'modprobe msr')")
    f = os.open(path, os.O_RDONLY)
    os.lseek(f, addr, os.SEEK_SET)
    val, = unpack('Q', os.read(f, 8))
    os.close(f)
    return val

def pack_offset(plane_index, offset=None):
    return ((1 << 63) | (plane_index << 40) | (1 << 36) |
            ((offset is not None) << 32) | (offset or 0))

def read_offset(plane, msr):
    plane_index = {'core': 0, 'gpu': 1, 'cache': 2, 'uncore': 3, 'analogio': 4}[plane]
    value_to_write = pack_offset(plane_index)
    for cpu in valid_cpus():
        path = f'/dev/cpu/{cpu}/msr'
        f = os.open(path, os.O_WRONLY)
        os.lseek(f, msr.addr_voltage_offsets, os.SEEK_SET)
        os.write(f, pack('Q', value_to_write))
        os.close(f)
        val = read_msr(msr.addr_voltage_offsets, cpu)
        return val

def unconvert_offset(y):
    rounded_offset = (y >> 21) & 0xFFF
    if rounded_offset >= 1024:
        rounded_offset -= 2048
    return rounded_offset / 1.024

def writemsr(msr_addr, value):
    for path in glob.glob('/dev/cpu/[0-9]*/msr'):
        try:
            fd = os.open(path, os.O_WRONLY)
            os.lseek(fd, msr_addr, os.SEEK_SET)
            os.write(fd, struct.pack('Q', value))
            os.close(fd)
        except Exception as e:
            print(f"⚠️  Error writing to {path}: {e}")

def apply_undervolt(target, mv):
    if mv > 0:
        raise ValueError(f"Value for {target} must be 0 or negative (millivolts).")
    ids = {'cpu': [0, 2], 'gpu': [1]}[target]
    for i in ids:
        offset = round(mv * 1.024)
        val = (1 << 63) | (i << 40) | (0x11 << 32) | ((offset & 0xFFF) << 21)
        writemsr(0x150, val)
        print(f"✔️ Undervolt applied to {target.upper()} ({mv} mV)")

def temp_cpu():
    output = subprocess.check_output(["sensors"]).decode()
    temps = []
    for line in output.splitlines():
        match = re.search(r'Core\s+\d+:\s+\+([\d\.]+)', line)
        if match:
            temps.append(float(match.group(1)))
    return temps

def get_energy():
    path = "/sys/class/powercap/intel-rapl:0/energy_uj"
    try:
        with open(path, "r") as f:
            return int(f.read())
    except:
        return None

def burn_cpu():
    def worker():
        while True:
            x = 999999
            x *= x  # Operation keep CPU busy

    processes = []
    for _ in range(multiprocessing.cpu_count()):
        p = multiprocessing.Process(target=worker)
        p.daemon = True
        p.start()
        processes.append(p)

    return processes

def track_temperatures(segundos=60):
    temperatures = []
    initial_energy = get_energy()

    threads = burn_cpu()
    print(f"🔥 Running CPU load for {segundos} seconds...\n")

    start = time.time()
    for second in range(segundos):
        temps = temp_cpu()
        if temps:
            temperatures.append(sum(temps) / len(temps))

        completed = second + 1
        total = segundos
        bar_width = 40
        progress = int(bar_width * completed / total)
        bar = "█" * progress + "-" * (bar_width - progress)
        print(f"\r⏳ [{bar}] {completed}/{total} s", end='', flush=True)

        time.sleep(1)

    print("\n⏹️  Load finished.")

    final_energy = get_energy()
    energy_used_wh = None
    if initial_energy and final_energy:
        energy_used_j = final_energy - initial_energy
        energy_used_wh = round((energy_used_j / 1_000_000) / 3600, 4)

    result = {
        'min': round(min(temperatures), 1),
        'max': round(max(temperatures), 1),
        'avg': round(statistics.mean(temperatures), 1)
    }

    if energy_used_wh:
        result['consumption_wh'] = energy_used_wh

    return result

def quitar_undervolt():
    print("↩️ Removing undervolt...")
    try:
        apply_undervolt("cpu", 0)
        apply_undervolt("gpu", 0)
    except Exception as e:
        print(f"⚠️ Error removing undervolt: {e}")

def color_temp(temp):
    if temp >= 85:
        return f"\033[91m{temp}°C\033[0m"
    elif temp >= 65:
        return f"\033[93m{temp}°C\033[0m"
    else:
        return f"\033[92m{temp}°C\033[0m"

def test_with_undervolt(msr):
    print("🔧 Running stress test with undervolt:")
    res = track_temperatures()
    print(f"\n🌡️  Results with undervolt:")
    print(f"🔻 Min: {color_temp(res['min'])}")
    print(f"🔺 Max: {color_temp(res['max'])}")
    print(f"📊 Average: {color_temp(res['avg'])}")
    if 'consumption_wh' in res:
        print(f"⚡ Estimated CPU energy consumption: {res['consumption_wh']} Wh")
    return res

def test_without_undervolt(msr):
    print("\n🔧 Running stress test without undervolt:")
    apply_undervolt("cpu", 0)
    apply_undervolt("gpu", 0)
    res = track_temperatures()
    print(f"\n🌡️  Results without undervolt:")
    print(f"🔻 Min: {color_temp(res['min'])}")
    print(f"🔺 Max: {color_temp(res['max'])}")
    print(f"📊 Average: {color_temp(res['avg'])}")
    if 'consumption_wh' in res:
        print(f"⚡ Estimated CPU energy consumption: {res['consumption_wh']} Wh")
    return res

def run_full_test(msr):
    core_uv = unconvert_offset(read_offset('core', msr))
    gpu_uv = unconvert_offset(read_offset('gpu', msr))

    res_uv = test_with_undervolt(msr)

    sleep(30)
    print("⌚ Waiting 30s to lower the temperature")

    res_no_uv = test_without_undervolt(msr)

    print("\n♻️ Restoring original undervolt settings...")
    apply_undervolt("cpu", core_uv)
    apply_undervolt("gpu", gpu_uv)
    print("✅ Undervolt restored.")

    if 'consumption_wh' in res_uv and 'consumption_wh' in res_no_uv:
        savings = res_no_uv['consumption_wh'] - res_uv['consumption_wh']
        savings = round(savings, 4)
        print(f"\n💡 Energy savings during test: {savings} Wh")

def show_current_settings(msr):
    print("\n📥 Current voltage information:\n")

    core_offset = unconvert_offset(read_offset('core', msr))
    gpu_offset = unconvert_offset(read_offset('gpu', msr))
    cache_offset = unconvert_offset(read_offset('cache', msr))
    uncore_offset = unconvert_offset(read_offset('uncore', msr))
    analogio_offset = unconvert_offset(read_offset('analogio', msr))

    print(f"Core Voltage Offset: {core_offset} mV")
    print(f"GPU Voltage Offset: {gpu_offset} mV")
    print(f"Cache Voltage Offset: {cache_offset} mV")
    print(f"Uncore Voltage Offset: {uncore_offset} mV")
    print(f"AnalogIO Voltage Offset: {analogio_offset} mV")

    turbo_status = read_turbo_status(msr)
    print(f"Turbo Status: {turbo_status}\n")

    sensors()

parser = argparse.ArgumentParser(description="Intel undervolt utility and stress tester")
parser.add_argument('-cpu', type=int, help="Set CPU undervolt in mV")
parser.add_argument('-gpu', type=int, help="Set GPU undervolt in mV")
parser.add_argument('--testunder', action='store_true', help="Run a stress test with undervolt only")
parser.add_argument('--testnormal', action='store_true', help="Run a stress test without undervolt")
parser.add_argument('--alltest', action='store_true', help="Run stress test with and without undervolt and show consumption difference")

args = parser.parse_args()

if not any(vars(args).values()):
    show_current_settings(msr)
    exit()
if args.cpu is not None:
    apply_undervolt("cpu", args.cpu)
if args.gpu is not None:
    apply_undervolt("gpu", args.gpu)

if args.testunder:
    test_with_undervolt(msr)
elif args.testnormal:
    test_without_undervolt(msr)
elif args.alltest:
    run_full_test(msr)

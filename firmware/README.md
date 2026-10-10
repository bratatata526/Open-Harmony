# 固件源码（Hi3861）

这里存放**我们编写的固件源码**。它们不能独立编译，需要放进 HiSpark SDK 的指定位置。

> **为什么 SDK 不在仓库里**：`hi3861_application/` 含完整 HiSpark SDK（约 514 MB、2.6 万个文件），
> `Tool/` 含 DevEco 工具链（约 3 GB）。这些是**厂商提供的、不是我们写的**，
> 放进 Git 会让仓库无法克隆。请按下面的说明获取 SDK 后再集成我们的代码。

---

## 一、目录内容

| 文件 | 说明 |
|---|---|
| `agri_logger/log_def.h` | **日志规范实现** —— 全项目唯一的日志出口。字段宏、故障码表、自写十进制转换 |
| `agri_logger/agri_logger_multi.c` | **主固件**。单设备模式 + 六棚逻辑设备模式（`SINGLE_DEVICE_MODE` 开关）、故障注入器、OLED 显示、健康检查 |
| `agri_logger/BUILD.gn` | 编译配置。**注意 sources 里必须列出用到的所有 BSP 驱动**，否则链接期报 `undefined reference` |
| `agri_logger/agri_logger_demo.c.reference` | 阶段 1 的单设备参考实现，不参与编译，仅作对照 |
| `sdk-integration/app_BUILD.gn` | 需要替换 SDK 里的 `app/BUILD.gn`（它决定编译哪个 app） |
| `sdk-integration/deveco.ini` | 需要替换 SDK 里的 `.deveco/deveco.ini`（DevEco 的编译/烧录配置） |

---

## 二、怎么集成（新同学按这个做）

### 1. 获取 SDK

`hi3861_application/` 是课程随板提供的 HiSpark SDK 工作区（含 `vendor/hqyj/fs_hi3861` 板级适配）。
**从课程资料或队友那里拷贝一份**，放在仓库根目录（与 `firmware/` 平级）。

### 2. 复制我们的固件源码

```powershell
# 在仓库根目录执行
$dst = "hi3861_application\src\applications\sample\wifi-iot\app\agri_logger"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item firmware\hi3861\agri_logger\*.c, firmware\hi3861\agri_logger\*.h, firmware\hi3861\agri_logger\BUILD.gn $dst -Force
```

### 3. 替换两个配置文件

```powershell
Copy-Item firmware\sdk-integration\app_BUILD.gn "hi3861_application\src\applications\sample\wifi-iot\app\BUILD.gn" -Force
Copy-Item firmware\sdk-integration\deveco.ini   "hi3861_application\.deveco\deveco.ini" -Force
```

> `deveco.ini` 里的 `upload_port` / `monitor_port` 要改成**你板子实际枚举出来的 COM 号**。
> Windows 按物理 USB 插口分配 COM 号，换个口就会变（本项目实测出现过 COM3→COM4→COM5→COM3）。

### 4. 编译与烧录

VSCode 工作区打开 **`hi3861_application` 目录**（不是仓库根目录），然后：

1. DevEco Device Tool → **PROJECT TASKS** → **`Rebuild`**（约 97 秒）
2. **`Upload`**
3. **★ 物理拔插一次 USB ★** —— HiBurn 会把芯片留在 bootloader，
   不拔插的话板子连 ROM 日志都不打印，现象与"板子坏了"完全一样

### 5. 校验烧录是否生效

```powershell
.venv\Scripts\python.exe tools/check_firmware.py --port auto --duration 20
```

采集期间**按一下板子复位键**，会抓到开机横幅和数值自检行：

```
[agri_logger] FW=v2.4 mode=single-device(真实传感器直通), period=5000ms, tickfreq=100Hz, heap=...
I A003 AgriSelfCheck: 1 0 5 1001 8 20480 15 -355 z=-
                        ↑零 ↑4位   ↑5位     ↑负数
```

**看到这两行，才说明烧录真的生效了。** 详细判据见 `docs/09` 坑 11。

---

## 三、运行模式开关

`agri_logger_multi.c` 顶部：

```c
#define SINGLE_DEVICE_MODE  1
```

| 值 | 模式 | 说明 |
|---|---|---|
| **1**（当前） | 单设备 | 只上报板载 SHT20（温湿度）+ AP3216C（光照）的**原始读数**，不加任何人工成分 |
| 0 | 六棚逻辑设备 | 一个真实基准 → 6 个逻辑大棚（固定物理偏置 + 热惯性）。物理依据见 `docs/03` §1.4.2 |

**切换模式后记得把开机横幅的 `FW=` 号 +1**，否则无法判断板上跑的是哪一版。

---

## 四、改完代码务必同步存档

固件源码在仓库里有两份：**SDK 部署目录**（会被编译）和 **`firmware/` 存档**。
手工 `copy` 很容易把方向搞反、把改动覆盖掉（本项目出过这个事故），所以用脚本：

```powershell
python tools/sync_firmware.py          # SDK → firmware，并核对哈希
python tools/sync_firmware.py --check  # 只核对
```

---

## 五、踩过的坑（都是真机上验证过的）

| 坑 | 一句话 |
|---|---|
| 平台 `printf` 多位数截断 | `1001` 打成 `1`，`-1270` 打成 `-`。**日志路径完全不用 stdio 格式化** |
| 时基错位 | 把 10ms/tick 当 1ms，时间戳慢 10 倍，但定时完全正常。用 `osKernelGetTickFreq()` |
| 链接期 `undefined reference` | 头文件只管声明，**实现必须在 BUILD.gn 的 sources 里点名** |
| 改了源码没重烧 | 现象和"平台 bug"一模一样。加开机版本号 + 数值自检行 |
| 烧录后无输出 | 必须**物理拔插 USB**，按复位键不够 |

完整记录（14 个坑，含定位方法）见 `docs/09-troubleshooting-log.md`。

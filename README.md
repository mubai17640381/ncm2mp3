# ncm2mp3

把网易云音乐的 `.ncm` 加密文件还原成标准 MP3 / FLAC，用来拷进车载 U 盘、MP3 播放器、剪辑软件。

**解密，不是转码** —— 原文件里存的就是标准 MP3/FLAC 数据，本项目只是把外面那层壳去掉，音质零损失、不做二次编码。

---

## 为什么要重写一个

网上能搜到的 ncm 转换教程和脚本，大多停留在 2021 年前后的格式。网易云在 2024 年之后改了两处，旧脚本跑出来的文件**全是打不开的垃圾**，而且不报错，直到你插上车载 U 盘才发现：

| | 旧版（网上教程里的） | 新版（2024 至今） |
|---|---|---|
| 音频密钥长度 | 16 字符 | 99 字符 |
| 密钥流相位 | 第 1 字节对应 j=0 | 第 1 字节对应 j=1（差一位就全错） |
| 元数据段 | XOR 0x63 → AES-ECB → base64 | XOR 0x63 → base64（不再 AES） |

本项目对新旧两种格式都做了兼容，实测 2026 年 10 月下载的 ncm 可正常还原。详见 [docs/FORMAT.md](docs/FORMAT.md)。

## 特性

- 支持 2024 年以后的新版 NCM 格式
- 自动识别真实音频容器（mp3 / flac / m4a / wav / ogg），按实际格式输出扩展名
- 自动把专辑封面写进 ID3 `APIC` 帧，车载播放器可直接显示封面
- 自动携带同名 `.lrc` 歌词
- 批量处理、目录递归，同名文件自动加 `(1)` 后缀
- 纯 Python，**不需要 ffmpeg**
- 单文件实现，400 行以内，方便审计和二次开发

## 安装

```bash
pip install -r requirements.txt
# 或者
pip install pycryptodome mutagen
```

只有 `pycryptodome` 是硬性依赖；`mutagen` 仅用于内嵌封面，缺失时自动跳过这一步。

## 用法

```bash
# 整个目录批量转换，输出到指定目录（拷 U 盘就用这个）
python ncm2mp3.py "D:\CloudMusic\VipSongsDownload" -o "H:\NCM转MP3"

# 单个文件，输出到同级的 ncm2mp3_out 目录
python ncm2mp3.py "song.ncm"

# 递归处理子目录
python ncm2mp3.py "D:\CloudMusic" -r -o "H:\音乐"

# 只要音频，不内嵌封面
python ncm2mp3.py "song.ncm" --no-cover

# 只看看里面到底是什么格式，不写文件
python ncm2mp3.py "D:\CloudMusic\VipSongsDownload" --probe
```

命令行参数：

| 参数 | 说明 |
|---|---|
| `target` | ncm 文件或所在目录（位置参数，必填） |
| `-o, --output` | 输出目录，默认 `<源目录>/ncm2mp3_out` |
| `--probe` | 只探测内部格式，不转换 |
| `--no-cover` | 不内嵌专辑封面 |
| `-r, --recursive` | 递归处理子目录 |

## 作为库调用

```python
from ncm2mp3 import parse_header, decrypt_to_file, detect_format

meta, mask, cover, offset = parse_header("song.ncm")
fmt = detect_format("song.ncm", mask, offset)      # 'mp3' / 'flac' / ...
decrypt_to_file("song.ncm", mask, offset, f"song.{fmt}")
```

## 实测数据

40 首 320kbps 曲目，共 386 MB、169 分钟，转换耗时约 22 秒（纯 Python，大整数批量异或优化，未用 C 扩展）。

## 常见问题

**Q：转出来的文件播不了？**
先确认用的是本版脚本。旧版脚本的输出文件大小看着正常，但文件头是乱的，播放器会报"格式不支持"。可用 `--probe` 检查识别出的格式是否正确。

**Q：为什么输出的是 flac 而不是 mp3？**
ncm 内部存什么就输出什么。VIP 无损音质下载的是 flac，标准音质是 mp3。本项目不做转码——如果要压成 mp3，请自行用 ffmpeg 处理。

**Q：元数据（歌名/歌手/专辑）会丢吗？**
不会。网易云下载的音频本身就带完整的 ID3/Vorbis 标签，解密只是还原原始数据，标签原样保留。ncm 头部另有一份元数据 JSON，可用于文件名命名（见 `parse_header` 返回值）。

**Q：U 盘用什么文件系统？**
老车机普遍不认 NTFS，建议 exFAT（单文件 >4GB 也支持）或 FAT32。

## 免责声明

本项目仅供个人把**自己已购买 / 已下载**的音乐迁移到自己的设备上播放，属于格式转换用途。

请勿用于批量破解、分发或任何商业用途。下载的音乐版权归原权利人所有，请尊重正版，支持你喜欢的音乐人。作者不对任何滥用行为负责。

## License

[MIT](LICENSE)

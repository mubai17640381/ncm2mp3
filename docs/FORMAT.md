# NCM 文件格式解析（含 2024 新版变化）

本文档记录 `.ncm` 容器的二进制结构与解密流程，基于 2026 年 10 月网易云音乐 PC 客户端产出的文件实测整理。网上流传的资料大多已过时，这里把新旧差异一并写清楚。

## 一、整体结构

```
┌──────────────────┬────────────────────────────────────┐
│ magic 8B         │ "CTENFDAM"                          │
│ gap 2B           │ 保留                                │
│ key_len 4B (LE)  │ 音频密钥段长度（实测 128）            │
│ key_data N B     │ XOR 0x64 → AES-128-ECB(CORE_KEY)    │
│ meta_len 4B (LE) │ 元数据段长度                         │
│ meta_data N B    │ XOR 0x63 → [AES] → base64 → JSON    │
│ crc32 4B         │                                     │
│ gap 5B           │ 保留                                │
│ img_len 4B (LE)  │ 封面长度                             │
│ img_data N B     │ JPEG / PNG 原图，未加密               │
│ audio_data ...   │ 用 256 周期密钥流异或                │
└──────────────────┴────────────────────────────────────┘
```

结构本身在新旧版本中没有变化，变的是**密钥长度、密钥流相位、元数据段是否 AES**。

## 二、音频密钥段

```python
key_data = bytes(b ^ 0x64 for b in raw_key_data)
plain    = AES_128_ECB_decrypt(key_data, CORE_KEY)   # PKCS7 去填充
rc4_key  = plain[17:]                                # 去掉前缀 "neteasecloudmusic"
```

`CORE_KEY = 687a4852416d736f356b496e62617857`（即 ASCII `hzHRAmso5kInbaxW`）。

- **旧版**：`rc4_key` 长度 16
- **新版**：`rc4_key` 长度 99（base64 字符风格的长随机串）

两者都能喂给下面同一个密钥盒算法，代码无需分支。

## 三、密钥盒与密钥流（最容易踩坑的地方）

```python
def build_key_box(key):
    box = list(range(256))
    c = last_byte = offset = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last_byte + key[offset]) & 0xFF
        offset = (offset + 1) % len(key)
        box[i] = box[c]
        box[c] = swap
        last_byte = c
    return box
```

这是一个 RC4 KSA 的变体。密钥流第 j 个字节为：

```python
mask[j] = box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
```

**关键：官方实现里循环变量 `i` 从 1 开始计数**，即第 1 个数据字节对应 `j = 1`，而不是 `j = 0`。所以真正用于异或的序列要整体左移一位：

```python
raw = bytes(mask_expr(j) for j in range(256))
mask = raw[1:] + raw[:1]        # 少这一步 = 输出全是坏文件
```

由于 `j = i & 0xFF`，整个密钥流周期就是 256，可以一次性算完再整块异或，比逐字节循环快一个数量级（40 首 386MB 约 22 秒）。

## 四、元数据段

```python
meta_raw = bytes(b ^ 0x63 for b in raw_meta_data)
```

之后分两代：

```python
# 旧版
body = AES_128_ECB_decrypt(meta_raw, META_KEY)[len("163 key(Don't modify):"):]
meta = json.loads(base64.b64decode(body))

# 新版（2024+）：不再有 AES 这一步
body = meta_raw[len("163 key(Don't modify):"):]
meta = json.loads(base64.b64decode(body))
```

`META_KEY = 2331346c6a6b5f215c5d2630553c2628`。

兼容性判断很简单：XOR 之后看是否以 `163 key(Don't modify):` 开头，是就直接 base64，否则先过一遍 AES 再看。

> 注：实测部分新版文件的元数据段仍解不出明文 JSON（疑似还有一层未公开的改动）。这不影响转换——音频文件名沿用原文件名，真实容器格式用下面的魔数探测即可。

## 五、音频数据格式探测

不要相信元数据里的 `format` 字段（新版可能拿不到），解密前 16 字节后看魔数最可靠：

| 明文起始 | 格式 |
|---|---|
| `ID3` | mp3 |
| `0xFF 0xFB/0xFA/0xF3`（`(b1 & 0xE0) == 0xE0`） | mp3（裸 MPEG 帧） |
| `fLaC` | flac |
| `OggS` | ogg |
| `RIFF` | wav |
| 偏移 4 处 `ftyp` | m4a |

## 六、如何判断一段密文用的是流密码还是分组密码

逆向时如果不知道对方换了什么算法，可以用熵做个快速判别：把密文按 `i % 256` 分组，分别算信息熵。

- 若分组熵明显低于整体熵（本例：7.73～7.76 vs 8.00），说明明文分布特征被保留在固定位置上 → **周期 256 的 XOR 流密码**
- 若分组熵与整体熵一致且都接近 8.0 → **AES 之类的分组密码**

本项目就是靠这个特征先确认"还是 XOR 流密码、只是密钥或相位变了"，再暴力搜索相位才定位到问题。

## 七、性能

| 方式 | 40 首 / 386MB 耗时 |
|---|---|
| 逐字节 Python 循环 | 约 数分钟（不可用） |
| 256 字节密钥流 + 大整数整块异或 | 约 22 秒 |

`int.from_bytes(...)` 异或后再 `to_bytes(...)`，让大块数据的异或落在 C 层完成。

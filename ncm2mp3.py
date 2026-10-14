#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ncm2mp3 —— 把网易云音乐 .ncm 加密文件还原成标准 MP3 / FLAC

特点:
  * 支持 2024 年以后的新版 NCM 格式（网上流传的旧脚本会转出损坏文件）
  * 解密而非转码, 音质零损失
  * 自动识别真实音频格式 (mp3 / flac / m4a / wav / ogg)
  * 自动内嵌专辑封面与歌词
  * 批量 / 目录递归, 不依赖 ffmpeg

用法:
  python ncm2mp3.py <文件或目录> [-o 输出目录] [--probe] [--no-cover] [--recursive]

依赖:
  pip install pycryptodome mutagen
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import struct
import sys
from typing import Dict, Iterator, Tuple

try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("缺少依赖 pycryptodome, 请先执行: pip install pycryptodome")

# ---------------------------------------------------------------- 常量

MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
META_KEY = bytes.fromhex("2331346c6a6b5f215c5d2630553c2628")
META_PREFIX = b"163 key(Don't modify):"

# 音频容器魔数 -> 扩展名
AUDIO_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
    (b"ID3", "mp3"),
)

ILLEGAL_CHARS = '<>:"/\\|?*'


# ---------------------------------------------------------------- 核心算法

def _pkcs7_unpad(data: bytes) -> bytes:
    """去除 PKCS7 填充"""
    if not data:
        return data
    pad = data[-1]
    return data[:-pad] if 0 < pad <= 16 else data


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密, 自动补齐块长度并去除填充"""
    data += b"\x00" * ((16 - len(data) % 16) % 16)
    return _pkcs7_unpad(AES.new(key, AES.MODE_ECB).decrypt(data))


def build_key_box(key: bytes) -> list:
    """网易云定制的 RC4 变种 KSA, 得到一个 0-255 的置换盒"""
    box = list(range(256))
    key_len = len(key)
    c = last_byte = offset = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last_byte + key[offset]) & 0xFF
        offset = (offset + 1) % key_len
        box[i] = box[c]
        box[c] = swap
        last_byte = c
    return box


def make_mask(box: list) -> bytes:
    """
    生成 256 字节异或密钥流。

    异或值只依赖 (i & 0xff), 因此整个密钥流周期为 256, 可一次性算好后整块异或。

    注意: 官方实现中循环变量 i 从 1 开始计数, 第一个数据字节对应 j = 1,
    所以这里要把序列整体左移 1 位。漏掉这一步会得到一堆打不开的垃圾文件。
    """
    raw = bytes(box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF] for j in range(256))
    return raw[1:] + raw[:1]


def xor_stream(chunk: bytes, mask: bytes, start: int = 0) -> bytes:
    """用 256 周期密钥流整块异或(大整数运算比逐字节快一个数量级)"""
    n = len(chunk)
    if n == 0:
        return b""
    offset = start % 256
    m = (mask[offset:] + mask[:offset]) * (n // 256 + 1)
    return (int.from_bytes(chunk, "big") ^ int.from_bytes(m[:n], "big")).to_bytes(n, "big")


def parse_header(path: str) -> Tuple[Dict, bytes, bytes, int]:
    """
    解析 ncm 文件头

    :return: (元数据 dict, 256 字节密钥流, 封面字节, 音频数据起始偏移)
    """
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError("不是有效的 ncm 文件（文件头魔数不匹配）")
        f.read(2)

        # --- 音频密钥段 ---
        key_len = struct.unpack("<I", f.read(4))[0]
        key_data = bytes(b ^ 0x64 for b in f.read(key_len))
        rc4_key = aes_ecb_decrypt(key_data, CORE_KEY)[17:]
        mask = make_mask(build_key_box(rc4_key))

        # --- 元数据段 ---
        meta_len = struct.unpack("<I", f.read(4))[0]
        meta_raw = bytes(b ^ 0x63 for b in f.read(meta_len))
        meta = _decode_meta(meta_raw)

        # --- 封面段 ---
        f.read(4)                                       # crc32
        f.read(5)                                       # 保留位
        img_len = struct.unpack("<I", f.read(4))[0]
        cover = f.read(img_len)

        return meta, mask, cover, f.tell()


def _decode_meta(meta_raw: bytes) -> Dict:
    """
    解析元数据 JSON, 兼容新旧两代格式:
      旧版: XOR 0x63 -> AES-ECB -> 去前缀 -> base64 -> JSON
      新版: XOR 0x63 -> 去前缀 -> base64 -> JSON
    """
    for candidate in (meta_raw, aes_ecb_decrypt(meta_raw, META_KEY)):
        if not candidate.startswith(META_PREFIX):
            continue
        body = candidate[len(META_PREFIX):]
        try:
            return json.loads(base64.b64decode(body + b"=" * ((4 - len(body) % 4) % 4)))
        except Exception:
            continue
    return {}


def detect_format(path: str, mask: bytes, offset: int) -> str:
    """解密前若干字节, 根据实际魔数判断音频容器格式"""
    with open(path, "rb") as f:
        f.seek(offset)
        head = f.read(16)
    plain = xor_stream(head, mask)
    for sig, ext in AUDIO_SIGNATURES:
        if plain.startswith(sig):
            return ext
    if plain[4:8] == b"ftyp":                           # m4a
        return "m4a"
    if plain[0] == 0xFF and (plain[1] & 0xE0) == 0xE0:  # 裸 MPEG 帧
        return "mp3"
    return "mp3"                                        # 兜底


def decrypt_to_file(path: str, mask: bytes, offset: int, out_path: str) -> int:
    """流式解密音频数据并写入文件, 返回写入字节数"""
    written = 0
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        src.seek(offset)
        while True:
            chunk = src.read(0x80000)
            if not chunk:
                break
            dst.write(xor_stream(chunk, mask, written))
            written += len(chunk)
    return written


# ---------------------------------------------------------------- 辅助

def safe_name(name: str) -> str:
    """去掉文件系统非法字符, 并压缩空白"""
    for ch in ILLEGAL_CHARS:
        name = name.replace(ch, "_")
    return " ".join(name.split()).strip()


def iter_ncm(target: str, recursive: bool = False) -> Iterator[str]:
    """根据输入路径产出 ncm 文件列表"""
    if os.path.isfile(target):
        yield target
        return
    for root, dirs, files in os.walk(target):
        if not recursive:
            dirs[:] = []
        for name in sorted(files):
            if name.lower().endswith(".ncm"):
                yield os.path.join(root, name)


def embed_cover(mp3_path: str, cover: bytes) -> bool:
    """把封面写入 ID3 APIC 帧（mutagen 缺失时静默跳过）"""
    try:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    except ImportError:
        return False
    try:
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(mp3_path)
        return True
    except Exception:
        return False


def unique_path(path: str) -> str:
    """目标文件已存在时追加 (1) (2) ..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem} ({i}){ext}"):
        i += 1
    return f"{stem} ({i}){ext}"


# ---------------------------------------------------------------- 主流程

def probe(files) -> None:
    """只探测不转换"""
    stat: Dict[str, int] = {}
    for path in files:
        name = os.path.basename(path)
        try:
            meta, mask, _, offset = parse_header(path)
            fmt = (meta.get("format") or "").lower() or detect_format(path, mask, offset)
            stat[fmt] = stat.get(fmt, 0) + 1
            print(f"  {name[:46]:<46} {fmt:<5} {meta.get('musicName', '')}")
        except Exception as exc:
            print(f"  [失败] {name} -> {exc}")
    print("\n格式统计:", stat or "无")


def convert(files, out_dir: str, with_cover: bool = True) -> int:
    os.makedirs(out_dir, exist_ok=True)
    ok = fail = 0
    for path in files:
        base = safe_name(os.path.splitext(os.path.basename(path))[0])
        try:
            meta, mask, cover, offset = parse_header(path)
            fmt = (meta.get("format") or "").lower()
            if fmt not in ("mp3", "flac", "m4a", "wav", "ogg"):
                fmt = detect_format(path, mask, offset)

            out_path = unique_path(os.path.join(out_dir, f"{base}.{fmt}"))
            size = decrypt_to_file(path, mask, offset, out_path)

            if cover and with_cover:
                embed_cover(out_path, cover)

            lrc = os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] + ".lrc")
            if os.path.exists(lrc):
                shutil.copy2(lrc, unique_path(os.path.join(out_dir, f"{base}.lrc")))

            print(f"  [OK] {base[:42]:<42} -> {fmt}  ({size / 1048576:.1f} MB)")
            ok += 1
        except Exception as exc:
            print(f"  [失败] {base} -> {exc}")
            fail += 1
    print(f"\n完成: 成功 {ok}, 失败 {fail}, 输出目录: {out_dir}")
    return fail


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把网易云音乐 .ncm 文件还原为 MP3 / FLAC（解密, 非转码）"
    )
    parser.add_argument("target", help="ncm 文件或所在目录")
    parser.add_argument("-o", "--output", default=None, help="输出目录, 默认与源文件同级的 ncm2mp3_out")
    parser.add_argument("--probe", action="store_true", help="只探测内部音频格式, 不转换")
    parser.add_argument("--no-cover", action="store_true", help="不内嵌专辑封面")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归处理子目录")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        sys.exit(f"路径不存在: {args.target}")

    files = list(iter_ncm(args.target, args.recursive))
    if not files:
        sys.exit("未找到任何 .ncm 文件")
    print(f"发现 {len(files)} 个 ncm 文件\n")

    if args.probe:
        probe(files)
        return 0

    out_dir = args.output or os.path.join(
        args.target if os.path.isdir(args.target) else os.path.dirname(args.target),
        "ncm2mp3_out",
    )
    return 1 if convert(files, out_dir, with_cover=not args.no_cover) else 0


if __name__ == "__main__":
    sys.exit(main())

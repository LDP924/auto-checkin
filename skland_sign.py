#!/usr/bin/env python3
"""森空岛自动签到：明日方舟（角色签到 + 登岛检票）+ 终末地（角色签到 + 登岛检票）。

认证链路：鹰角通行证 token → OAuth2 grant_code → cred + sign_token → 签名请求。
签名算法：sign = MD5( HMAC-SHA256( sign_token, path + body + timestamp + headerCA_json ) )。

零第三方依赖：HTTP（http.client 直连，保留自定义 header 大小写）、加密、gzip 全部标准库实现，
任何裸 Python 3.6+ 容器直接运行（无需 pip）。

凭证来源（优先级从高到低）：
  1. 环境变量 SKLAND_TOKENS（逗号分隔多个）
  2. creds.txt（每行一个，# 开头为注释）
  3. 环境变量 SKLAND_PHONE + SKLAND_PASSWORD（密码登录，亦用于 token 失效时自动续期）
"""

import base64
import gzip
import hashlib
import hmac
import http.client
import io
import json
import os
import ssl
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlparse

# ---------------------------------------------------------------- HTTP 层
# http.client 直连：逐字节保留 header 大小写（dId / vName / X-Requested-With
# 等自定义头对服务端 WAF 校验至关重要，urllib 会强制改写大小写故不可用），
# gzip 自动解压，零第三方依赖，裸 Python 3.6+ 环境直接运行。


class RequestException(Exception):
    """网络请求失败（连接 / 超时 / 协议错误）。"""


_CERT_VERIFY_ERROR = getattr(ssl, "SSLCertVerificationError", ssl.CertificateError)


class _Resp:
    __slots__ = ("status_code", "ok", "headers", "_body")

    def __init__(self, status, headers, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers
        self._body = body

    def json(self):
        return json.loads(self._body.decode("utf-8", "replace"))

    @property
    def text(self):
        return self._body.decode("utf-8", "replace")


def _send(method, url, body, headers, timeout):
    """单次发送，返回 (status, headers_dict, raw)。证书校验失败时降级重试一次。"""
    u = urlsplit(url)
    target = (u.path or "/") + ("?" + u.query if u.query else "")
    unverified = False
    for _ in range(2):
        try:
            if u.scheme == "https":
                context = ssl._create_unverified_context() if unverified else None
                conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                                   timeout=timeout, context=context)
            else:
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
            try:
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status, rheaders = resp.status, dict(resp.getheaders())
            finally:
                conn.close()
            if rheaders.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return status, rheaders, raw
        except _CERT_VERIFY_ERROR:
            if unverified:
                raise RequestException("SSL 证书校验失败（系统缺少 CA 证书）")
            log("  系统 CA 证书校验失败，降级为跳过证书校验重试")
            unverified = True
        except (OSError, http.client.HTTPException) as exc:
            raise RequestException(str(exc))
    raise RequestException("unreachable")


def http_get(url, headers=None, timeout=15):
    status, rheaders, raw = _send("GET", url, None, dict(headers or {}), timeout)
    try:
        print('[RAW][skland GET {}] {} → {}'.format(
            urlsplit(url).path, status, raw[:300].decode('utf-8', 'replace')))
    except Exception:
        pass
    return _Resp(status, rheaders, raw)


def http_post(url, json_body=None, data=None, headers=None, timeout=15):
    """json_body: dict → JSON 请求体；data: bytes → 原始 body。"""
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode()
        hdrs.setdefault("Content-Type", "application/json")
    status, rheaders, raw = _send("POST", url, data, hdrs, timeout)
    try:
        print('[RAW][skland POST {}] {} → {}'.format(
            urlsplit(url).path, status, raw[:300].decode('utf-8', 'replace')))
    except Exception:
        pass
    return _Resp(status, rheaders, raw)


SK_BASE = "https://zonai.skland.com"
SK_API = f"{SK_BASE}/api/v1"
SK_WEB = f"{SK_BASE}/web/v1"
AS_BASE = "https://as.hypergryph.com"
PASSWORD_LOGIN_URL = f"{AS_BASE}/user/auth/v1/token_by_phone_password"
TIMEOUT = 15
APP_CODE = "4ca99fa6b56cc2ba"
UA = "Skland/1.5.1 (com.hypergryph.skland; build:100501001; Android 34; ) Okhttp/4.11.0"

SIGN_PROFILES = {
    "default": {"platform": "1", "vName": "1.5.1", "dId": None},
    "endfield": {"platform": "3", "vName": "1.0.0", "dId": ""},
}
CHECKIN_MAP = {"arknights": "1", "endfield": "3"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 持久目录：云效流水线把 /root/.cache 列为默认缓存目录（跨次运行持久）。
# 注意：alinux4 等基础镜像里该目录可能不存在，必须主动创建，
# 否则 token 会写回随容器销毁的 workspace，缓存步骤找不到目录可打包（skip）
_CACHE_ROOT = "/root/.cache"
try:
    os.makedirs(_CACHE_ROOT, exist_ok=True)
except OSError:
    pass
PERSIST_DIR = _CACHE_ROOT if os.access(_CACHE_ROOT, os.W_OK) else SCRIPT_DIR
LOG_FILE = os.path.join(SCRIPT_DIR, "sign_log.txt")
_CACHED_DID = None
_TIME_OFFSET = 0  # 本地与服务器时间偏差（秒），启动时校准


def sync_time_offset():
    """校准本地时间与森空岛服务器的时间偏差"""
    global _TIME_OFFSET
    try:
        from email.utils import parsedate_to_datetime
        resp = http_get(SK_BASE, timeout=TIMEOUT)
        date_str = resp.headers.get("Date", "")
        if date_str:
            server_dt = parsedate_to_datetime(date_str)
            server_ts = int(server_dt.timestamp())
            local_ts = int(time.time())
            _TIME_OFFSET = server_ts - local_ts
            if abs(_TIME_OFFSET) > 5:
                log(f"  时间校准: 本地与服务器偏差 {_TIME_OFFSET} 秒，已自动修正", False)
    except Exception as e:
        log(f"  时间校准失败: {e}", False)


def now_ts():
    """返回校准后的秒级时间戳"""
    return str(int(time.time()) + _TIME_OFFSET)


# ---------------------------------------------------------------- 数美设备指纹

def get_did():
    """设备指纹 dId：优先级 环境变量 SKLAND_DID > config.json 的 did > 数美实时注册 > 随机值。

    云效等 CI 的机房 IP 已被数美风控（code 1901），实时注册会失败；
    本地生成一个 dId 填入环境变量或 config 即可长期复用（deviceId 生命周期长）。
    dId 失效（重新出现"设备信息无效"）时本地再生成一个换上：
        python3 -c "import skland_sign as s; print(s.gen_sm_did())"
    """
    global _CACHED_DID
    if _CACHED_DID is None:
        configured = os.environ.get("SKLAND_DID", "").strip() or _load_configured_did()
        if configured:
            _CACHED_DID = configured
            log(f"  使用预配置 dId: {configured[:12]}...{configured[-6:]}")
        else:
            try:
                _CACHED_DID = gen_sm_did()
            except Exception as exc:
                log(f"  数美 dId 注册失败，退回随机指纹: {exc}")
                _CACHED_DID = base64.b64encode(str(uuid.uuid4()).replace("-", "")[:32].encode()).decode().rstrip("=")
    return _CACHED_DID


def _load_configured_did():
    """从 config.json 读预配置 dId（有则返回，无则空串）。"""
    try:
        return str(load_config().get("did", "") or "").strip()
    except Exception:
        return ""


# 数美(ShuMei)设备指纹 — generate_cred_by_code 自 2024-09 起强制校验，
# 缺失真实 dId 时返回 code=10001「设备信息无效」。下方为纯 Python 逆向实现，
# 运行时实时向数美接口注册真实 dId（无需浏览器）。
# 参考: https://github.com/nuthx/auto-sign (SecuritySm.py)

SM_CONFIG = {
    "organization": "UWXspnCCJN4sfYlNfqps",
    "appId": "default",
    "publicKey": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCmxMNr7n8ZeT0tE1R9j/mPixoinPkeM+k4VGIn/s0k7N5rJAfnZ0eMER+QhwFvshzo0LNmeUkpR8uIlU/GEVr8mN28sKmwd2gpygqj0ePnBmOW4v0ZVwbSYK+izkhVFk2V/doLoMbWy6b+UnA8mkjvg0iYWRByfRsK2gdl7llqCwIDAQAB",
}
DEVICE_URL = "https://fp-it.portal101.cn/deviceprofile/v4"

_SM_DES_RULE = {
    "appId": {"cipher": "DES", "is_encrypt": 1, "key": "uy7mzc4h", "obfuscated_name": "xx"},
    "box": {"is_encrypt": 0, "obfuscated_name": "jf"},
    "canvas": {"cipher": "DES", "is_encrypt": 1, "key": "snrn887t", "obfuscated_name": "yk"},
    "clientSize": {"cipher": "DES", "is_encrypt": 1, "key": "cpmjjgsu", "obfuscated_name": "zx"},
    "organization": {"cipher": "DES", "is_encrypt": 1, "key": "78moqjfc", "obfuscated_name": "dp"},
    "os": {"cipher": "DES", "is_encrypt": 1, "key": "je6vk6t4", "obfuscated_name": "pj"},
    "platform": {"cipher": "DES", "is_encrypt": 1, "key": "pakxhcd2", "obfuscated_name": "gm"},
    "plugins": {"cipher": "DES", "is_encrypt": 1, "key": "v51m3pzl", "obfuscated_name": "kq"},
    "pmf": {"cipher": "DES", "is_encrypt": 1, "key": "2mdeslu3", "obfuscated_name": "vw"},
    "protocol": {"is_encrypt": 0, "obfuscated_name": "protocol"},
    "referer": {"cipher": "DES", "is_encrypt": 1, "key": "y7bmrjlc", "obfuscated_name": "ab"},
    "res": {"cipher": "DES", "is_encrypt": 1, "key": "whxqm2a7", "obfuscated_name": "hf"},
    "rtype": {"cipher": "DES", "is_encrypt": 1, "key": "x8o2h2bl", "obfuscated_name": "lo"},
    "sdkver": {"cipher": "DES", "is_encrypt": 1, "key": "9q3dcxp2", "obfuscated_name": "sc"},
    "status": {"cipher": "DES", "is_encrypt": 1, "key": "2jbrxxw4", "obfuscated_name": "an"},
    "subVersion": {"cipher": "DES", "is_encrypt": 1, "key": "eo3i2puh", "obfuscated_name": "ns"},
    "svm": {"cipher": "DES", "is_encrypt": 1, "key": "fzj3kaeh", "obfuscated_name": "qr"},
    "time": {"cipher": "DES", "is_encrypt": 1, "key": "q2t3odsk", "obfuscated_name": "nb"},
    "timezone": {"cipher": "DES", "is_encrypt": 1, "key": "1uv05lj5", "obfuscated_name": "as"},
    "tn": {"cipher": "DES", "is_encrypt": 1, "key": "x9nzj1bp", "obfuscated_name": "py"},
    "trees": {"cipher": "DES", "is_encrypt": 1, "key": "acfs0xo4", "obfuscated_name": "pi"},
    "ua": {"cipher": "DES", "is_encrypt": 1, "key": "k92crp1t", "obfuscated_name": "bj"},
    "url": {"cipher": "DES", "is_encrypt": 1, "key": "y95hjkoo", "obfuscated_name": "cf"},
    "version": {"is_encrypt": 0, "obfuscated_name": "version"},
    "vpw": {"cipher": "DES", "is_encrypt": 1, "key": "r9924ab5", "obfuscated_name": "ca"},
}

_SM_BROWSER_ENV = {
    "plugins": "MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1",
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "canvas": "259ffe69", "timezone": -480, "platform": "Win32",
    "url": "https://www.skland.com/", "referer": "",
    "res": "1920_1080_24_1.25", "clientSize": "0_0_1080_1920_1920_1080_1920_1080", "status": "0011",
}


# ---- 纯 Python 密码学（DES-ECB / AES-128-CBC / RSA-PKCS1v15），已与 pycryptodome 对拍验证 ----

# ---- DES 常量表 ----
_IP     = [58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]
_IP_INV = [40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]
_E      = [32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,12,13,14,15,16,17,16,17,18,19,20,21,20,21,22,23,24,25,24,25,26,27,28,29,28,29,30,31,32,1]
_P      = [16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]
_PC1    = [57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35,27,19,11,3,60,52,44,36,63,55,47,39,31,23,15,7,62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,28,20,12,4]
_PC2    = [14,17,11,24,1,5,3,28,15,6,21,10,23,19,12,4,26,8,16,7,27,20,13,2,41,52,31,37,47,55,30,40,51,45,33,48,44,49,39,56,34,53,46,42,50,36,29,32]
_SBOX = [
 [[14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7],[0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8],[4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0],[15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13]],
 [[15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10],[3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5],[0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15],[13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9]],
 [[10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8],[13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1],[13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7],[1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12]],
 [[7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15],[13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9],[10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4],[3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14]],
 [[2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9],[14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6],[4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14],[11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3]],
 [[12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11],[10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8],[9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6],[4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13]],
 [[4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1],[13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6],[1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2],[6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12]],
 [[13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7],[1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2],[7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8],[2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11]],
]

def _bytes_to_bits(b):
    bits = []
    for byte in b:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits

def _bits_to_bytes(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i + j]
        out.append(byte)
    return bytes(out)

def _des_subkeys(key):
    kb = _bytes_to_bits(key)
    c = [kb[_PC1[i] - 1] for i in range(28)]
    d = [kb[_PC1[i + 28] - 1] for i in range(28)]
    subs = []
    shifts = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]
    for s in shifts:
        c = c[s:] + c[:s]
        d = d[s:] + d[:s]
        cd = c + d
        subs.append([cd[_PC2[i] - 1] for i in range(48)])
    return subs

def _des_encrypt_block(block, subkeys):
    old = _bytes_to_bits(block)
    bits = [old[_IP[i] - 1] for i in range(64)]
    L = bits[:32]; R = bits[32:]
    for sk in subkeys:
        er = [R[_E[i] - 1] for i in range(48)]
        x = [er[j] ^ sk[j] for j in range(48)]
        b = []
        for j in range(8):
            s = x[j*6:(j+1)*6]
            row = (s[0] << 1) | s[5]
            col = (s[1] << 3) | (s[2] << 2) | (s[3] << 1) | s[4]
            v = _SBOX[j][row][col]
            b.extend([(v >> k) & 1 for k in (3, 2, 1, 0)])
        b = [b[_P[i] - 1] for i in range(32)]
        nR = [b[j] ^ L[j] for j in range(32)]
        L, R = R, nR
    bits = R + L
    bits = [bits[_IP_INV[i] - 1] for i in range(64)]
    return _bits_to_bytes(bits)

def pure_des_ecb(key8, data):
    """单 DES-ECB（数美各字段用 8 字节密钥，等价于原 cryptography TripleDES(8字节)）。"""
    subs = _des_subkeys(key8)
    pad = (8 - len(data) % 8) % 8
    data = data + b"\x00" * pad
    out = bytearray()
    for i in range(0, len(data), 8):
        out += _des_encrypt_block(data[i:i+8], subs)
    return bytes(out)

# ---- AES-128-CBC ----
_AES_S = [
 0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
 0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
 0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
 0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
 0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
 0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
 0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
 0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
 0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
 0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
 0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
 0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
 0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
 0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
 0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
 0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16]

def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)

def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p

def _key_expansion(key):
    w = [[key[4*i + j] for j in range(4)] for i in range(4)]
    rcon = 1
    for c in range(4, 44):
        t = w[c-1][:]
        if c % 4 == 0:
            t = t[1:] + t[:1]
            t = [_AES_S[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([w[c-4][j] ^ t[j] for j in range(4)])
    return [[[w[4*rnd + c][r] for c in range(4)] for r in range(4)] for rnd in range(11)]

def pure_aes_cbc(key16, iv, data):
    """AES-128-CBC，参数顺序 (密钥16字节, IV16字节, 明文)，返回 hex 字符串。"""
    rk = _key_expansion(key16)
    pad = (16 - len(data) % 16) % 16
    data = data + b"\x00" * pad
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytearray(data[i:i+16])
        for j in range(16):
            block[j] ^= prev[j]
        state = [[block[r + 4*c] for c in range(4)] for r in range(4)]
        for r in range(4):
            for c in range(4):
                state[r][c] ^= rk[0][r][c]
        for rnd in range(1, 11):
            for r in range(4):
                for c in range(4):
                    state[r][c] = _AES_S[state[r][c]]
            sr = [[0]*4 for _ in range(4)]
            for r in range(4):
                for c in range(4):
                    sr[r][c] = state[r][(c + r) % 4]
            state = sr
            if rnd < 10:
                for c in range(4):
                    col = [state[r][c] for r in range(4)]
                    state[0][c] = _gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3]
                    state[1][c] = col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3]
                    state[2][c] = col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3)
                    state[3][c] = _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)
            for r in range(4):
                for c in range(4):
                    state[r][c] ^= rk[rnd][r][c]
        enc = bytes(state[r][c] for c in range(4) for r in range(4))
        out += enc
        prev = enc
    return out.hex()

# ---- RSA PKCS1v15 公钥加密（从 DER 公钥解析 n/e） ----
def _der_nodes(data, start, end):
    pos = start
    while pos < end:
        tag = data[pos]; pos += 1
        ln = data[pos]; pos += 1
        if ln & 0x80:
            nb = ln & 0x7f
            ln = int.from_bytes(data[pos:pos+nb], "big"); pos += nb
        yield tag, pos, pos + ln
        pos += ln

def _parse_n_e(der_b64):
    data = base64.b64decode(der_b64)
    spki = list(_der_nodes(data, 0, len(data)))[0]
    kids = list(_der_nodes(data, spki[1], spki[2]))
    bitstring = kids[1]
    bs_start = bitstring[1] + 1
    rsa = list(_der_nodes(data, bs_start, bitstring[2]))[0]
    rkids = list(_der_nodes(data, rsa[1], rsa[2]))
    n = int.from_bytes(data[rkids[0][1]:rkids[0][2]], "big")
    e = int.from_bytes(data[rkids[1][1]:rkids[1][2]], "big")
    return n, e

def pure_rsa_pkcs1v15(pubkey_b64, msg):
    n, e = _parse_n_e(pubkey_b64)
    k = (n.bit_length() + 7) // 8
    ps_len = k - len(msg) - 3
    ps = b""
    while len(ps) < ps_len:
        b = os.urandom(1)[0]
        if b != 0:
            ps += bytes([b])
    em = b"\x00\x02" + ps + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), e, n)
    return c.to_bytes(k, "big")


def _sm_des(o):
    result = {}
    for key, res in o.items():
        if key in _SM_DES_RULE:
            rule = _SM_DES_RULE[key]
            if rule["is_encrypt"] == 1:
                # 与原 cryptography TripleDES/ECB 行为一致: 补 8 字节 \x00 后只加密完整块
                data = str(res).encode() + b"\x00" * 8
                enc = pure_des_ecb(rule["key"].encode(), data)
                enc = enc[: (len(data) // 8) * 8]
                res = base64.b64encode(enc).decode()
            result[rule["obfuscated_name"]] = res
        else:
            result[key] = res
    return result


def _sm_aes(v: bytes, k: bytes):
    iv = b"0102030405060708"
    # 与原 cryptography AES/CBC 行为一致: 至少补 1 字节, 再补到 16 字节整数倍
    v = v + b"\x00"
    while len(v) % 16 != 0:
        v += b"\x00"
    return pure_aes_cbc(k, iv, v)


def _gzip_no_mtime(data):
    """gzip.compress(mtime=) 需要 Python 3.8+，改用 GzipFile(mtime=0)（3.1+）
    兼容旧版；OS 字节补写 0x03（Unix），与 gzip.compress 输出完全一致。"""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=2, mtime=0) as f:
        f.write(data)
    out = bytearray(buf.getvalue())
    out[9] = 3  # GzipFile 写 0xff(unknown)，gzip.compress 写 0x03(Unix)
    return bytes(out)


def _sm_gzip(o):
    return base64.b64encode(_gzip_no_mtime(json.dumps(o, ensure_ascii=False).encode()))


def _sm_get_tn(o):
    result_list = []
    for i in sorted(o.keys()):
        v = o[i]
        if isinstance(v, (int, float)):
            v = str(v * 10000)
        elif isinstance(v, dict):
            v = _sm_get_tn(v)
        result_list.append(v)
    return "".join(result_list)


def _sm_get_smid():
    t = time.localtime()
    _t = "{}{:0>2d}{:0>2d}{:0>2d}{:0>2d}{:0>2d}".format(
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    uid = str(uuid.uuid4())
    v = _t + hashlib.md5(uid.encode()).hexdigest() + "00"
    smsk = hashlib.md5(("smsk_web_" + v).encode()).hexdigest()[0:14]
    return v + smsk + "0"


def gen_sm_did():
    """调用数美接口生成真实设备指纹 dId（返回 'B' + deviceId）"""
    uid = str(uuid.uuid4()).encode()
    priId = hashlib.md5(uid).hexdigest()[0:16]
    ep = base64.b64encode(pure_rsa_pkcs1v15(SM_CONFIG["publicKey"], uid)).decode()
    browser = _SM_BROWSER_ENV.copy()
    ct = int(time.time() * 1000)
    browser.update({"vpw": str(uuid.uuid4()), "svm": ct, "trees": str(uuid.uuid4()), "pmf": ct})
    des_target = {
        **browser, "protocol": 102, "organization": SM_CONFIG["organization"],
        "appId": SM_CONFIG["appId"], "os": "web", "version": "3.0.0", "sdkver": "3.0.0",
        "box": "", "rtype": "all", "smid": _sm_get_smid(), "subVersion": "1.0.0", "time": 0,
    }
    des_target["tn"] = hashlib.md5(_sm_get_tn(des_target).encode()).hexdigest()
    des_result = _sm_aes(_sm_gzip(_sm_des(des_target)), priId.encode())
    resp = http_post(DEVICE_URL, json_body={
        "appId": "default", "compress": 2, "data": des_result, "encode": 5,
        "ep": ep, "organization": SM_CONFIG["organization"], "os": "web",
    }, timeout=TIMEOUT).json()
    if resp.get("code") != 1100:
        raise RuntimeError(f"数美接口返回异常: {resp.get('message', resp)}")
    return "B" + resp["detail"]["deviceId"]


def log(msg, to_file=False):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if to_file:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- 凭证获取


def load_config():
    path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_tokens():
    tokens = [t.strip() for t in os.getenv("SKLAND_TOKENS", "").split(",") if t.strip()]
    if not tokens:
        creds_path = os.path.join(PERSIST_DIR, "creds.txt")
        if os.path.exists(creds_path):
            with open(creds_path, "r", encoding="utf-8") as f:
                tokens = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return [t for t in tokens if len(t) >= 10]


def load_account_creds(config):
    """手机号+密码：环境变量 SKLAND_PHONE / SKLAND_PASSWORD 优先，config.json 的 phone / password 兜底。"""
    phone = os.getenv("SKLAND_PHONE", "").strip() or str(config.get("phone") or "").strip()
    password = os.getenv("SKLAND_PASSWORD", "").strip() or str(config.get("password") or "").strip()
    return phone, password


def login_by_password(phone, password, to_file=False):
    """手机号+密码登录鹰角通行证，成功返回新 token。"""
    try:
        r = http_post(
            PASSWORD_LOGIN_URL,
            json_body={"phone": phone, "password": password},
            headers={"Content-Type": "application/json", "User-Agent": UA,
                     "dId": get_did(), "X-Requested-With": "com.hypergryph.skland"},
            timeout=TIMEOUT,
        ).json()
    except RequestException as exc:
        log(f"  密码登录请求失败: {exc}", to_file)
        return None
    if r.get("status") != 0:
        log(f"  密码登录失败: {r.get('msg', '未知错误')}", to_file)
        return None
    return r.get("data", {}).get("token") or None


def save_new_token(new_token, old_token=None, to_file=False):
    """新 token 写回 creds.txt（不存在则创建）。流水线容器只读时静默跳过。"""
    try:
        creds_path = os.path.join(PERSIST_DIR, "creds.txt")
        if old_token and os.path.exists(creds_path):
            with open(creds_path, "r", encoding="utf-8") as f:
                text = f.read()
            if old_token in text:
                with open(creds_path, "w", encoding="utf-8") as f:
                    f.write(text.replace(old_token, new_token))
                log("  新 token 已替换写回 creds.txt", to_file)
                return
        with open(creds_path, "a", encoding="utf-8") as f:
            f.write(new_token + "\n")
        log("  新 token 已写入 creds.txt", to_file)
    except OSError:
        log("  无法写回 creds.txt（只读环境正常），本次运行继续", to_file)


# ---------------------------------------------------------------- 签名与请求


def compute_sign(sign_token, path, body_str, profile="default"):
    t = now_ts()
    p = SIGN_PROFILES[profile]
    header_ca = {"platform": p["platform"], "timestamp": t,
                 "dId": get_did() if p["dId"] is None else p["dId"], "vName": p["vName"]}
    ca_str = json.dumps(header_ca, separators=(",", ":"))
    message = path + body_str + t + ca_str
    hmac_hex = hmac.new(sign_token.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hashlib.md5(hmac_hex.encode()).hexdigest(), header_ca


def signed_request(cred, sign_token, url, method="POST", body=None, profile="default", extra_headers=None):
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    sign, ca = compute_sign(sign_token, urlparse(url).path, body_str, profile)
    headers = {
        "cred": cred, "User-Agent": UA, "Accept-Encoding": "gzip", "Connection": "close",
        "platform": ca["platform"], "timestamp": ca["timestamp"], "dId": ca["dId"],
        "vName": ca["vName"], "sign": sign,
    }
    if extra_headers:
        headers.update(extra_headers)
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
        return http_post(url, headers=headers, data=body_str.encode() if body_str else b"", timeout=TIMEOUT).json()
    return http_get(url, headers=headers, timeout=TIMEOUT).json()


def get_cred_and_sign_token(token):
    """token → grant_code → cred + sign_token。

    generate_cred_by_code 自 2024-09 起强制校验数美设备指纹，
    认证头必须携带 platform / timestamp / dId / vName，否则返回
    code=10001「设备信息无效」。
    """
    auth_h = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "X-Requested-With": "com.hypergryph.skland",
        "platform": "3",
        "timestamp": now_ts(),
        "dId": get_did(),
        "vName": "1.0.0",
    }
    r = http_post(f"{AS_BASE}/user/oauth2/v2/grant",
                      json_body={"token": token, "appCode": APP_CODE, "type": 0},
                      headers=auth_h, timeout=TIMEOUT).json()
    if r.get("status") != 0:
        log(f"  获取授权码失败: {r.get('msg', '未知')}")
        return None, None
    code = r.get("data", {}).get("code", "")
    if not code:
        return None, None
    r = http_post(f"{SK_WEB}/user/auth/generate_cred_by_code",
                      json_body={"kind": 1, "code": code}, headers=auth_h, timeout=TIMEOUT).json()
    if r.get("code") != 0:
        log(f"  获取 cred 失败: {r.get('message', '未知')}")
        return None, None
    data = r.get("data", {})
    return data.get("cred", ""), data.get("token", "")


# ---------------------------------------------------------------- 签到 API


def get_bindings(cred, sign_token):
    return signed_request(cred, sign_token, f"{SK_API}/game/player/binding", method="GET")


def sign_attendance(cred, sign_token, uid, game_id):
    return signed_request(cred, sign_token, f"{SK_API}/game/attendance",
                          body={"uid": str(uid), "gameId": str(game_id)})


def sign_endfield(cred, sign_token, role_id, server_id):
    return signed_request(cred, sign_token, f"{SK_API}/game/endfield/attendance",
                          profile="endfield",
                          extra_headers={"sk-game-role": f"3_{role_id}_{server_id}"})


def get_attendance_calendar(cred, sign_token, role_id, server_id):
    """GET 终末地签到日历 → (已签天数, 总天数, 明日奖励名, 明日奖励数)；失败返回 None。"""
    try:
        r = signed_request(
            cred, sign_token, f"{SK_WEB}/game/endfield/attendance",
            method="GET", profile="endfield",
            extra_headers={"sk-game-role": f"3_{role_id}_{server_id}"},
        )
    except RequestException as exc:
        log(f"  签到日历查询失败: {exc}")
        return None
    if r.get("code") != 0:
        return None
    data = r.get("data") or {}
    calendar = data.get("calendar") or []
    if not calendar:
        return None
    info_map = data.get("resourceInfoMap") or {}
    done = sum(1 for c in calendar if c.get("done"))
    tomorrow = next((info_map.get(d.get("awardId", "")) or {}
                     for d in calendar if not d.get("done")), {})
    return done, len(calendar), tomorrow.get("name"), tomorrow.get("count")


def sign_checkin(cred, sign_token, game_id):
    return signed_request(cred, sign_token, f"{SK_API}/score/checkin", body={"gameId": str(game_id)})


# ---------------------------------------------------------------- 结果与通知


def _award_name_and_count(award, resource_info_map):
    if isinstance(award, dict):
        award_id = str(award.get("id") or award.get("awardId") or award.get("resourceId") or "")
        count = award.get("count") or award.get("num") or award.get("quantity")
    else:
        award_id, count = str(award), None
    info = resource_info_map.get(award_id)
    if not info and award_id.isdigit():
        info = resource_info_map.get(int(award_id))
    info = info if isinstance(info, dict) else {}
    name = info.get("name") or info.get("resource", {}).get("name") or "未知奖励"
    if count is None:
        count = info.get("count") or info.get("num") or info.get("quantity") or 1
    return name, count


def parse_awards(result):
    """签到响应 → [(名称, 数量), ...]，兼容 awards 与 awardIds+resourceInfoMap 两种返回结构。"""
    data = result.get("data") or {}
    if data.get("awards"):
        return [(a.get("resource", {}).get("name", "未知奖励"), a.get("count", 0))
                for a in data["awards"] if isinstance(a, dict)]
    award_ids = data.get("awardIds") or []
    resource_map = data.get("resourceInfoMap") or {}
    if award_ids and resource_map:
        return [_award_name_and_count(item, resource_map) for item in award_ids]
    return []


def fmt_awards(pairs):
    return "，".join(f"「{n}」{c}个" for n, c in pairs if n)


def mask_nickname(name):
    return (name[0] + "*" * (len(name) - 1)) if name and len(name) > 1 else (name or "*")


INVISIBLE_CHARS = {"ㅤ", "​", "﻿", " "}


def build_role_label(app_name, channel, name, level=None):
    """【游戏名】渠道角色 昵** lv.等级；昵称全为不可见字符时省略昵称。"""
    visible = "".join(ch for ch in (name or "") if ch not in INVISIBLE_CHARS).strip()
    nickname = f" {mask_nickname(visible)}" if visible else ""
    return f"【{app_name}】{channel}角色{nickname}" + (f" lv.{level}" if level else "")


def handle_result(result, label, to_file=False, checkin=False):
    """处理单次签到响应：写日志 + 汇总行。"""
    code = result.get("code", -1)
    verb = "检票" if checkin else "签到"
    if code == 0:
        awards = fmt_awards(parse_awards(result))
        line = f"{label} {verb}成功" + (f"，获得了{awards}" if awards else "")
    elif code == 10001:
        line = f"{label} 今天已经{verb}过了"
    else:
        line = f"{label} {verb}失败（{result.get('message', '')}，code={code}）"
    log(f" {line}", to_file)


def process_game(cred, sign_token, game, to_file):
    app_code = game.get("appCode", "")
    app_name = game.get("appName", "")
    log(f"  游戏: {app_name} ({app_code})", to_file)
    for binding in game.get("bindingList", []):
        uid = binding.get("uid", "")
        gid = binding.get("channelMasterId", "")
        nick = binding.get("nickName", uid)
        channel = binding.get("channelName", "")
        if not uid or not gid:
            continue
        if app_code == "endfield":
            roles = binding.get("roles") or ([binding["defaultRole"]] if binding.get("defaultRole") else [])
            for role in roles:
                role_id, server_id = role.get("roleId", ""), role.get("serverId", "")
                if role_id and server_id:
                    label = build_role_label(app_name, channel,
                                             role.get("nickname") or nick or uid, role.get("level"))
                    handle_result(sign_endfield(cred, sign_token, role_id, server_id), label, to_file)
                    cal = get_attendance_calendar(cred, sign_token, role_id, server_id)
                    if cal:
                        done, total, tm_name, tm_count = cal
                        line = f"【签到日历】{app_name} 本月已签 {done}/{total} 天"
                        if tm_name:
                            line += f" · 明日可得「{tm_name}」{tm_count or 1}个"
                        log(f" {line}", to_file)
        else:
            label = build_role_label(app_name, channel, nick)
            handle_result(sign_attendance(cred, sign_token, uid, gid), label, to_file)
    if app_code in CHECKIN_MAP:
        handle_result(sign_checkin(cred, sign_token, CHECKIN_MAP[app_code]),
                      f"【登岛检票】{app_name}", to_file, checkin=True)


# ---------------------------------------------------------------- 主流程


def resolve_token(token, phone, password, to_file):
    """统一返回 (token, cred, sign_token)，失败时 cred 为空。"""
    if not token:
        if not (phone and password):
            return None, None, None
        log("  密码登录模式", to_file)
        token = login_by_password(phone, password, to_file)
        if not token:
            return None, None, None
        save_new_token(token, to_file=to_file)

    cred, sign_token = get_cred_and_sign_token(token)
    if not cred and phone and password:
        log("  token 失效，尝试密码登录续期...", to_file)
        new_token = login_by_password(phone, password, to_file)
        if new_token:
            save_new_token(new_token, old_token=token, to_file=to_file)
            token = new_token
            cred, sign_token = get_cred_and_sign_token(token)
    return token, cred, sign_token


def main():
    sync_time_offset()
    config = load_config()
    to_file = config.get("log_to_file", True)
    game_filter = set(config.get("games", []))
    phone, password = load_account_creds(config)
    tokens = load_tokens()

    if not tokens and not (phone and password):
        log("未找到 token：请在 creds.txt 填入，或配置 SKLAND_TOKENS / SKLAND_PHONE + SKLAND_PASSWORD", to_file)
        sys.exit(1)

    accounts = tokens or [None]  # 无 token 且配了密码 → 纯密码模式
    log(f"共 {len(accounts)} 个账号，开始签到...\n", to_file)
    for idx, token in enumerate(accounts, 1):
        mask = f"{token[:8]}****{token[-4:]}" if token else "密码登录"
        log(f"--- 账号 {idx} ({mask}) ---", to_file)

        _, cred, sign_token = resolve_token(token, phone, password, to_file)
        if not cred:
            log("  跳过（登录失败或 token 失效且未配置密码续期）\n", to_file)
            continue
        log(f"  cred 获取成功: {cred[:8]}****{cred[-4:]}", to_file)

        bindings = get_bindings(cred, sign_token)
        if bindings.get("code") != 0:
            log(f"  获取绑定列表失败: {bindings.get('message', '')}\n", to_file)
            continue
        games = bindings.get("data", {}).get("list", [])
        if game_filter:
            games = [g for g in games if g.get("appCode", "") in game_filter]
        if not games:
            log("  未绑定任何游戏\n", to_file)
            continue
        for game in games:
            process_game(cred, sign_token, game, to_file)

    log("完成！", to_file)


if __name__ == "__main__":
    main()

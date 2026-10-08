#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AppShare（App 分享 / app.sharess.cn）每日签到 —— 经验 + 分享值（连续签到升级奖励档）
协议逆向自官方 App 6.0.0.alpha.1（2026-09-26 实测验证）：

  签到   GET user/v1/daySign?token=T&sign=大写MD5(T + 北京分钟YYYYMMDDHHMM)
  登录   GET user/login/v1/accountLogin?token=T&account=A&password=大写MD5(明文)&androidId=I
              &sign=MD5(T + A + PWD + I + 分钟)   → code 100 时 data 即新 token
  防伪头 verify-sign-v2 = 大写 HMAC-SHA256("appshare_verify_hmac_v1", "GET\\n路径\\nsign\\nnonce")
  容忍   服务端按自身时钟 ±1 分钟校验（脚本自动重试 -1/0/+1）
  失效   code 300 "用户不存在" = token 已被顶掉/清理

环境变量：
  APPSHARE_TOKEN     抓包 token（优先使用，直签）
  APPSHARE_ACCOUNT   账号（用户名或邮箱；token 失效或未配 token 时用它账密登录换新）
  APPSHARE_PASSWORD  明文密码（同上，需先在 App「账号安全」设置密码）
  APPSHARE_ANDROID_ID 设备ID（可选，默认独立值避免与手机互踢）

零依赖：纯 Python 标准库。
"""
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://app.sharess.cn"
HMAC_KEY = b"appshare_verify_hmac_v1"
UA = "okhttp/5.5.0"
# App 6.0.0 请求头常量（ad 系/经验接口硬校验，9-29 实测）
AD_SECRET = "9ppbhEjOAMN1RBpNGzS7KafcLC7Xyc3wHgjrKZemSg=="
BEIJING = timezone(timedelta(hours=8))
DEFAULT_ANDROID_ID = "6175746f30303030"

# 持久目录：云效流水线把 /root/.cache 列为默认缓存目录（跨次运行持久），
# 登录换来的 token 写进去，下次运行直接复用，token 失效才再账密登录。
# 注意：基础镜像里该目录可能不存在，必须主动创建（否则缓存步骤 skip）
_CACHE_ROOT = "/root/.cache"
try:
    os.makedirs(_CACHE_ROOT, exist_ok=True)
except OSError:
    pass
PERSIST_DIR = _CACHE_ROOT if os.access(_CACHE_ROOT, os.W_OK) \
    else os.path.dirname(os.path.abspath(__file__))
TOKEN_CACHE = os.path.join(PERSIST_DIR, "appshare_token.cache")


def load_cached_token():
    try:
        return open(TOKEN_CACHE, encoding="utf-8").read().strip() or None
    except (OSError, ValueError):
        return None


def save_cached_token(token):
    try:
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            f.write(token)
    except OSError:
        pass


def _md5u(s):
    return hashlib.md5(s.encode()).hexdigest().upper()


def _beijing_minute(skew=0):
    # 云效容器时区不定，固定按北京时间算
    now = datetime.now(BEIJING)
    if skew:
        now += timedelta(minutes=skew)
    return now.strftime("%Y%m%d%H%M")


def api_get(path, params, minute_skew=0, pre_sign=None):
    """发一次带全防伪头的 GET，返回解析后的 JSON dict。
    pre_sign：调用方预计算签名（部分接口 sign 基于逻辑参数序而非 query 序）。"""
    if pre_sign is not None:
        sign = pre_sign
    else:
        concat = "".join(str(v) for v in params.values() if str(v)) + _beijing_minute(minute_skew)
        sign = _md5u(concat)
    nonce = secrets.token_hex(16).upper()
    vsig = hmac.new(HMAC_KEY, "GET\n{}\n{}\n{}".format(path, sign, nonce).encode(),
                    hashlib.sha256).hexdigest().upper()
    qs = "&".join("{}={}".format(k, v) for k, v in params.items() if v is not None) + "&sign=" + sign
    req = urllib.request.Request(
        "{}/{}?{}".format(BASE, path, qs),
        headers={
            "User-Agent": UA,
            "accept-encoding": "gzip",
            "device": "V2338A",
            "buildtime": "1790486928",
            "devicesdk": "35",
            "versioncode": AD_SECRET,
            "verify-version": "hmac-v1",
            "verify-nonce": nonce,
            "verify-sign-v2": vsig,
            "api_sign": "{}:{}:{}".format(path, sign, int(datetime.now().timestamp() * 1000)),
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except Exception as e:
        print('[RAW][appshare] 网络异常: {}'.format(e))
        return {"code": -1, "message": f"网络错误: {e}"}
    try:
        j = json.loads(raw.decode("utf-8"))
        print('[RAW][appshare] {}'.format(raw[:400].decode("utf-8", "replace")))
        return j
    except ValueError:
        print('[RAW][appshare] 非JSON响应: {}'.format(raw[:200].decode("utf-8", "replace")))
        return {"code": -1, "message": "响应非JSON: " + raw[:100].decode("utf-8", "replace")}


def api_call(path, params):
    """带 ±1 分钟容错的调用。返回 (json, 是否通过签名层)。"""
    for skew in (0, -1, 1):
        j = api_get(path, params, minute_skew=skew)
        msg = j.get("message", "")
        if "系统时间" not in msg:
            return j, True
    return j, False


def account_login(account, password, android_id):
    """账密登录，成功返回新 token，失败返回 None + 原因。
    注意：登录接口的成功码是 200（与 daySign 的 100 不同），且错误信息也在 data 字段——
    按 token 特征（32~64 位十六进制）判定成功，不赌 code 值。"""
    pwd = _md5u(password)
    j, ok = api_call("user/login/v1/accountLogin",
                     {"token": "", "account": account, "password": pwd, "androidId": android_id})
    if not ok:
        return None, "签名校验失败: " + str(j.get("message"))
    data = j.get("data")
    if isinstance(data, str) and re.fullmatch(r"[0-9a-fA-F]{32,64}", data.strip()):
        return data.strip(), None
    reason = j.get("message") or (data if isinstance(data, str) else None) \
        or "登录失败(code={})".format(j.get("code"))
    return None, reason[:120]


def fetch_experience(token):
    """拉今日经验（/user/level/v1/experienceLogs）。sign=MD5(token+p+分钟)，需全请求头。
    拼接序实锤（10-01 jadx 反编译 UserRequest.experienceLogs + tk3 签名函数）：
    App 调用形参 {token, p} 按序 append → token 在前 p 在后；之前 p+token 顺序
    反了导致服务端"验证失败，请检查系统时间"必挂（与风控无关，从未成功过）。
    返回今日签到经验 int 或 None。"""
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    for page in (1, 2):
        for skew in (0, -1, 1, -2, 2):  # 分钟容错(服务端时钟边界波动)
            sign = _md5u(token + str(page) + _beijing_minute(skew))
            j = api_get("user/level/v1/experienceLogs",
                        {"token": token, "p": page}, pre_sign=sign)
            if "系统时间" not in (j.get("message") or ""):
                break
        else:
            return None
        if j.get("code") != 100 or not isinstance(j.get("data"), list):
            return None
        for it in j["data"]:
            if (it.get("title") == "签到" and it.get("time") and
                    datetime.fromtimestamp(it["time"], BEIJING).strftime("%Y-%m-%d") == today):
                return it.get("count")
    return None


def day_sign(token):
    """执行签到。返回 (状态, 文本)：状态 in ok/already/dead/err。"""
    j, ok = api_call("user/v1/daySign", {"token": token})
    if not ok:
        return "err", "签名校验失败: " + str(j.get("message"))
    code, msg, data = j.get("code"), j.get("message", ""), j.get("data")
    if code == 100 and isinstance(data, dict):
        # 真实响应结构（2026-09-27 首签实测）：
        #   count: "分享值 +15 ，积分 +0"            ← 汇总
        #   list:  [{name: "日常签到获取", count: "+5 分享值"}, ...] ← 明细
        #   signConseDays: 10, nextConseDay: 15, nextConseApsc: 10  ← 连签
        #   videoRewardText: 看广告诱导文案，从未入账，不进通知
        lines = []
        total = str(data.get("count", "")).strip()
        if total:
            lines.append(f"签到成功：{total}")
        for it in data.get("list") or []:
            if isinstance(it, dict):
                nm = str(it.get("name", "")).strip()
                ct = str(it.get("count", "")).strip()
                if nm or ct:
                    lines.append("{} {}".format(nm, ct))
        conse = data.get("signConseDays")
        if conse is not None:
            nd, na = data.get("nextConseDay"), data.get("nextConseApsc")
            lines.append("连签 {} 天{}".format(
                conse, "，下档 {} 天 +{} 分享值".format(nd, na) if nd is not None else ""))
        return "ok", "\n".join(lines) if lines else "签到成功"
    if code == 200 and "已签" in msg:
        return "already", "今日已签到"
    if code == 300 or "用户不存在" in msg:
        return "dead", f"token已失效({msg})"
    return "err", "code={} {}".format(code, msg)


def run():
    """主入口，打印结果（main.py 经 subprocess 读 stdout 聚合）。"""
    account = os.environ.get("APPSHARE_ACCOUNT", "").strip()
    password = os.environ.get("APPSHARE_PASSWORD", "").strip()
    token = os.environ.get("APPSHARE_TOKEN", "").strip()
    android_id = os.environ.get("APPSHARE_ANDROID_ID", "").strip() or DEFAULT_ANDROID_ID

    lines = []
    token = token or load_cached_token()
    if not account and not token:
        print("AppShare: 未配置 APPSHARE_TOKEN 或 APPSHARE_ACCOUNT/PASSWORD，跳过")
        return

    # token 优先级：环境变量显式 token > 上次运行缓存的 token
    # 有 token 直接签；失效(code 300)且有账密才登录换新并写缓存
    final_token = ""
    if token:
        final_token = token
        status, text = day_sign(token)
        if status == "dead" and account and password:
            new_token, err = account_login(account, password, android_id)
            if new_token:
                save_cached_token(new_token)
                lines.append("token失效，账密自动续期成功")
                final_token = new_token
                status, text = day_sign(new_token)
            else:
                status, text = "err", f"token失效且续期失败: {err}"
    elif account and password:
        new_token, err = account_login(account, password, android_id)
        if new_token:
            save_cached_token(new_token)
            final_token = new_token
            status, text = day_sign(new_token)
        else:
            status, text = "err", f"自动登录失败: {err}"
    else:
        status, text = "err", "无可用token"
    if final_token and status in ("ok", "already"):
        exp = fetch_experience(final_token)
        if exp is not None:
            lines.append(f"经验 +{exp}（今日签到）")
    lines.append(text)
    print("AppShare 签到:\n" + "\n".join(lines))


if __name__ == "__main__":
    print("---------- AppShare 开始签到 ----------")
    try:
        run()
    except Exception:
        import traceback
        print("AppShare 签到:\n模块异常（完整栈）:\n" + traceback.format_exc())
    print("---------- AppShare 签到完毕 ----------")

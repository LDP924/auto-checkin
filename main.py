#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一签到调度入口：森空岛 + 夸克网盘 + AppShare + QQ音乐 + 音贝任务
依次运行五个模块（各自独立完整、可单跑），聚合输出，Server酱/WeCom 推送。

推送标题直接显示完成度「自动签到完成 N/5」，聊天列表卡片一眼看完不用点开：
  森空岛   成功 = 签到成功 且 检票成功（当天已签/已检票同样算成功）
  夸克网盘 成功 = 签到领空间成功
  AppShare 成功 = 签到完成（含"今日已签到"）
  QQ音乐   成功 = 输出成长值合计行（今日已签幂等同样算成功）
  音贝任务 成功 = 输出音贝新增行（额度满额幂等同样算成功）
  失败模块在标题点名，如「自动签到完成 4/5 · 失败:夸克网盘」

正文为清洗后的结果明细（去时间戳/过程日志，只留每项所得），markdown 分段。

环境变量总表（缺哪个就跳过哪个模块）：
  森空岛   SKLAND_PHONE + SKLAND_PASSWORD（密码登录，token 失效自动续）
           SKLAND_DID（数美设备指纹，防 CI 机房 IP 风控）
  夸克网盘 COOKIE_QUARK
  AppShare APPSHARE_ACCOUNT + APPSHARE_PASSWORD（推荐，自动续token）
           / APPSHARE_TOKEN（直签模式）
  QQ音乐   QM_KEY + QM_UIN（音贝任务共用同一凭证）
  推送     SERVERCHAN_SENDKEY / WECOM_CORP_ID + WECOM_SECRET + WECOM_AGENT_ID
           （+ WECOM_TOUSER 可选定向）

阿里云效流水线：每日定时 → 命令 `python main.py` → 环境变量配在上面。
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

MODULES = ["skland_sign.py", "quark_sign.py", "appshare_sign.py",
           "qqmusic_sign.py", "yinbei_sign.py"]
TIMEOUT = 300  # 单模块最长 5 分钟

NAMES = {"skland_sign.py": "森空岛", "quark_sign.py": "夸克网盘", "appshare_sign.py": "AppShare",
         "qqmusic_sign.py": "QQ音乐", "yinbei_sign.py": "音贝任务"}

# 森空岛模块的过程日志行（推送时过滤，只留结果）
SKLAND_NOISE = (
    "密码登录模式", "使用预配置 dId", "新 token 已写入", "cred 获取成功",
    "游戏: ", "完成！", "开始签到", "token 失效", "跳过（登录失败",
)


def run_all(scripts):
    """五模块并发运行（subprocess 池），完成后按固定顺序聚合输出。
    总时长 = 最慢单模块（QQ音乐 30005 等待 60s + 冷却间隔，约 2 分钟内），远小于串行之和。"""
    procs = {}
    for script in scripts:
        procs[script] = subprocess.Popen(
            [sys.executable, script], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)))
    outs = {}
    for script, p in procs.items():
        try:
            out, err = p.communicate(timeout=TIMEOUT)
            out = (out or "").strip()
            if p.returncode != 0 and not out:
                out = "{} 运行异常: {}".format(script, (err or "").strip()[:200])
            elif err and err.strip():
                out += "\n[stderr] " + err.strip()[:200]
        except subprocess.TimeoutExpired:
            p.kill()
            out = "{} 运行异常: 超时（>{}秒）".format(script, TIMEOUT)
        outs[script] = out
        print(out + "\n")  # 全量进云效构建日志（含 RAW）
    return [outs[s] for s in scripts]


def judge(script, out):
    """按各模块输出判定成败。返回 True/False/None（None=跳过未配置）。"""
    if "跳过" in out or "未找到 token" in out:
        return None
    if script == "skland_sign.py":
        # (签到成功 或 今天已签) 且 (检票成功 或 今天已检票)，且无失败
        signed = ("签到成功" in out or "已经签到" in out) and "签到失败" not in out
        checked = ("检票成功" in out or "已经检票" in out) and "检票失败" not in out
        return signed and checked and "❌" not in out and "获取绑定列表失败" not in out
    if script == "quark_sign.py":
        return "❌" not in out and ("✅" in out or "已签到" in out)
    if script == "appshare_sign.py":
        return ("签到成功" in out or "今日已签到" in out) and "❌" not in out \
            and "自动登录失败" not in out and "无可用token" not in out
    if script == "qqmusic_sign.py":
        # 输出汇总行（输出汇总行）即成功
        return "[QQ音乐]" in out and "运行失败" not in out
    if script == "yinbei_sign.py":
        return "[音贝] 本次新增:" in out and "运行失败" not in out
    return None


def clean_section(script, out):
    """清洗模块输出：去时间戳、banner、模块自带标题行、过程日志、内容层状态emoji。
    ✅/❌ 只出现在段落标题（服务级状态）。"""
    lines = []
    for ln in out.splitlines():
        ln = re.sub(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?\]?\s*", "", ln.strip())
        if ln.startswith('[RAW[') or ln.startswith('[RAW]'):
            continue  # 原始响应日志只进云效构建日志，不进推送正文
        ln = ln.replace("✅", "").replace("❌", "").replace("⏭", "")
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        if not ln or ln.startswith("----------"):
            continue
        # 模块自带标题行（与段落标题"✅ 服务名"重复）
        if ln.rstrip("：:").rstrip() in ("AppShare 签到", "夸克网盘签到", "AppShare", "夸克网盘"):
            continue
        # 账号分隔线（如 "--- 账号 1 (密码登录) ---"），非签到结果
        if ln.startswith("---") and ln.endswith("---"):
            continue
        if script == "skland_sign.py" and any(ln.startswith(n) or n in ln[:20] for n in SKLAND_NOISE):
            continue
        if script == "qqmusic_sign.py" and ln.startswith("[状态]"):
            continue  # 执行前快照（含跨天残留脏值），进通知易误读"未签到"
        if ln.startswith("[续期]") and script in ("qqmusic_sign.py", "yinbei_sign.py"):
            continue  # 令牌续期过程日志
        lines.append(ln)
    return "\n".join(lines)


def push_serverchan(sendkey, title, text):
    """Server酱 Turbo 推送（sendkey 或完整 URL 均可，正文 markdown）。免费 5 条/天。"""
    url = sendkey if sendkey.startswith("http") else \
        "https://sctapi.ftqq.com/{}.send".format(sendkey)
    try:
        req = urllib.request.Request(url, data=json.dumps({
            "title": title, "desp": text[:32 * 1024]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
            print("[Server酱] " + ("成功" if r.get("code") == 0 else str(r.get("message"))))
    except Exception as e:
        print("[Server酱] 失败: {}".format(e))


def push_wecom_school(corp_id, secret, agent_id, touser, title, digest, html):
    """WeCom 家校通道（学校通知）mpnews 推送：卡片=标题+两行摘要，正文=腾讯托管 HTML 页。
    两步调用：GET gettoken 换 access_token（2小时有效）→ POST externalcontact/message/send。
    家校通道 thumb 可省；额度账号×200人次/天，远超个人用量。"""
    try:
        token_url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={}&corpsecret={}" \
            .format(urllib.parse.quote(corp_id), urllib.parse.quote(secret))
        with urllib.request.urlopen(
                urllib.request.Request(token_url, headers={"User-Agent": "python-sign"}),
                timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        if j.get("errcode") != 0:
            print("[WeCom] token获取失败: {} {}".format(j.get("errcode"), j.get("errmsg")))
            return
        payload = {
            "agentid": agent_id,
            "msgtype": "mpnews",
            "mpnews": {"articles": [{
                "title": title[:42],            # ≤128 字节（中文≈42字）
                "author": "自动签到",             # 作者行（卡片/正文页作者位）
                "digest": digest[:170],         # ≤512 字节（\n 折两行）
                "content": html[:650 * 1024],   # ≤666KB
            }]},
        }
        if touser:
            payload["to_parent_userid"] = [touser]
        else:  # 未指定收件人 → 全体家长广播
            payload["recv_scope"] = 0
            payload["toall"] = 1
        req = urllib.request.Request(
            "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/message/send?access_token={}"
            .format(j["access_token"]),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("errcode") == 0:
            print("[WeCom] 成功")
        else:
            print("[WeCom] 失败: {} {}".format(r.get("errcode"), r.get("errmsg")))
    except Exception as e:
        print("[WeCom] 失败: {}".format(e))


def parts_to_html(parts):
    """markdown 分段（### 标题 + 正文）转简单 HTML（mpnews 正文用）。"""
    out = []
    for sec in parts:
        lines = sec.splitlines()
        if lines and lines[0].startswith("### "):
            out.append("<h3>" + html.escape(lines[0][4:]) + "</h3>")
            lines = lines[1:]
        for ln in lines:
            ln = ln.strip()
            if ln:
                out.append("<p>" + html.escape(ln) + "</p>")
    return "".join(out)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    scripts = [m for m in MODULES if os.path.exists(m)]
    sections = run_all(scripts)

    # 展示顺序按模块文件名字母序：AppShare → QQ音乐 → 夸克网盘 → 森空岛 → 音贝任务
    results = sorted([(s, o, judge(s, o)) for s, o in zip(scripts, sections)],
                     key=lambda r: r[0])

    total = len(results)
    ok_list = [NAMES[s] for s, _, r in results if r is True]
    fail_list = [NAMES[s] for s, _, r in results if r is False]

    title = "自动签到完成 {}/{}".format(len(ok_list), total)
    if fail_list:
        title += " · 失败:{}".format("、".join(fail_list))
    now_full = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")  # 腾讯正文页只显示到日，秒数自己带

    # 推送正文：按模块分段（服务名下直接是内容），成功 ✅ 失败 ❌ 跳过 ⏭
    parts = []
    status_line = []
    for script, out, r in results:
        icon = "✅" if r is True else ("⏭" if r is None else "❌")
        status_line.append("{} {}".format(icon, NAMES[script]))
        body = clean_section(script, out) or "（无输出）"
        parts.append("### {} {}:\n{}".format(icon, NAMES[script], body))

    # 双通道推送：Server酱 + WeCom 家校（各自独立，一个失败不影响另一个）
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if sendkey:
        push_serverchan(sendkey, title, now_full + "\n\n" + "\n\n".join(parts))

    corp_id = os.environ.get("WECOM_CORP_ID", "").strip()
    secret = os.environ.get("WECOM_SECRET", "").strip()
    agent_id = (os.environ.get("WECOM_AGENT_ID", "") or "0").strip()
    if secret and corp_id and agent_id != "0":
        push_wecom_school(
            corp_id, secret, int(agent_id),
            os.environ.get("WECOM_TOUSER", "").strip(),
            title, "{}\n{}".format("\n".join(status_line), now_full),
            "<p><strong>{}</strong></p>".format(now_full) + parts_to_html(parts))

    print(title)
    sys.exit(0 if not fail_list and total > 0 else 1)


if __name__ == "__main__":
    main()

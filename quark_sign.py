#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘每日签到（领取空间），V2 移动端接口（kps/sign/vcode 抓包自抽奖页 URL）。

环境变量 COOKIE_QUARK（多账号换行或 && 分隔）：
  user=张三; url=https://drive-m.quark.cn/1/clouddrive/act/growth/reward?kps=xxx&sign=xxx&vcode=xxx
  或旧格式 user=张三; kps=xxx; sign=xxx; vcode=xxx
抓包：手机访问夸克网盘抽奖页 → 找 growth/reward 请求 → 复制整段 URL
"""
import json
import os
import re
import urllib.parse
import urllib.request


def _http_json(url, data=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    print('[RAW][quark] {}'.format(raw[:500]))
    return json.loads(raw)


class Quark:
    def __init__(self, user_data):
        self.param = user_data

    @staticmethod
    def convert_bytes(b):
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        i = 0
        while b >= 1024 and i < len(units) - 1:
            b /= 1024
            i += 1
        return "{:.2f} {}".format(b, units[i])

    def _qs(self):
        return urllib.parse.urlencode({
            "pr": "ucpro", "fr": "android",
            "kps": self.param.get("kps"), "sign": self.param.get("sign"),
            "vcode": self.param.get("vcode")})

    def get_growth_info(self):
        r = _http_json("https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?" + self._qs())
        return r.get("data") or False

    def get_growth_sign(self):
        r = _http_json("https://drive-m.quark.cn/1/clouddrive/capacity/growth/sign?" + self._qs(),
                       data={"sign_cyclic": True})
        if r.get("data"):
            return True, r["data"]["sign_daily_reward"]
        return False, r.get("message", "未知错误")

    def do_sign(self):
        info = self.get_growth_info()
        if not info:
            return "❌ 获取成长信息失败（凭证可能过期，重新抓包）"
        vip = "88VIP" if info.get("88VIP") else "普通用户"
        sign_cap = info["cap_composition"].get("sign_reward", 0)
        log = " {} {}\n💾 总容量 {}，签到累计 {}".format(
            vip, self.param.get("user", ""),
            self.convert_bytes(info["total_capacity"]), self.convert_bytes(sign_cap))
        cap = info["cap_sign"]
        if cap["sign_daily"]:
            log += "\n✅ 今日已签到（{} 已计入容量），连签({}/{})".format(
                self.convert_bytes(cap["sign_daily_reward"]),
                cap["sign_progress"], cap["sign_target"])
        else:
            ok, ret = self.get_growth_sign()
            if ok:
                log += "\n✅ 执行签到+{}，连签({}/{})".format(
                    self.convert_bytes(ret), cap["sign_progress"] + 1, cap["sign_target"])
            else:
                log += "\n❌ 签到异常: {}".format(ret)
        return log


def extract_params(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {"kps": q.get("kps", [""])[0], "sign": q.get("sign", [""])[0], "vcode": q.get("vcode", [""])[0]}


def run():
    raw = os.environ.get("COOKIE_QUARK", "")
    if not raw:
        out = "夸克网盘: 未配置 COOKIE_QUARK，跳过"
        print(out)
        return out
    accounts = re.split(r"\n|&&", raw)
    lines = ["共 {} 个夸克网盘账号".format(len(accounts))]
    for i, acc in enumerate(accounts, 1):
        user_data = {}
        for kv in acc.replace(" ", "").split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                user_data[k] = v
        if "url" in user_data:
            user_data.update(extract_params(user_data["url"]))
        lines.append("🙍 账号{}: {}".format(i, Quark(user_data).do_sign()))
    out = "夸克网盘签到:\n" + "\n".join(lines)
    print(out)
    return out


if __name__ == "__main__":
    print("---------- 夸克网盘开始签到 ----------")
    run()
    print("---------- 夸克网盘签到完毕 ----------")

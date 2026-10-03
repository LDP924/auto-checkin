#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ音乐系模块公共底座：u6 域协议、zzc 签名、令牌续期与缓存。

被 qqmusic_sign.py 与 yinbei_sign.py 共用（两者同凭证：QM_KEY/QM_UIN）。

环境变量：
  QM_UA        业务接口 UA（抓包你的完整 UA；不配则用通用精简 UA）
  QM_CACHE_DIR key 缓存目录（默认 /root/.cache，CI 缓存可持久化）

续期机制：qm_keyst 72h，经 QQConnectLogin.LoginServer.QQLogin 换新 key
（旧 key 不吊销，可无限续），新 key 写缓存文件，环境变量只需配置一次。
二级救活（2026-10-01 实测）：musickey 过期后（refresh 返 code:1000）可用
QQ 互联凭证 openid+access_token 走同一 QQLogin 接口换新（param 带上两者
即可）；access_token 有效期 90 天。续期成功时把 openid/access_token 一起
写缓存（第二行 JSON），过期后自动用它救活，CI 缓存丢失也不怕。
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from base64 import b64encode
from hashlib import sha1

API = 'https://u6.y.qq.com/cgi-bin/musics.fcg'
UA = os.environ.get('QM_UA') or 'QQMusic 20090008(android 15)'
CACHE_DIR = os.environ.get('QM_CACHE_DIR') or '/root/.cache'


def time33(s):
    h = 5381
    for c in s:
        h += (h << 5) + ord(c)
        h &= 0xFFFFFFFF
    return h & 0x7FFFFFFF


def zzc(payload):
    """u6 域请求签名（sha1 重组，同 @jixunmoe/qmweb-sign 算法）。"""
    h = sha1(payload.encode()).hexdigest().upper()
    p1 = ''.join(h[i] for i in (23, 14, 6, 36, 16, 7, 19))
    p2 = ''.join(h[i] for i in (16, 1, 32, 12, 19, 27, 8, 5))
    p3 = bytes(v ^ int(h[i * 2:i * 2 + 2], 16) for i, v in enumerate(
        (89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242,
         133, 143, 161, 121, 179)))
    return 'zzc{}{}{}'.format(p1, re.sub(rb'[\\/+=]', b'', b64encode(p3)).decode(), p2).lower()


class QMClient:
    """u6 域 musics.fcg 客户端：cookie/续期/通用调用。"""

    def __init__(self, key, uin, cache_prefix):
        self.uin = uin
        self.cache = os.path.join(CACHE_DIR, '{}_{}.txt'.format(cache_prefix, uin))
        self.key = self._load_or(key)

    def _load_or(self, env_key):
        """缓存 key（上次续期产物）优先，环境变量兜底。
        缓存文件两行：第一行 musickey；第二行可选 JSON（QQ 互联救活凭证）。"""
        try:
            if os.path.exists(self.cache):
                lines = open(self.cache).read().splitlines()
                if lines and lines[0].strip().startswith('Q_H_L_'):
                    self._rescue = self._parse_rescue(lines[1] if len(lines) > 1 else '')
                    return lines[0].strip()
        except OSError:
            pass
        self._rescue = {}
        return env_key

    @staticmethod
    def _parse_rescue(line):
        try:
            d = json.loads(line)
            if d.get('openid') and d.get('access_token'):
                return d
        except (ValueError, TypeError):
            pass
        return {}

    def _save_cache(self, extra):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            open(self.cache, 'w').write(self.key + ('\n' + extra if extra else ''))
        except OSError:
            pass

    def _cookie(self):
        # 最小 Cookie：三键同值 + uin，其余设备/统计段服务端不校验
        return 'qm_keyst={k}; qqmusic_key={k}; p_lskey={k}; uin=o{u}'.format(
            k=self.key, u=self.uin)

    def _post(self, body, sign):
        url = '{}?_={}&sign={}'.format(API, int(time.time() * 1000), sign)
        req = urllib.request.Request(url, data=body.encode(), headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': self._cookie(), 'Referer': 'https://y.qq.com/',
            'Origin': 'https://y.qq.com', 'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
        print('[RAW][musicu] {}'.format(raw[:600]))
        return json.loads(raw)

    def api(self, module, method, param, comm=None, tag='musicu'):
        body = json.dumps({
            'comm': comm or {'g_tk': time33(self.key), 'uin': int(self.uin),
                             'format': 'json', 'inCharset': 'utf-8',
                             'outCharset': 'utf-8', 'notice': 0, 'platform': 'h5',
                             'needNewCode': 1, 'ct': 23, 'cv': 0},
            'req_0': {'module': module, 'method': method, 'param': param}},
            separators=(',', ':'))
        d = self._post(body, zzc(body)).get('req_0', {}) or {}
        if d.get('code') == 2000:
            raise RuntimeError('登录态失效（2000）：请重新抓包更新 QM_KEY')
        return d

    def refresh(self):
        param = {'expired_in': 7776000, 'musicid': int(self.uin), 'musickey': self.key}
        result = self._qq_login(param)
        new = result.get('musickey', '')
        rescue = {'openid': result.get('openid', ''), 'access_token': result.get('access_token', '')}
        if not new or new == self.key:
            # 一级续期失败（musickey 过期返 code:1000）→ 二级救活：
            # 用缓存的 QQ 互联凭证 openid+access_token 再走一次 QQLogin
            resc = self._rescue
            if resc:
                param2 = dict(param, openid=resc['openid'], access_token=resc['access_token'])
                result = self._qq_login(param2, tag='rescue')
                new = result.get('musickey', '')
                rescue = {'openid': result.get('openid', '') or resc['openid'],
                          'access_token': result.get('access_token', '') or resc['access_token']}
        if new and new != self.key:
            self.key = new
            extra = ''
            if rescue.get('openid') and rescue.get('access_token'):
                extra = json.dumps(rescue, separators=(',', ':'))
            self._save_cache(extra)
            print('[续期] musickey 已刷新并缓存（72h，可无限续）')
        else:
            print('[续期] 未取得新 key，沿用当前 key')

    def _qq_login(self, param, tag='refresh'):
        ds = json.dumps({'req1': {'module': 'QQConnectLogin.LoginServer',
                                  'method': 'QQLogin', 'param': param}},
                        separators=(',', ':'))
        url = '{}?sign={}&format=json&inCharset=utf8&outCharset=utf-8&data={}'.format(
            API, zzc(ds), urllib.parse.quote(ds))
        req = urllib.request.Request(url, headers={
            'Cookie': self._cookie(), 'Referer': 'https://y.qq.com/',
            'Origin': 'https://y.qq.com', 'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
        except Exception as ex:
            print('[RAW][{}] 请求异常: {}'.format(tag, ex))
            return {}
        print('[RAW][{}] {}'.format(tag, raw[:300]))
        return json.loads(raw).get('req1', {}).get('data', {}) or {}

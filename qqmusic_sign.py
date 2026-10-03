#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ音乐会员成长值签到（任务中心五任务，接口实测逆向）。

每日产出（成长值）：签到 15（响应 Total 字段实值） + 挂件 5 + 装扮 5 + 听书 5 + 收听节目 10。

任务协议（music.lvz.LevelConfigSvr）：GetState 查状态 / IssueAward 领奖
(30009=已领) / RecordBehavior 行为上报(仅30) / StartTask 开始(仅20)。
服务端"超级会员专享"仅 App 弹窗拦截，接口层放行。

任务动作语义（2026-10-01 全量重查定稿）：
  四任务全部即做即领，无任何"异步成熟"。
  8 挂件  - UsePendant 当日动作（已佩戴时返 42502，需 Cancel→Use 成对）
  9 装扮  - UseCosmetic 当日动作（动态跟随使用中 state=1 的装扮，不锁定 ID）
  20 节目 - StartTask 挂开始标记 + 播放上报（内置 cmd=2000049 节目模板）
            把状态推到 W
  30 听书 - RecordBehavior(ID:30) 把状态推到 W

领取定律（10-03 终案）：
  20020 = 领取冷却——一次成功发放(code:0)后 ~2 秒内的下一个 IssueAward 被
  拒。撞到等 3s 原地重领一次。
  30005 = 行为未计次，两场景：①上报已受理但池同步延迟（统计埋点通道
  imusic_tj 异步批处理，10-01 实测 9.6s 拒/54s 成）——仅任务20触发60s
  兜底重领（每天仅两轮，不等=+10丢到明天；8/9/30 走秒级通道不陪等）；
  ②上报被统计通道去重丢弃（固定 songid 连续上报第 4-5 次起，10-03 实锤）
  →量永远不足，等再久也无解，重领一次即弃留待下轮。songid 轮换防 ②。
  10006 = 写操作锁——当日 IssueAward 失败累计过多后 StartTask/IssueAward
  顶层被锁（GetState 只读不受影响），次日恢复。勿连撞领取。
  30009 = 已领（含当日已领与跨天前的昨日延续：服务端 20 跨天重置在 0 点后
  滚动执行，00:01 轮所见 20:Y 即昨日残留——昨日单若未完成会被作废关单
  转 Y，奖励蒸发不发，经验明细可辨真领与作废）。

环境变量：
  QM_KEY         qm_keyst 值（Q_H_L_ 开头，抓包任意 y.qq.com 请求的 Cookie）
  QM_UIN         QQ 号（纯数字）
  QM_PLAY_TPL_B64 任务20播放上报模板（base64，可选覆盖，不配用内置）
  （QM_UA / QM_CACHE_DIR 见 qmcommon.py）
"""
import json
import os
import re
import time
import urllib.request
from base64 import b64decode
from gzip import compress as gzip_compress

from qmcommon import QMClient, time33

REPORT_API = 'https://stat.y.qq.com/android/fcgi-bin/imusic_tj'
# App 通道 comm（cosmetic 系接口实测认 ct:11）
APP_COMM = {'ct': '11', 'cv': '20090008'}

# 任务20 (songid, album_id) 轮换池：368182354 的相似有声书集
# （get_simsongs 实测拉取，songid 与 album 配对为真实关系）。
# 同 id 连续上报被统计通道去重（10-03 破案），按日轮换打散。
SONG_POOL = [(368182354, 29145437), (508222545, 29145437),
             (367799432, 29145437), (493099611, 51326893),
             (368182357, 29145437), (270384187, 13210184)]

# 内置任务20行为模板：cmd=2000049 听书/节目播放上报（抓包机 V2338A 真实样本
# 原样内置）。运行时刷新 qq/authst/tid/traceid/optime/sid/str3 时间戳。
# 环境变量 QM_PLAY_TPL_B64 可覆盖（自定义模板优先）。
PLAY_TPL_2000049 = '''<?xml version="1.0" encoding="UTF-8"?><root><fPersonality>0</fPersonality><tmeLoginType>2</tmeLoginType><tmeLoginMethod>2</tmeLoginMethod><OpenUDID>00000000759db7d0000000000033c587</OpenUDID><udid>00000000759db7d0000000000033c587</udid><ct>11</ct><cv>20090008</cv><v>20090008</v><chid>10003505</chid><os_ver>15</os_ver><aid>6f1c8c9bac5e4639</aid><phonetype>V2338A</phonetype><OpenUDID2>00000000759db7d0000001a040bbcdb7</OpenUDID2><devicelevel>50</devicelevel><newdevicelevel>40</newdevicelevel><deviceScore>804.16</deviceScore><QIMEI36>e09203ffdf46f5920fe2c9fb10001f61a817</QIMEI36><oaid>01C0200C618031C548A1E867C0702C54E022C56464D630AAE451DD5A9C16C15B91508B690411CDFB442A8A76693AC2CC7E1A033D3DBECAAE56F100E7873C6D07E4601D4CB006DA4E66</oaid><taid>0101869F2436A6BAABAA265FB77974AA500B10AC497B526399B22D828983A516EDE01448D9BE86B8B1A57F95</taid><tmeAppID>qqmusic</tmeAppID><tid>7562536653225149440</tid><modeSwitch>6</modeSwitch><teenMode>0</teenMode><M-Value>NM5nBGgMV3Jx/STX9QmVlQ==</M-Value><ui_mode>1</ui_mode><nettype>1020</nettype><wid>6755792100</wid><rom>vivo/FUNTOUCH/OriginOS 5</rom><uid>6755792100</uid><sid>202609271441196755792100</sid><qq>1852178459</qq><authst>Q_H_L_63k3N_6Sol56IeuR1KvAfC9D22kT-TjGrwmViafmbwmcyKpM9OdqNZlN-y4-s1XSYdx9lPoMQ1PevOyKcMx9atXOsZmoSR7ip_i1NuoFIeWYGf89ir_D62ep7aFvg0a1cf9HSZ9_kpyiiXU1MPCmsZsHO</authst><psrf_qqopenid>2FB47F3AA9C8279396089C48EB916A32</psrf_qqopenid><psrf_access_token_expiresAt>1795674948</psrf_access_token_expiresAt><tyt_exp_env>0</tyt_exp_env><v4ip>116.162.226.126</v4ip><psrf_qqaccess_token>197070EDF8BB9A9A5E010B4D054D56F6</psrf_qqaccess_token><trigger_type>ipc</trigger_type><hotfix>200000000</hotfix><traceid>11_06755792100_1790491671</traceid><cid>228</cid>
<item cmd="2000049" optime="1790491335" nettype="1020" QQ="1852178459" uid="6755792100" os="15" model="V2338A" version="20.9.0.8" songid="368182354" int1="3" int2="1" int3="48" int4="1" int6="65540" str1="8,157,42800367," str2="album:29145437" str3="s368182354.ct11.u1852178459.t1790491335801" int5="0" str4="" str5="" str6="0" abt="2553_2553004"/></root>'''


class QQMusic(QMClient):
    def __init__(self, key, uin):
        super().__init__(key, uin, 'qqmusic_key')

    # ---------- 五任务 ----------

    def state(self):
        d = self.api('music.lvz.LevelConfigSvr', 'GetState', {})
        if d.get('code') != 0:
            raise RuntimeError('GetState 异常 code:{}'.format(d.get('code')))
        return (d.get('data') or {}).get('StateMap') or {}

    def sign_daily(self):
        d = self.api('music.lvz.MuFest13TaskSvr', 'EveryDaySignLvzScore', {'Cmd': 'get'})
        data = d.get('data') or {}
        if data.get('Ret') == 0:
            # Total 字段=本次签到实得成长值（10-01 实测: 领取前查询 Total:0 / 领取后 Total:15，
            # 与 App 成长明细"每日签到 成长值+15"一致）
            got = data.get('Total')
            if isinstance(got, int) and got > 0:
                print('[签到] 签到成功 成长值+{}'.format(got))
                return got
            print('[签到] 签到成功（成长值实值见 App 明细）')
            return 0
        print('[签到] Ret:{} {}'.format(data.get('Ret'), data.get('Msg', '')))
        return 0

    def _pendant(self):
        """任务8：已佩戴时 Cancel→Use 制造当日动作。"""
        d = self.api('music.vip.PendantUserSvr', 'QueryUserPendant', {})
        pid = ((d.get('data') or {}).get('pendant') or {}).get('id')
        if not pid:
            print('[挂件] 无挂件库存，任务8跳过')
            return
        r = self.api('music.vip.PendantUserSvr', 'UsePendant', {'id': int(pid)})
        rc = (r.get('data') or {}).get('retCode')
        if rc == 42502:  # repeat error = 已佩戴，取消后重戴
            self.api('music.vip.PendantUserSvr', 'CancelPendant', {'id': int(pid)})
            r = self.api('music.vip.PendantUserSvr', 'UsePendant', {'id': int(pid)})
            rc = (r.get('data') or {}).get('retCode')
        print('[挂件] UsePendant retCode:{}'.format(rc))

    def _cosmetic(self, method, param):
        comm = {'g_tk': time33(self.key), 'uin': int(self.uin), 'format': 'json',
                'inCharset': 'utf-8', 'outCharset': 'utf-8', 'notice': 0}
        comm.update(APP_COMM)
        return self.api('music.cosmeticcgi.UserCosmeticCgi', method, param, comm=comm)

    def _subject(self):
        """任务9：UseCosmetic 当前使用中的装扮（换装扮自动跟随）。"""
        items = ((self._cosmetic('GetUserCosmetic', {'category': 'subject'})
                  .get('data') or {}).get('cosmetics')) or []
        cur = next((c for c in items if c.get('state') == 1), items[0] if items else None)
        if cur is None:
            print('[装扮] subject 类目无装扮，任务9跳过')
            return
        print('[装扮] 当前装扮: id={} {}'.format(cur.get('id'), str(cur.get('name'))[:20]))
        r = self._cosmetic('UseCosmetic', {'category': 'subject', 'itemID': cur.get('id'),
                                           'manufacture': 'vivo'})
        print('[装扮] UseCosmetic code:{}'.format(r.get('code')))

    def _fake_play(self):
        """任务20：重放节目播放上报（cmd=2000049）。默认内置模板；
        环境变量 QM_PLAY_TPL_B64 可覆盖。
        2026-10-03 破案：固定 songid 连续多天上报后被统计通道去重不计次
        （10-01/10-02 第1-3次入账成功，10-03 第4-5次起 20 永远 W/30005，
        App 端同样领不动）。修复：songid 按日期轮换（池=368182354 的相似
        有声书集，真实存在且同为 ct11 语境）。"""
        tpl_b64 = os.environ.get('QM_PLAY_TPL_B64', '').strip()
        if tpl_b64:
            try:
                tpl = b64decode(tpl_b64).decode()
            except Exception:
                print('[节目] QM_PLAY_TPL_B64 不是合法 base64，改用内置模板')
                tpl = PLAY_TPL_2000049
        else:
            tpl = PLAY_TPL_2000049
        now = str(int(time.time()))
        songid, album_id = SONG_POOL[time.localtime().tm_yday % len(SONG_POOL)]
        if songid != 368182354:
            tpl = tpl.replace('368182354', str(songid))
        if album_id != 29145437:
            tpl = tpl.replace('album:29145437', 'album:{}'.format(album_id))
        m_uid = re.search(r'<uid>(\d+)</uid>', tpl)
        tme_uid = m_uid.group(1) if m_uid else '0'
        body = re.sub(r'<qq>\d+</qq>', '<qq>{}</qq>'.format(self.uin), tpl)
        body = re.sub(r'QQ="\d+"', 'QQ="{}"'.format(self.uin), body)
        body = re.sub(r'<authst>[^<]*</authst>',
                      '<authst>{}</authst>'.format(self.key), body)
        body = re.sub(r'(<traceid>)[^<]*(</traceid>)',
                      r'\g<1>11_{}_{}\g<2>'.format(tme_uid, now), body)
        body = re.sub(r'(<tid>)[^<]*(</tid>)', r'\g<1>{}0000abcd\g<2>'.format(now), body)
        body = re.sub(r'(<sid>)[^<]*(</sid>)',
                      r'\g<1>{}{}\g<2>'.format(time.strftime('%Y%m%d%H%M%S'), tme_uid), body)
        body = re.sub(r'(?<= )optime="\d+"', 'optime="{}"'.format(now), body)
        # cmd=2000049 模板：str3 里的时间戳（s368182354.ct11.u{uin}.t{ts}001）
        body = re.sub(r'(str3="s\d+\.ct\d+\.u\d+\.t)\d+(")',
                      r'\g<1>{}\g<2>'.format(now + '001'), body)
        data = gzip_compress(body.encode())
        req = urllib.request.Request(
            REPORT_API, data=data, method='POST', headers={
                'User-Agent': 'QQMusic 20090008(android 15)',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Content-Encoding': 'gzip', 'Host': 'stat.y.qq.com',
                'Content-Length': str(len(data))})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print('[RAW][fake_play] {} {}'.format(r.status, r.read()[:80]))
        except Exception as ex:
            print('[节目] 伪造播放上报异常: {}'.format(ex))
        print('[节目] 节目播放上报已提交（cmd=2000049, songid={}）'.format(songid))

    def _sign_state(self):
        """签到预查（Cmd:qry）：True=今日已签。"""
        d = self.api('music.lvz.MuFest13TaskSvr', 'EveryDaySignLvzScore', {'Cmd': 'qry'})
        return (d.get('data') or {}).get('Ret') == 20019

    def run(self):
        """每次运行同构：探状态 → 未完成的执行动作 → 全量领取（发放后冷却
        已拉开）。已完成的任务零动作请求（防风控）。

        行为约定（保守路径，实测稳定）：
        W 只领不做动作（9-29 实测 W 态补发行为上报后领取失败过，不碰）
        20 领取前先挂 StartTask（顺带拉开与上一领取的间隔，避开冷却）
        20 跨天不重置：当日未签(qry 20028)时 20:Y 是昨日残留 → 强制重做
        """
        self.refresh()
        st = self.state()
        signed = self._sign_state()
        print('[状态] 签到:{} 挂件:{} 装扮:{} 听书:{} 节目:{}'.format(
            '已签' if signed else '未签', st.get('8'),
            st.get('9'), st.get('30'), st.get('20')))

        total = 0
        if signed:
            print('[签到] 今日已签到')
        else:
            total += self.sign_daily()

        # 状态三态统一：N=执行动作 / W=进行中直接领取（能否领取决于行为已计次） / Y=零请求
        if st.get('8') == 'N':
            self._pendant()
        if st.get('9') == 'N':
            self._subject()
        if st.get('30') == 'N':
            self.api('music.lvz.LevelConfigSvr', 'RecordBehavior', {'ID': '30'})
            print('[听书] 行为上报完成')
        if st.get('20') == 'N' or not signed:
            # 当日未签时 20:Y 是昨日残留（跨天不重置），强制重做
            r = (self.api('music.lvz.LevelConfigSvr', 'StartTask',
                          {'ID': '20'}).get('data') or {})
            started = r.get('code')
            cnt = r.get('count')
            if cnt is not None:
                print('[节目] 当前播放计数: {}（StartTask 返回）'.format(cnt))
            if started == 1:
                print('[节目] 开始标记已挂 → 伪造播放上报')
                self._fake_play()
            else:
                print('[节目] StartTask 返回 code:{}，无法开始'.format(started))

        # 领取：20020=冷却（成功发放后~2s内下一个被拒，等3s重领拉开）；
        # 30005=行为未计次——仅20值得等60s兜底（10-01实测9.6s拒/54s成，
        # 池同步窗口10-60s；每天仅两轮，不等=+10丢到明天）。8/9/30走秒级
        # 入库通道从未撞过30005，撞了也不陪等直接留待下轮。
        # 勿连撞多次（10-03 当日累计6次失败后写接口10006锁）。
        st_now = self.state()
        for tid, name in [(8, '头像挂件'), (9, '主题装扮'), (30, '逛听书频道'), (20, '收听节目')]:
            k = str(tid)
            if tid == 20 and st_now.get(k) == 'W':
                self.api('music.lvz.LevelConfigSvr', 'StartTask', {'ID': '20'})  # 领取钥匙
            d = self.api('music.lvz.LevelConfigSvr', 'IssueAward', {'ID': k})
            code = d.get('code')
            if code == 20020 or (code == 30005 and tid == 20):
                wait = 3 if code == 20020 else 60
                print('[任务] {} → {} 触发{}秒兜底重领'.format(name, code, wait))
                time.sleep(wait)
                d = self.api('music.lvz.LevelConfigSvr', 'IssueAward', {'ID': k})
                code = d.get('code')
            if code == 0:
                got = (d.get('data') or {}).get('DrawScores', 0)
                print('[任务] {} → 领取成功 +{}'.format(name, got))
                total += got
                time.sleep(3)  # 成功发放后 ~2s 冷却，拉开下一个领取
            elif code == 30009:
                print('[任务] {} → 今日已完成'.format(name))
            else:
                print('[任务] {} → code:{}（未识别，留待下次运行）'.format(name, code))
                print('[RAW][award] {} code:{} {}'.format(
                    name, code, json.dumps((d.get('data') or {}), ensure_ascii=False)[:200]))

        tasks_done = all(st_now.get(k) == 'Y' for k in ('8', '9', '20', '30'))
        if tasks_done and signed:
            print('[QQ音乐] 今日任务全部完成，无新增（合计: {}）'.format(total))
        else:
            print('[QQ音乐] 本次新增成长值合计: {}'.format(total))
        return total


def main():
    keys = [k.strip() for k in (os.environ.get('QM_KEY') or '').splitlines() if k.strip()]
    uin = (os.environ.get('QM_UIN') or '').strip()
    if not keys or not uin:
        print('[QQ音乐] 未配置 QM_KEY / QM_UIN，跳过')
        return
    for key in keys:
        try:
            QQMusic(key, uin).run()
        except Exception as ex:
            print('[QQ音乐] 运行失败: {}'.format(ex))


if __name__ == '__main__':
    main()

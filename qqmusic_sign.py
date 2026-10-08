#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ音乐会员成长值签到（任务中心五任务 + 会员福利站，接口实测逆向）。

每日产出（成长值）：签到 15（响应 Total 字段实值） + 挂件 5 + 装扮 5 + 听书 5
+ 收听节目 10 + 福利站签到 15（独立于任务中心的第二份，RenewalPlaySignIn）。

会员福利站（10-09 协议实测，网页版 renewal_reward，api() 直连）：
  每日签到   RenewalPlaySignInCgi.RenewalPlaySignIn（幂等码 14822150=已签）
  礼券任务   StarWishTaskCgi.ListStarWishTask(type 502 status≠2 →)
             VipRenewalCouponCgi.ClaimVipRenewCoupon(couponID 网页内置)
             （幂等码 14816772=已领，+20 星愿值/日）
  周里程碑   连签满 7 天 → 星动抽奖券（SVIP 奖池抽奖用）
  月里程碑   连签满 20 天 → 腾讯视频月卡等
  （听歌秒数伪造与自动补签链 10-09 验证后撤除，只留每日签到+礼券）

任务协议（music.lvz.LevelConfigSvr）：GetState 查状态 / IssueAward 领奖
(30009=已领) / RecordBehavior 行为上报(仅30) / StartTask 开始(仅20)。
服务端"超级会员专享"仅 App 弹窗拦截，接口层放行。

任务动作语义（2026-10-08 全量重查定稿）：
  8/9/30 即做即领（任务系统实时账本，0 点准时翻新一天）。
  20 节目走播放统计池（跨天滚动翻账本，时刻不定<06:02；00:01 轮撞 30009
  即昨日账未翻，留待第二轮必成）。RecordBehavior(20) 实测 30012 不受理、
  StartTask(30) 实测 30013 不受理——两任务通道互斥，不可互换。
  8 挂件  - UsePendant 当日动作（已佩戴时返 42502，需 Cancel→Use 成对）
  9 装扮  - UseCosmetic 当日动作（动态跟随使用中 state=1 的装扮，不锁定 ID）
  20 节目 - StartTask 挂开始标记 + 播放上报（cmd=2000049 模板经 QM_PLAY_TPL_B64 传入）
  30 听书 - RecordBehavior(ID:30) 把状态推到 W

领取定律（10-08 定稿，10-09/10-10 修订）：
  20020 = 领取冷却——一次成功发放(code:0)后 ~2 秒内的下一个 IssueAward 被
  拒。撞到等 3s 原地重领一次。
  30005 = 行为未计次，两场景：①上报已受理但池同步延迟（统计埋点通道
  imusic_tj 异步批处理，实测窗口无固定上限：10-01 ≤54s 成 / 10-09 >5.7s /
  10-10 >60.3s 仍未入、当天 11:45 App 实查任务已可领——盲等多久都可能
  不够）→ 10-10 起弃盲等，改 GetState 只读轮询：state 20 翻 W 即行为已
  入账，翻的瞬间补领（POLL_WAIT/POLL_MAX 控制，只读不占 10006 额度）；
  ②上报被统计通道去重丢弃（固定 songid 连续上报第 4-5 次起，10-03 实锤）
  →量永远不足，轮询超时仍 N 即弃。songid 轮换防 ②。
  10006 = 写操作锁——当日 IssueAward 失败累计过多后 StartTask/IssueAward
  顶层被锁（GetState 只读不受影响），次日恢复。勿连撞领取。
  30009 = 已领（含当日已领与跨天前的昨日延续：服务端 20 跨天重置在 0 点后
  滚动执行，00:01 轮所见 20:Y 即昨日残留——昨日单若未完成会被作废关单
  转 Y，奖励蒸发不发，经验明细可辨真领与作废）。
  10-10 复盘：00:01 轮 20:Y（昨日残留）跳过领取+上报被旧账本吞掉（设计
  内），06:02 轮重上报后 +0.2s/+60.3s 两领皆 30005——池同步 >60s，60s
  盲等不够，当日 +5 蒸发。用户 11:45 App 实查任务已可领（上报最终入账，
  只是晚于盲等窗）。

环境变量：
  QM_KEY         qm_keyst 值（Q_H_L_ 开头，抓包任意 y.qq.com 请求的 Cookie）
  QM_UIN         QQ 号（纯数字）
  QM_PLAY_TPL_B64 任务20播放上报模板（base64，必配——仓库不留含凭证的样本）
  （QM_CACHE_DIR 见 qmcommon.py）
"""
import json
import os
import random
import re
import time
import urllib.request
from base64 import b64decode
from gzip import compress as gzip_compress

from qmcommon import QMClient, time33, UA

REPORT_API = 'https://stat.y.qq.com/android/fcgi-bin/imusic_tj'
# App 通道 comm（cosmetic 系接口实测认 ct:11）
APP_COMM = {'ct': '11', 'cv': '20090008'}
# 福利站每日礼券 ID（网页前端内置值，HAR 实证 10-09；接口响应不下发）
COUPON_ID = 'ywa1ap44aj'

# 任务20 回落池（(songid, album_id)，同模板专辑的真实曲目）：
# 白名单实时拉取失败时兜底。同 id 连续上报被统计通道去重（10-03 破案），
# 回落按日轮换打散。
SONG_POOL = [(368182354, 29145437), (508222545, 29145437),
             (367799432, 29145437), (493099611, 51326893),
             (368182357, 29145437), (270384187, 13210184)]

# 30005 状态轮询参数（10-10 起）：每 POLL_WAIT 秒 GetState 一次，state 20
# 翻 W 即补领；上限 POLL_MAX。注意 main.py 单模块 TIMEOUT 须 ≥ POLL_MAX+余量。
POLL_WAIT = 15
POLL_MAX = 240

class QQMusic(QMClient):
    def __init__(self, key, uin):
        super().__init__(key, uin, 'qqmusic_key')

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
                print(f'[签到] 签到成功 成长值+{got}')
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
        print(f'[挂件] UsePendant retCode:{rc}')

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
        """任务20：重放节目播放上报（cmd=2000049）。模板经环境变量
        QM_PLAY_TPL_B64 传入（仓库内置样本已移除：含账号凭证不进库）。
        2026-10-03 破案：固定 songid 连续多天上报后被统计通道去重不计次。
        修复：同专辑白名单随机选曲（GetAlbumSongList 实时拉取全集 ~3500 集），
        拉取失败回落内置池按日轮换。"""
        tpl_b64 = os.environ.get('QM_PLAY_TPL_B64', '').strip()
        tpl = ''
        if tpl_b64:
            try:
                tpl = b64decode(tpl_b64).decode()
            except Exception:
                print('[节目] QM_PLAY_TPL_B64 不是合法 base64')
        if not tpl:
            print('[节目] 未配置 QM_PLAY_TPL_B64 播放模板，跳过任务20上报')
            return
        now = str(int(time.time()))
        # 白名单实时拉取：模板同专辑曲目全集（日更有声书 ~3500 集），
        # 随机段随机选；失败回落内置池按日轮换
        m_song = re.search(r'(?<= )songid="(\d+)"', tpl)
        m_album = re.search(r'album:(\d+)', tpl)
        ori_songid = (m_song.group(1) if m_song else '368182354')
        songid = ori_songid
        album_id = 29145437
        if m_album:
            try:
                begin = random.randint(0, 3000)
                d = self.api('music.musichallAlbum.AlbumSongList', 'GetAlbumSongList',
                             {'albumid': int(m_album.group(1)), 'begin': begin, 'num': 40})
                pool = [str((s.get('songInfo') or {}).get('id'))
                        for s in (d.get('data') or {}).get('songList') or []]
                pool = [x for x in pool if x and x != ori_songid]
                if pool:
                    songid = random.choice(pool)
                    print('[节目] 白名单随机曲目 songid={}（专辑第{}段起40选1）'.format(songid, begin))
            except Exception as ex:
                print(f'[节目] 白名单拉取失败（{ex}），回落内置池')
        if songid == ori_songid:
            songid, album_id = SONG_POOL[time.localtime().tm_yday % len(SONG_POOL)]
        if songid != ori_songid:
            tpl = tpl.replace(ori_songid, str(songid))
        if album_id != 29145437:
            tpl = tpl.replace('album:29145437', f'album:{album_id}')
        m_uid = re.search(r'<uid>(\d+)</uid>', tpl)
        tme_uid = m_uid.group(1) if m_uid else '0'
        body = re.sub(r'<qq>\d+</qq>', f'<qq>{self.uin}</qq>', tpl)
        body = re.sub(r'QQ="\d+"', 'QQ="{}"'.format(self.uin), body)
        body = re.sub(r'<authst>[^<]*</authst>',
                      f'<authst>{self.key}</authst>', body)
        body = re.sub(r'(<traceid>)[^<]*(</traceid>)',
                      r'\g<1>11_{}_{}\g<2>'.format(tme_uid, now), body)
        body = re.sub(r'(<tid>)[^<]*(</tid>)', rf'\g<1>{now}0000abcd\g<2>', body)
        body = re.sub(r'(<sid>)[^<]*(</sid>)',
                      r'\g<1>{}{}\g<2>'.format(time.strftime('%Y%m%d%H%M%S'), tme_uid), body)
        body = re.sub(r'(?<= )optime="\d+"', 'optime="{}"'.format(now), body)
        # cmd=2000049 模板：str3 里的时间戳（s368182354.ct11.u{uin}.t{ts}001）
        body = re.sub(r'(str3="s\d+\.ct\d+\.u\d+\.t)\d+(")',
                      r'\g<1>{}\g<2>'.format(now + '001'), body)
        data = gzip_compress(body.encode())
        req = urllib.request.Request(
            REPORT_API, data=data, method='POST', headers={
                'User-Agent': UA,
                'Content-Type': 'application/x-www-form-urlencoded',
                'Content-Encoding': 'gzip', 'Host': 'stat.y.qq.com',
                'Content-Length': str(len(data))})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print('[RAW][fake_play] {} {}'.format(r.status, r.read()[:80]))
        except Exception as ex:
            print(f'[节目] 伪造播放上报异常: {ex}')
        print(f'[节目] 节目播放上报已提交（cmd=2000049, songid={songid}）')

    def _sign_state(self):
        """签到预查（Cmd:qry）：True=今日已签。"""
        d = self.api('music.lvz.MuFest13TaskSvr', 'EveryDaySignLvzScore', {'Cmd': 'qry'})
        return (d.get('data') or {}).get('Ret') == 20019

    def _vip_station(self):
        """会员福利站（网页版 renewal_reward，10-09 协议实测）：每日签到 +
        星愿任务。走 u6 h5 通道与 App 接口同 cookie 同签名（g_tk/webkey/UA
        均不校验，api() 直连）。返回本次新增成长值（签到 15）。"""
        gained = 0
        # ---- 每日签到（独立于任务中心的 EveryDaySignLvzScore，两份都拿）----
        d = self.api('music.vipcgi.RenewalPlaySignInCgi', 'GetMonthSignInData',
                     {'month': int(time.strftime('%Y%m'))})
        days = {x.get('day'): x.get('signInStatus')
                for x in (d.get('data') or {}).get('days') or []}
        month_days = (d.get('data') or {}).get('signInDays') or 0
        if days.get(int(time.strftime('%d'))) == 2:
            print(f'[福利站] 今日已签到（本月 {month_days} 天）')
        else:
            r = self.api('music.vipcgi.RenewalPlaySignInCgi', 'RenewalPlaySignIn', {})
            code = r.get('code')
            if code == 0:
                data = r.get('data') or {}
                name = ((data.get('todayIncentive') or {}).get('incentiveName')) or ''
                print('[福利站] 签到成功：{}（本月 {} 天）'.format(name or '奖励', month_days + 1))
                m = re.match(r'(\d+)成长值', name)
                if m:
                    gained += int(m.group(1))
                for mk in ('weekMilestoneIncentive', 'monthMilestoneIncentive'):
                    inc = data.get(mk) or {}
                    if inc.get('incentiveName'):
                        print('[福利站] {}：{}'.format(
                            '周里程碑达成' if mk.startswith('week') else '月里程碑达成',
                            inc.get('incentiveName')))
            elif code == 14822150:
                print(f'[福利站] 今日已签到（幂等码，本月 {month_days} 天）')
            else:
                print(f'[福利站] 签到异常 code:{code}')

        # ---- 星愿任务（礼券是完成动作）----
        d = self.api('music.vipcgi.StarWishTaskCgi', 'ListStarWishTask',
                     {'source': 'renewalplay_station'})
        tasks = (d.get('data') or {}).get('tasks') or []
        t502 = next((t for t in tasks if t.get('taskType') == 502), None)
        if t502 and t502.get('taskStatus') != 2:
            r = self.api('music.vipcgi.VipRenewalCouponCgi', 'ClaimVipRenewCoupon',
                         {'couponID': COUPON_ID})
            code = r.get('code')
            if code == 0:
                print('[礼券] 领取成功 +{}星愿值'.format(t502.get('taskReward') or 20))
            elif code == 14816772:
                print('[礼券] 今日已领取')
            else:
                print(f'[礼券] code:{code}（couponID 可能已换，留待排查）')
        elif t502:
            print('[礼券] 今日已完成')
        return gained

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
        t_report = 0  # 任务20本次上报时刻（兜底时距上报秒数）
        if st.get('20') == 'N' or not signed:
            # 当日未签时 20:Y 是昨日残留（跨天不重置），强制重做
            r = (self.api('music.lvz.LevelConfigSvr', 'StartTask',
                          {'ID': '20'}).get('data') or {})
            started = r.get('code')
            cnt = r.get('count')
            if cnt is not None:
                print(f'[节目] 当前播放计数: {cnt}（StartTask 返回）')
            if started == 1:
                print('[节目] 开始标记已挂 → 伪造播放上报')
                self._fake_play()
                t_report = time.time()
            else:
                print(f'[节目] StartTask 返回 code:{started}，无法开始')

        # 领取：20020=冷却（等3s重领拉开）；30005=行为未计次——10-10 实锤
        # 池同步可>60s（盲等多久都可能不够），改 GetState 只读轮询翻 W 即领
        # （不占 10006 失败额度）；超时仍 N=场景②去重丢弃，等无用即弃。
        # 8/9/30 走秒级入库通道从未撞过30005，撞了也不陪等。
        # 勿连撞领取（10-03 当日累计6次失败后写接口10006锁）。
        st_now = self.state()
        for tid, name in [(8, '头像挂件'), (9, '主题装扮'), (30, '逛听书频道'), (20, '收听节目')]:
            k = str(tid)
            if st_now.get(k) == 'Y':
                print(f'[任务] {name} → 今日已完成')
                continue  # 已领零写（10-09 实测此前每天 4 发幂等写，改为真零写）
            if tid == 20 and st_now.get(k) == 'W':
                self.api('music.lvz.LevelConfigSvr', 'StartTask', {'ID': '20'})  # 领取钥匙
            d = self.api('music.lvz.LevelConfigSvr', 'IssueAward', {'ID': k})
            code = d.get('code')
            if code == 20020:
                print('[任务] {} → 20020 触发3秒兜底重领'.format(name))
                time.sleep(3)
                d = self.api('music.lvz.LevelConfigSvr', 'IssueAward', {'ID': k})
                code = d.get('code')
            elif code == 30005 and tid == 20:
                # 10-10 实锤：盲等60s不够（06:02 轮 +0.2s/+60.3s 两领皆 30005，
                # 当天 11:45 App 实查任务已可领——池同步窗口无固定上限）。
                # 只读轮询 GetState：state 20 翻 W=行为已入账，翻的瞬间补领；
                # 翻 Y=他处已领按 30009 走；超时仍 N=去重丢弃等无用。
                # t_report=0 且 15s 后仍 N=本轮无上报可等（行为缺失），直接弃。
                gap = int(time.time() - t_report) if t_report else -1
                print('[任务] {} → 30005 转状态轮询（距上报{}，翻W即领/上限{}s）'.format(
                    name, (str(gap) + 's') if t_report else '无上报', POLL_MAX))
                deadline = time.time() + POLL_MAX
                extra = 0
                while time.time() < deadline and extra < 2:
                    time.sleep(POLL_WAIT)
                    v = self.state().get(k)
                    if v == 'N' and not t_report:
                        print(f'[任务] {name} → 无上报可等且 state 仍 N（行为缺失非延迟），弃')
                        break
                    if v == 'Y':
                        print(f'[任务] {name} → 轮询见 state:Y（他处已领）')
                        code = 30009
                        break
                    if v == 'W':
                        d = self.api('music.lvz.LevelConfigSvr', 'IssueAward', {'ID': k})
                        code = d.get('code')
                        extra += 1
                        print('[任务] {} → 轮询{}s 翻W → 补领 code:{}'.format(
                            name, int(time.time() - t_report) if t_report else -1, code))
                        if code in (0, 30009):
                            break
                        if code == 30005:
                            time.sleep(10)  # state 已 W 仍 30005：读侧先行，再等一轮
                        else:
                            break
                else:
                    v = self.state().get(k)
                    print('[任务] {} → 30005 轮询未成（终态 state:{}），留待下次运行'.format(
                        name, v))
            if code == 0:
                got = (d.get('data') or {}).get('DrawScores', 0)
                print('[任务] {} → 领取成功 +{}'.format(name, got))
                total += got
                time.sleep(3)  # 成功发放后 ~2s 冷却，拉开下一个领取
            elif code == 30009:
                print(f'[任务] {name} → 今日已完成')
            else:
                print('[任务] {} → code:{}（未识别，留待下次运行）'.format(name, code))
                print('[RAW][award] {} code:{} {}'.format(
                    name, code, json.dumps((d.get('data') or {}), ensure_ascii=False)[:200]))

        tasks_done = all(st_now.get(k) == 'Y' for k in ('8', '9', '20', '30'))
        # 会员福利站（独立体系，失败不影响任务中心主流程）
        try:
            total += self._vip_station()
        except Exception as ex:
            print(f'[福利站] 运行异常: {ex}')
        if tasks_done and signed:
            print(f'[QQ音乐] 今日任务全部完成，无新增（合计: {total}）')
        else:
            print(f'[QQ音乐] 本次新增成长值合计: {total}')


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
            print(f'[QQ音乐] 运行失败: {ex}')


if __name__ == '__main__':
    main()

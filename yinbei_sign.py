#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ音乐开放平台「音贝」任务签到（任务中心全套，接口实测逆向）。

每日/每周产出（音贝）：签到 10 + 邀请入驻 100×30（每日刷新）+ 互动/作品任务
（每周刷新）+ 周阶梯奖励（完成 3/6/8 个任务 → 10/20/50）。

任务协议（music.sociality.KolTask，与 QQ 音乐任务体系共用认证与公共底座）：
  getUserTask          任务列表（progress/total 即已用/上限额度）
  reportUserTask       行为上报（type 任务类型，count 笔数，op:1=新记录，批量可推满）
  userDoCollect        领取（count>0 即到账，重复领取返回 0 幂等）
  getUserWeekTask      周进度与阶梯（stage[].state: 1=可领 2=已领）
  completeUserWeekTask 领取周阶梯
服务端对上报内容零校验（op:1 直接入账）。

统一逻辑：先探额度只补差量（已满任务零请求防风控）；通知全量：每个任务
可见（本次+X（进度）、已满、签到单独行），周阶梯全状态。

环境变量（与 qqmusic_sign.py 共用同一凭证）：QM_KEY / QM_UIN。
"""
import os
import time

from qmcommon import QMClient, time33

SIGN_TASK_ID = 0  # 每日签到固定 taskid/type

# 隐藏任务表：任务列表接口不返回、但 reportUserTask 直接付钱的任务。
# type 44「分享黑胶作品」+30/次、3次/天，2026-11-02 全号段扫描实测发现。
HIDDEN_TASKS = [
    {'type': 44, 'times': 3, 'credits': 30, 'title': '分享黑胶作品'},
]


class YinBei(QMClient):
    def __init__(self, key, uin):
        super().__init__(key, uin, 'yinbei_key')

    def call(self, method, param):
        return self.api('music.sociality.KolTask', method, param, comm={
            'g_tk': time33(self.key), 'uin': int(self.uin), 'format': 'json',
            'platform': 'h5', 'ct': 23, 'cv': 0})

    def tasks(self):
        groups = (self.call('getUserTask', {}).get('data') or {}).get('listByKolType') or []
        out = []
        for g in groups:
            if isinstance(g, dict):
                out.extend(g.get('list') or [])
        return out

    def collect(self, tid):
        return ((self.call('userDoCollect', {'taskId': tid})
                 .get('data') or {}).get('count', 0)) or 0

    def _credits(self):
        """音贝余额只读查询（music.sociality.KolCredits，隐藏任务到账复核用）。"""
        d = self.api('music.sociality.KolCredits', 'queryCredits', {'type': 0}, comm={
            'g_tk': time33(self.key), 'uin': int(self.uin), 'format': 'json',
            'platform': 'h5', 'ct': 23, 'cv': 0})
        return (d.get('data') or {}).get('current') or 0

    def run(self):
        self.refresh()
        st = self.tasks()
        if not st:
            print('[音贝] 任务列表为空，跳过')
            return
        total = 0

        sign = next((t for t in st if t.get('taskid') == SIGN_TASK_ID), None)
        if sign and sign.get('progress', 0) < 1:
            self.call('reportUserTask', {'type': SIGN_TASK_ID, 'count': 1, 'op': 0})
            got = self.collect(SIGN_TASK_ID)
            print(f'[签到] +{got}' if got else '[签到] 领取未到账')
            total += got
        elif sign:
            print('[签到] 今日已完成')

        for t in st:
            if t.get('taskid') == SIGN_TASK_ID:
                continue
            prog = t.get('progress') or 0
            tot = t.get('total') or 0
            if prog >= tot:
                print('[任务] {} 已满（{}/{}）'.format(t.get('title'), prog, tot))
                continue
            self.call('reportUserTask', {'type': t.get('type'), 'count': tot - prog, 'op': 1})
            got = self.collect(t.get('taskid'))
            if got:
                print('[任务] {} → +{}（{}/{}）'.format(t.get('title'), got, tot, tot))
                total += got
            else:
                print('[任务] {} 已上报（{}/{}）领取未到账'.format(t.get('title'), tot, tot))

        d = self.call('getUserWeekTask', {})
        for s in (d.get('data') or {}).get('stage') or []:
            if s.get('state') == 1:
                self.call('completeUserWeekTask', {'id': s.get('id')})
                print('[周奖励] {}（{}）已领取'.format(s.get('desc'), s.get('credits')))
            elif s.get('state') == 2:
                print('[周奖励] {}（{}）已完成'.format(s.get('desc'), s.get('credits')))

        # 隐藏任务：getUserTask 列表不返回但 reportUserTask 直付（2026-11-02 实测逆向）。
        # 延迟入账（~20s 自动到账），userDoCollect 恒返 0，不调用。
        # 11-03 实证 44 为周任务（周一重置，非每日）——纯日志记 +90 会虚报：
        # 改先探后做：报 1 发 → 20s 余额复核 → 有到账才补满剩余次数；
        # 未到账=本周已满/窗口未开，零写跳过（余额 queryCredits 只读便宜）。
        for h in HIDDEN_TASKS:
            base = self._credits()
            self.call('reportUserTask', {'type': h['type'], 'count': 1, 'op': 1})
            time.sleep(20)
            paid = self._credits() - base
            if paid >= h['credits']:
                for _ in range(h['times'] - 1):
                    self.call('reportUserTask', {'type': h['type'], 'count': 1, 'op': 1})
                got = h['times'] * h['credits']
                total += got
                print('[隐藏任务] {} ×{} → +{}（延迟到账）'.format(
                    h['title'], h['times'], got))
            else:
                print('[隐藏任务] {} 本周已满（探针1发未到账，零写跳过）'.format(h['title']))
        print(f'[音贝] 本次新增: {total}')


def main():
    key = (os.environ.get('QM_KEY') or '').strip()
    uin = (os.environ.get('QM_UIN') or '').strip()
    if not key or not uin:
        print('[音贝] 未配置 QM_KEY / QM_UIN，跳过')
        return
    try:
        YinBei(key, uin).run()
    except Exception as ex:
        print(f'[音贝] 运行失败: {ex}')


if __name__ == '__main__':
    main()

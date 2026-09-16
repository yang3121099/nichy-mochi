"""Beijing-time labels and quiet, local-calendar reminders."""
from datetime import date, datetime, timedelta, timezone
import json
import time

import mochi_core as core

BEIJING = timezone(timedelta(hours=8))
CALENDAR_SOURCE = 'https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html'


def beijing_time(at=None):
    return datetime.fromtimestamp(time.time() if at is None else at, BEIJING)


def timestamp(at=None):
    return beijing_time(at).strftime('%Y-%m-%d %H:%M:%S')


def submission_hour(at):
    return beijing_time(at).strftime('%Y-%m-%d_%H')


def submission_title(record):
    hour = record.get('hour')
    if hour and record.get('hour_number'):
        day, clock = hour.split('_')
        return '{} {}时 · 第 {} 次提交'.format(day,clock,record['hour_number'])
    return '第 {} 次提交'.format(record['number'])


def default_calendar():
    holidays = {}
    ranges = [('2026-01-01','2026-01-03','元旦'),('2026-02-15','2026-02-23','春节'),
              ('2026-04-04','2026-04-06','清明节'),('2026-05-01','2026-05-05','劳动节'),
              ('2026-06-19','2026-06-21','端午节'),('2026-09-25','2026-09-27','中秋节'),
              ('2026-10-01','2026-10-07','国庆节')]
    for start,end,name in ranges:
        day,stop = date.fromisoformat(start),date.fromisoformat(end)
        while day <= stop:
            holidays[day.isoformat()] = name
            day += timedelta(days=1)
    return {'years':{'2026':{'source':CALENDAR_SOURCE,'holidays':holidays,
                            'workdays':['2026-01-04','2026-02-14','2026-02-28',
                                        '2026-05-09','2026-09-20','2026-10-10']}}}


def load_calendar(path):
    years = default_calendar()['years']
    if path.exists():
        custom = json.loads(path.read_text())
        if not isinstance(custom,dict) or not isinstance(custom.get('years'),dict):
            raise ValueError('holidays.json 需要 years 字段')
        years.update(custom['years'])
    for year,record in years.items():
        if not isinstance(record,dict) or not isinstance(record.get('holidays'),dict) or not isinstance(record.get('workdays'),list):
            raise ValueError('节假日日历格式需要检查')
        for day in list(record['holidays'])+record['workdays']:
            if str(date.fromisoformat(day).year) != year:
                raise ValueError('节假日日历年份不一致')
        if any(not isinstance(name,str) for name in record['holidays'].values()):
            raise ValueError('节假日名称需要使用文字')
    return years


class Reminders:
    def __init__(self, root, say):
        self.root,self.say = root,say
        self.last_minute = None
        self.sent = None
        self.error = None
        try:
            self.calendar = load_calendar(root/'holidays.json')
        except (OSError,ValueError,TypeError) as exc:
            self.calendar = {}
            self.error = str(exc)

    def command_suffix(self, at):
        now = beijing_time(at)
        day = now.date().isoformat()
        calendar = self.calendar.get(str(now.year))
        holiday = bool(calendar and day in calendar['holidays'] and day not in calendar['workdays'])
        workday = bool(calendar and not holiday and (day in calendar['workdays'] or now.weekday()<5))
        notes = []
        if now.weekday() == 4:
            notes.append('🎉 终于周五啦')
        if holiday:
            notes.append('🎈 今天节假日，别加班了')
        if workday and (now.hour,now.minute)>=(22,30):
            notes.append('🌙 该下班了')
        return ' · '.join(notes)

    def emit_once(self, key, message, at):
        if key in self.sent:
            return
        updated = dict(self.sent)
        updated[key] = at
        # These decorative reminders are at-most-once, including after restart.
        core.write_json(self.root/'.reminders.json',updated)
        self.sent = updated
        self.say(message)

    def poll(self, at=None):
        at = time.time() if at is None else at
        minute = int(at//60)
        if minute == self.last_minute:
            return
        self.last_minute = minute
        if self.sent is None:
            path = self.root/'.reminders.json'
            loaded = core.read_json(path) if path.exists() else {}
            if not isinstance(loaded,dict):
                raise ValueError('彩蛋记录需要检查')
            self.sent = loaded
        now = beijing_time(at)
        day = now.date().isoformat()
        year = str(now.year)
        if self.error:
            self.emit_once('calendar-error:'+self.error,'📅 Mochi：日历需要检查，节假日提醒暂缓。',at)
        elif year not in self.calendar:
            self.emit_once('calendar-year:'+year,'📅 Mochi：'+year+' 年节假日日历待更新。',at)
        events = [(22,30,'night','🌙 Mochi：该下班了。')]
        if now.weekday() == 4:
            events += [(hour,0,'friday-'+str(hour),'🎉 Mochi：终于周五啦 终于周五啦 终于周五啦')
                       for hour in (9,12,17)]
        calendar = self.calendar.get(year,{})
        if day in calendar.get('holidays',{}) and day not in calendar.get('workdays',[]):
            events.append((9,0,'holiday','🎈 Mochi：今天节假日，别加班了。'))
        for hour,minute,kind,message in events:
            scheduled = now.replace(hour=hour,minute=minute,second=0,microsecond=0)
            delay = (now-scheduled).total_seconds()
            # A short catch-up window tolerates busy supervision without flooding
            # the log with old messages when the service starts late in the day.
            if 0 <= delay < 600:
                self.emit_once(day+':'+kind,message,at)

import garth, getpass
from datetime import date
c = garth.Client()
c.login('wm.john.craig@gmail.com', getpass.getpass('Password: '))
today = date.today().isoformat()
print('today:', today)
try:
    bb = c.connectapi(f'/wellness-service/wellness/bodyBattery/readingsByDate/{today}/{today}')
    print('BB:', bb)
except Exception as e:
    print('BB error:', e)
try:
    s = c.connectapi(f'/wellness-service/wellness/dailySleepData/wm.john.craig@gmail.com?date={today}')
    print('Sleep keys:', list(s.keys()) if s else 'empty')
except Exception as e:
    print('Sleep error:', e)

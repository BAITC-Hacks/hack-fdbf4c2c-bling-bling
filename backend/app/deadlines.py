import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import dateparser


def normalize(raw, started_at, timezone='Asia/Qyzylorda'):
    result = {'raw': raw, 'date': None, 'kind': 'missing', 'needs_review': True, 'note': 'Срок не указан'}
    if not raw:
        return result
    result.update(kind='relative', note='Проверьте интерпретацию срока')
    value = raw.casefold().strip()
    if any(term in value for term in ('после', 'при ', 'с момента', 'по факту', 'оплат', 'рабоч', 'жұмыс')):
        return {**result, 'kind': 'event', 'note': 'Условие/рабочие дни требуют проверки календаря и события'}
    explicit_year = re.search(r'\b20\d{2}\b', value)
    if not started_at and not explicit_year:
        return {**result, 'note': 'Нет даты совещания; год и относительную дату не додумывать'}
    base = datetime.fromisoformat(started_at) if started_at else datetime(2000, 1, 1)
    base = base.replace(tzinfo=ZoneInfo(timezone)) if base.tzinfo is None else base.astimezone(ZoneInfo(timezone))
    # Exact deterministic relative forms, including common Kazakh expressions.
    relative = {'сегодня': 0, 'бүгін': 0, 'завтра': 1, 'ертең': 1, 'послезавтра': 2}
    day = next((offset for word, offset in relative.items() if word in value), None)
    date = base + timedelta(days=day) if day is not None else None
    weekdays = {'понедельник': 0, 'вторник': 1, 'сред': 2, 'четверг': 3, 'пятниц': 4, 'суббот': 5, 'воскресень': 6,
                'дүйсенбі': 0, 'сейсенбі': 1, 'сәрсенбі': 2, 'бейсенбі': 3, 'жұма': 4}
    if date is None:
        for word, weekday in weekdays.items():
            if word in value:
                date = base + timedelta(days=(weekday - base.weekday()) % 7)
                break
    if date is None:
        value = re.sub(r'^(к|до|не позднее)\s+', '', value)
        date = dateparser.parse(value, languages=['ru', 'kk'], settings={'RELATIVE_BASE': base.replace(tzinfo=None), 'PREFER_DATES_FROM': 'future', 'DATE_ORDER': 'DMY'})
    if date:
        result.update(date=date.date().isoformat(), kind='date' if explicit_year else 'relative', note='Предложение; подтвердите перед напоминаниями')
    return result

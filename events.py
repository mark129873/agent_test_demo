"""Local event evidence, correlation and SQLite persistence."""
import re
from datetime import date, datetime, time, timedelta

EVENT_NAMES = {"interview":"面试","exam":"考试","meeting":"会议","appointment":"约会","birthday":"生日"}
NAME_KEYS = {name: key for key, name in EVENT_NAMES.items()}
OUTCOME = r"结束|做完了|完了|完啦|取消|通过了?|没过|过了|有结果了|拿到(?:了)?(?:offer|录用通知)|不去了"
CLAUSES = r"[，。！？；、,.!?;\n]"
ASSUME = r"假如|假设|如果|要是|设想|假装|比如(?:说)?|举个例子"
UNCERTAIN = r"不知道|不确定|能不能|能否|会不会|有可能|可能|也许|希望|但愿|担心|害怕|怕会|要不要|想要|听说|据说|还没(?:有)?(?:结束|结果|取消|通过|面试)|没有(?:取消|结束)|别取消|不取消|取消不了|不一定"
QUESTION = r"什么|哪儿|哪里|哪个|哪[一两]?场|是不是|(?:吗|么)[！!？?。\s]*$|[？?]$"
RESCHEDULE = r"改到|改成|改期(?:到|为)|改期了|推迟(?:到|至)|提前(?:到|至)|挪(?:到|至)|延期(?:到|至)|延到"
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]|(?:上上|上|下下|下|本|这)?(?:周|星期)[一二三四五六日天]|前天|昨天|今天|明天|后天"
EVENT_PATTERN = "|".join(NAME_KEYS)


def resolve_date(value, today):
    if not isinstance(value, str) or not re.fullmatch(DATE_PATTERN, value):
        return None
    try:
        if value in ("前天", "昨天", "今天", "明天", "后天"):
            return today + timedelta(days=("前天", "昨天", "今天", "明天", "后天").index(value) - 2)
        if "-" in value:
            return date.fromisoformat(value)
        if "月" in value:
            m, d = re.findall(r"\d+", value)
            target = date(today.year, int(m), int(d))
            if target < today:
                target = date(today.year + 1, int(m), int(d))
            return target
        weekday = "一二三四五六日".index(value[-1].replace("天", "日"))
        weeks = 2 if value.startswith("下下") else 1 if value.startswith("下") else -2 if value.startswith("上上") else -1 if value.startswith("上") else 0
        target = today + timedelta(days=weeks * 7 + weekday - today.weekday())
        if not value.startswith(("本", "这", "下", "上")) and target < today:
            target += timedelta(days=7)
        return target
    except ValueError:
        return None


def _identity(clause, match):
    """Use only a literal descriptor immediately attached to the event."""
    category = NAME_KEYS[match[0]]
    original = clause[:match.start()]
    prefix = re.split(DATE_PATTERN, original)[-1]
    prefix = re.split(r"还(?:有|要|得)|接下来|另外|再(?:有|去|加|跟|和)|分别|接着|然后|两场|一场|二场|一个|另[一有]|有(?:个|场)?|要去|将要|打算|参加|参加完|刚刚|刚|收到|通知我|安排了|那场|这场|的那场|的这场|[:：、]", prefix)[-1]
    prefix = re.sub(r"^(?:(?:我(?:和|跟|去|的)?|跟|和|去|给|是|在|跟着|上午|中午|下午|晚上|早上|是我|由)+)", "", prefix.strip())
    prefix = re.sub(r"(?:那场|这场|的|公司方)\s*$", "", prefix).strip(" \t“”\"‘’")
    generic = {"的","那场","这场","那个","这个","这个是","那个是","已经","刚才","要","得","就","又","之前","原来","原定","最后","之前的","约了","定了","快要","进行","还有"}
    suffix = re.match(r"\s*[（(]([^（）()]{1,30})[）)]", clause[match.end():])
    if not suffix:
        suffix = re.match(r"\s*([A-Za-z0-9\u4e00-\u9fff·＆& -]{1,20}?(?:公司|集团|银行|大学|研究所))", clause[match.end():])
    if not prefix or prefix in generic:
        organizations = re.findall(r"([A-Za-z0-9\u4e00-\u9fff·＆& -]{1,20}?(?:公司|集团|银行|大学|研究所))", original)
        if len(organizations) == 1:
            prefix = re.sub(r"^(?:我(?:和|跟|的|去)?|约了|跟|和|去|有|在|那场|这场)+", "", organizations[0].strip())
        elif suffix:
            prefix = suffix[1].strip()
    if not prefix or len(prefix) > 30 or not re.fullmatch(r"[\w＆&· -]+", prefix) or re.search(OUTCOME, prefix):
        return category
    if prefix in generic:
        return category
    # Equivalent company spelling/case should match without losing readable display.
    return f"{category}:{prefix}"


def _date_values(text, day):
    return [value.isoformat() for token in re.finditer(DATE_PATTERN, text)
            if (value := resolve_date(token[0], day)) is not None]


def _anchor_options(text, day):
    options = []
    for token in re.findall(DATE_PATTERN, text):
        if "月" in token:
            month, day_of_month = map(int, re.findall(r"\d+", token))
            for year in (day.year - 1, day.year, day.year + 1):
                try:
                    options.append(date(year, month, day_of_month).isoformat())
                except ValueError:
                    pass
        elif re.fullmatch(r"(?:周|星期)[一二三四五六日天]", token):
            value = resolve_date(token, day)
            if value:
                options.extend((value.isoformat(), (value - timedelta(days=7)).isoformat()))
    return options


def event_candidates(source, day, recent=()):
    """Compile literal clauses to changes; never take identity or date from model output."""
    previous = ""
    spans = list(re.finditer(r"[^，。！？；、,.!?;\n]+[？?]?", source))
    index = 0
    while index < len(spans):
        item = spans[index]
        index += 1
        quote = item[0].strip()
        if index < len(spans) and re.search(EVENT_PATTERN, quote) and not re.search(OUTCOME, quote):
            following = spans[index]
            between = source[item.end():following.start()]
            next_clause = following[0].strip()
            if (re.fullmatch(r"[，,]", between) and re.match(RESCHEDULE, next_clause)
                    and re.search(DATE_PATTERN, next_clause)):
                join_quote = source[item.start():following.end()].strip()
                # Leave out a preliminary “改期了” when locating the definitive date change.
                quote = join_quote
                index += 1
        prior, previous = previous, quote
        if (not quote or re.search(ASSUME, quote) or re.search(QUESTION, quote)
                or re.search(UNCERTAIN, quote) or re.search(r"扮演|忽略.{0,8}指令", quote)
                or re.fullmatch(r"(?:比如(?:说)?|举个例子|假设一下)[:：\s]*", prior)):
            continue
        # Explicit conjunctions split complete dated or action-bearing event clauses.
        parts = [quote]
        for separator in (r"(?:；|另外|然后|同时|还有|也有|而|和|以及)",):
            for match in re.finditer(separator, quote):
                left, right = quote[:match.start()].strip(), quote[match.end():].strip()
                if (re.search(EVENT_PATTERN, left) and re.search(EVENT_PATTERN, right)
                        and all(re.search(f"{DATE_PATTERN}|{OUTCOME}|{RESCHEDULE}", p) for p in (left, right))):
                    parts = [left, right]
                    break
        for clause in parts:
            mentions = list(re.finditer(EVENT_PATTERN, clause))
            changes = list(re.finditer(RESCHEDULE, clause))
            changed, resolved = (changes[-1] if changes else None), re.search(OUTCOME, clause, re.I)
            if len(mentions) > 1:
                if changed or resolved:
                    yield ("clarify_event", "", None, clause)
                continue
            key = _identity(clause, mentions[0]) if mentions else ""
            if not mentions:
                if resolved and not changed and not re.match(r"那[个场]|这[个场]|它", clause):
                    previous_user = next((m["text"] for m in reversed(recent) if m["role"] == "user"), "")
                    if not re.search(EVENT_PATTERN, previous_user):
                        continue
                if not re.fullmatch(
                    rf"(?:那[个场]|这[个场]|它|日期|时间|已经|刚才|刚|后来|最后|结果|原来)?\s*(?:(?:{DATE_PATTERN})(?:那场|这场)?)?\s*(?:{RESCHEDULE}|{OUTCOME}).*",
                    clause, re.I
                ):
                    continue
            if not changed and not resolved and not mentions:
                continue
            if resolved and not changed:
                dates = _date_values(clause, day)
                if len(set(dates)) > 1:
                    yield ("clarify_event", key, None, clause)
                else:
                    yield ("resolve", key, {"date": dates[0] if dates else None, "anchor_options": _anchor_options(clause, day)}, clause)
            elif changed:
                dates = _date_values(clause[changed.end():], day)
                from_dates = _date_values(clause[:changed.start()], day)
                if len(set(dates)) != 1 or len(set(from_dates)) > 1:
                    if dates or not resolved:
                        yield ("clarify_event", key, None, clause)
                    continue
                if dates[0] >= day.isoformat():
                    yield ("reschedule", key, {"date": dates[0], "previous_date": from_dates[0] if from_dates else None,
                                              "anchor_options": _anchor_options(clause[:changed.start()], day)}, clause)
            else:
                dates = _date_values(clause, day)
                if len(set(dates)) > 1 and not re.search(r"各有|分别|各一场|各参加", clause):
                    yield ("clarify_event", key, None, clause)
                    continue
                if re.search(r"不用|没有|不是|不去|不参加|准备|复习|练习|模拟|想(?:去|参加)|打算(?:去|参加)|后续|还要|还没排", clause):
                    continue
                for value in dict.fromkeys(dates):
                    if value >= day.isoformat():
                        yield ("event", key, {"date": value}, clause)


_OPEN_REQUEST = r"来找你|再找你|回来找你|等我(?:再)?回来|(?:再|重新|下次)?打开(?:晚灯|聊天|这个聊天)|回来(?:聊天|跟你聊|时|的时候)"
_PERMISSION = r"(?:可以|可不可以|能不能|能否|记得|请(?:你)?|到时候)问我(?:一句|一下)?|你问我(?:一句|一下)"
_PROHIBITION = r"(?:不要|别|别再|不用|不要再|不必|(?<!可)不可以|(?<!能)不能|不想让你)(?:主动|再|来)?问我"
_MORNING_TOMORROW = r"明早|明天(?:一早|早上|上午)"
_OPEN_TOPICS = (
    ("meeting", r"开完(?:了)?会|会(?:议)?开完|会议结束", r"(?:我)?(?:讲|说|汇报|分享)(?:得|的)?(?:怎么样|怎样|如何)", "讲得怎么样？"),
    ("interview", r"面试(?:完|结束)|结束面试", r"(?:我)?面试(?:得|的)?(?:怎么样|怎样|如何)", "面试怎么样？"),
    ("presentation", r"汇报(?:讲)?完|汇报结束|做完(?:工作)?汇报", r"(?:我)?(?:工作)?汇报(?:讲)?(?:得|的)?(?:怎么样|怎样|如何)", "汇报讲得怎么样？"),
)


def parse_followup_request(source, local_datetime):
    """Recognize a scoped ask-on-open request, not a reminder or an end detector.

    None means no actionable intent, {} means clarification is needed; a spec
    carries only the literal event and permission, never nearby private topics.
    """
    permission = re.search(_PERMISSION, source)
    if not permission:
        return None
    before = source[:permission.start()]
    after_permission = source[permission.end():]
    # Do not act on language presented as a quote, hypothetical or example.
    if (re.search(ASSUME, before)
            or re.search(_PROHIBITION, before + permission[0])
            or re.search(r"说[：:]\s*$|(?:他说|她说|同事说|朋友说)[：:「“]", before)
            or any(before.count(left) > before.count(right)
                   for left, right in (("“","”"),("「","」"),("『","』"),("‘","’")))
            or before.count('"') % 2):
        return None
    question_fragment = re.split(r"[，。！；、,.!;\n]", after_permission, maxsplit=1)[0]
    question_text = question_fragment.strip().strip('“”‘’「」"')
    if re.search(r"可不可以|能不能", question_text) or re.search(_PROHIBITION, question_text):
        return None
    opening = re.search(_OPEN_REQUEST, before)
    topics = [(key, match, topic_pattern, question)
              for key, event_pattern, topic_pattern, question in _OPEN_TOPICS
              for match in re.finditer(event_pattern, before)]
    if not opening and not topics:
        return None
    if not opening or len(topics) != 1:
        return {}
    key, event_match, topic_pattern, question = topics[0]
    clock_phrase = re.search(_MORNING_TOMORROW, before[:event_match.start()])
    if (not clock_phrase or re.search(UNCERTAIN, before)
            or not re.fullmatch(topic_pattern + r"吗?[？?]?", question_text)):
        return {}
    # Confirm the event and its day are in the same short clause. Later
    # unrelated details do not become part of the stored follow-up evidence.
    between = before[clock_phrase.end():event_match.start()]
    if re.search(r"[。！？；\n]", between):
        # A tightly scoped work-presentation form has a separate dated
        # schedule ("明天上午十点做工作汇报") followed by its completion cue.
        if key != "presentation" or not re.fullmatch(
            r"[^，。！？；\n]{0,16}(?:做|进行)(?:工作)?汇报[。；]\s*(?:等)?我", between
        ):
            return {}
    day = local_datetime.date() + timedelta(days=1)
    checkpoint = datetime.combine(day, time(12)).isoformat(timespec="minutes")
    ack = f"好。明天中午后你回来，我就问一句：{question}"
    # Keep one exact evidence substring, without the user's unrelated clauses.
    quote = (permission[0] + question_fragment).strip()
    return {"key": key, "label": EVENT_NAMES.get(key, "汇报"), "date": day.isoformat(),
            "not_before": checkpoint, "question": question, "ack": ack, "quote": quote}


def save_followup_request(con, spec, user_id, now):
    """Attach one explicit ask to the correlated event, without re-arming it."""
    key, day = spec["key"], spec["date"]
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    rows = con.execute("""SELECT event_id,status,not_before,followup_question,source_id
        FROM events WHERE event_date=? AND (key=? OR key LIKE (? || ':%'))
        ORDER BY event_id""", (day,key,key)).fetchall()
    if len(rows) > 1:
        return "ambiguous"
    if rows:
        event_id, status, not_before, question, source_id = rows[0]
        if source_id > user_id or status != "pending":
            return "closed"
        if (not_before, question) == (spec["not_before"], spec["question"]):
            return "saved"
        con.execute("""UPDATE events SET version=version+1,not_before=?,followup_question=?,
            source_id=?,quote=?,updated_at=? WHERE event_id=? AND status='pending' AND source_id<=?""",
            (spec["not_before"],spec["question"],user_id,spec["quote"],now,event_id,user_id))
        return "saved"
    # A delayed retry must not recreate an event whose later assertion changed
    # its date or resolved it.
    latest = con.execute("""SELECT 1 FROM events WHERE (key=? OR key LIKE (? || ':%'))
        AND source_id>? LIMIT 1""", (key,key,user_id)).fetchone()
    if latest:
        return "closed"
    con.execute("""INSERT INTO events(key,label,event_date,status,source_id,quote,updated_at,
        not_before,followup_question) VALUES (?,?,?,'pending',?,?,?,?,?)""",
        (key,spec["label"],day,user_id,spec["quote"],now,spec["not_before"],spec["question"]))
    return "saved"


def migrate_events(con):
    """Upgrade old one-row-per-key data without committing the caller's transaction."""
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    if columns and "event_id" not in columns:
        con.execute("ALTER TABLE events RENAME TO legacy_events")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        event_id INTEGER PRIMARY KEY, key TEXT NOT NULL, label TEXT NOT NULL,
        event_date TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        source_id INTEGER NOT NULL REFERENCES messages(id), quote TEXT NOT NULL, updated_at TEXT NOT NULL,
        followup_id INTEGER REFERENCES messages(id), not_before TEXT, followup_question TEXT)""")
    if columns and "event_id" not in columns:
        con.execute("""INSERT INTO events(key,label,event_date,status,source_id,quote,updated_at,followup_id)
                       SELECT key,label,event_date,status,source_id,quote,updated_at,followup_id
                       FROM legacy_events ORDER BY key""")
        con.execute("DROP TABLE legacy_events")
    if "event_id" in columns:
        for name in ("not_before", "followup_question"):
            if name not in columns:
                con.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS events_identity ON events(key, event_date)")
    con.execute("CREATE INDEX IF NOT EXISTS events_due ON events(status, event_date)")


def _label(key):
    group, sep, name = key.partition(":")
    return name + EVENT_NAMES.get(group, group) if sep else EVENT_NAMES.get(group, group)


def _key_match(a, b):
    def normalize(key):
        return re.sub(r"\s+|公司(?=$)", "", key).casefold()
    return normalize(a) == normalize(b)


def _ask(matches, key=""):
    if matches:
        items = list(dict.fromkeys(f"{r['label']}（{r['event_date'][5:].replace('-', '月')}日）" for r in matches))
        if len(items) > 1:
            return f"你说的是哪一场？{'，还是'.join(items[:3])}？可以带上那一场再说一下要怎么改。"
    group = EVENT_NAMES.get(key.partition(":")[0], "")
    return f"你说的是哪一场{group}？再说一下要怎么改，带上公司或原日期就好。" if group == "面试" else f"你说的是哪次{group}？再说一下要怎么改，带上场次或原日期就好。"


def apply_event(con, kind, key, value, quote, user_id, now):
    """Apply a supported action or return one clarification without mutating the event."""
    if kind == "clarify_event":
        group = key.partition(":")[0]
        return "我没确定你说的是哪一场、哪一天。能再说具体一点吗？" if not group else _ask([], key)
    if kind not in ("event", "reschedule", "resolve"):
        return None
    data = value if isinstance(value, dict) else {"date": value}
    target = data.get("date")
    anchor = target if kind == "resolve" else data.get("previous_date")
    rows = con.execute("""SELECT event_id,key,label,event_date,status,version,source_id
                          FROM events ORDER BY event_date,event_id""").fetchall()
    columns = ("event_id","key","label","event_date","status","version","source_id")
    rows = [dict(zip(columns, r)) for r in rows]
    if key and ":" in key:
        relevant = [r for r in rows if _key_match(r["key"], key)]
    elif key:
        relevant = [r for r in rows if r["key"].partition(":")[0] == key]
    else:
        relevant = rows
    if kind == "event":
        if not key or not target:
            return None
        exact = [r for r in relevant if _key_match(r["key"], key)]
        if any(r["event_date"] == target or r["source_id"] > user_id for r in exact):
            return None
        # An undifferentiated restatement on the same date is not another event.
        if ":" not in key and any(r["event_date"] == target or r["source_id"] > user_id for r in relevant):
            return None
        con.execute("""INSERT INTO events(key,label,event_date,status,source_id,quote,updated_at)
                       VALUES(?,?,?,'pending',?,?,?)""", (key, _label(key), target, user_id, quote, now))
        return None
    active = [r for r in relevant if r["status"] in ("pending", "asked")]
    anchors = {anchor, *data.get("anchor_options", ())}
    matches = [r for r in relevant if r["event_date"] in anchors and r["status"] in ("pending","asked")] if anchor else active
    if len(matches) != 1:
        if len(matches) > 1:
            return _ask(matches, key)
        if active or (kind == "reschedule" and key):
            return _ask(active, key)
        return None
    event = matches[0]
    if event["source_id"] > user_id or event["status"] not in ("pending","asked"):
        return None
    if kind == "reschedule":
        if not target or target == event["event_date"]:
            return None
        con.execute("""UPDATE events SET event_date=?,status='pending',version=version+1,
                       source_id=?,quote=?,updated_at=?,followup_id=NULL,not_before=NULL,followup_question=NULL
                       WHERE event_id=? AND source_id<=?""",
                    (target, user_id, quote, now, event["event_id"], user_id))
    elif kind == "resolve":
        con.execute("""UPDATE events SET status='resolved',version=version+1,source_id=?,quote=?,updated_at=?
                       WHERE event_id=? AND source_id<=?""", (user_id, quote, now, event["event_id"], user_id))
    return None

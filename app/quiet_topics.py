"""Narrow, temporary conversation boundaries. Raw history is never modified."""
import re
import unicodedata
from datetime import datetime, time, timedelta

QUIET_SCHEMA = """
CREATE TABLE IF NOT EXISTS quiet_topics(
 topic TEXT PRIMARY KEY,
 expires_at INTEGER NOT NULL
);
"""
_POLICY = "用户暂时不想聊被略去的内容。不要猜测、复述或主动追问该话题；正常回应用户其余内容。"
_REDACTED = "（暂时略去的话题）"
_QUOTES = {"“":"”", "‘":"’", "「":"」", "『":"』", "《":"》", "〈":"〉", '"':'"', "'":"'", "`":"`"}
_SPLIT = frozenset("，。！？；,.!?;\r\n")
_DIRECT = re.compile(
    r"^\s*(?:(?:但是|但|不过)\s*)?(?:请\s*)?(?:我\s*)?(?:今天晚上|今天|今晚)\s*(?:就\s*)?(?:请\s*)?"
    r"(?:我们\s*)?(?:先\s*)?(?:不要|别)\s*(?:继续\s*|再\s*)?(?:提起|提(?!醒|供|交|高|前|问|议)|聊起|聊|说起)\s*(.+?)\s*$"
)
_END = re.compile(r"(?:这个话题|那个话题|这件事情|那件事情|这件事|那件事|的事情|的事|这事|那事|的事儿|这事儿|那事儿)(?:了)?$|了$")
_BAD_TOPIC = re.compile(r"(?:什么|那件|这个|那个|这件|怎么办|吗|好不好|行不行|如果|或者|的话|可以吗)$")


def _normalized(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _clauses(text):
    start = 0
    closing = []
    for pos, char in enumerate(text):
        if closing and char == closing[-1]:
            closing.pop()
        elif char in _QUOTES:
            # Apostrophes in plain English are not quotation marks.
            if char == "'" and pos and pos + 1 < len(text) and text[pos-1].isascii() and text[pos+1].isascii() and text[pos-1].isalnum() and text[pos+1].isalnum():
                continue
            closing.append(_QUOTES[char])
        elif not closing and char in _SPLIT:
            yield start, pos, char
            start = pos + 1
    yield start, len(text), ""


def _requests(text):
    antecedent=""
    for start, end, punctuation in _clauses(text):
        clause=text[start:end]
        match = _DIRECT.fullmatch(clause)
        reported=bool(re.search(r"举(?:个)?例|比如|假如|假设|如果|(?:她|他|朋友)说",antecedent))
        antecedent=clause if punctuation in {"，",",","、"} else ""
        if not match or reported or punctuation in {"？", "?"}:
            continue
        topic = match[1].strip()
        for _ in range(2):
            topic = _END.sub("", topic).strip()
            if len(topic) >= 2 and _QUOTES.get(topic[0]) == topic[-1]:
                topic = topic[1:-1].strip()
        if not 1 <= len(topic) <= 32 or _BAD_TOPIC.search(topic) or not all(
            ch.isalnum() or ch in " ·-_" for ch in topic
        ):
            continue
        yield start, end, _normalized(topic)


def remember_quiet_topics(con, source, local_datetime):
    """Remember direct, explicit today/tonight boundaries until local midnight."""
    con.execute("DELETE FROM quiet_topics WHERE expires_at<=?", (int(local_datetime.timestamp()),))
    topics = tuple(dict.fromkeys(topic for _, _, topic in _requests(source)))
    if not topics:
        return topics
    midnight = datetime.combine(local_datetime.date() + timedelta(days=1), time.min, tzinfo=local_datetime.tzinfo)
    expires_at = int(midnight.timestamp())
    con.executemany("""INSERT INTO quiet_topics(topic,expires_at) VALUES(?,?)
        ON CONFLICT(topic) DO UPDATE SET expires_at=MAX(quiet_topics.expires_at,excluded.expires_at)""",
        ((topic, expires_at) for topic in topics))
    return topics


def active_quiet_topics(con, now_datetime):
    return tuple(row[0] for row in con.execute(
        "SELECT topic FROM quiet_topics WHERE expires_at>? ORDER BY length(topic) DESC, topic",
        (now_datetime.timestamp(),)))


def _patterns(topics):
    for topic in sorted(set(topics), key=lambda value: (-len(value), value)):
        needle = _normalized(topic).strip()
        if needle:
            left = r"(?<![a-z0-9])" if needle[0].isascii() and needle[0].isalnum() else ""
            right = r"(?![a-z0-9])" if needle[-1].isascii() and needle[-1].isalnum() else ""
            yield re.compile(left + re.escape(needle) + right)


def contains_quiet_topic(text, topics):
    """Literal matching, not semantic equivalence or synonym inference."""
    if not isinstance(text, str):
        return False
    normalized = _normalized(text)
    return any(pattern.search(normalized) for pattern in _patterns(topics))


def filter_context(context, topics):
    if not topics:
        return context
    patterns = tuple(_patterns(topics))
    def contains(value):
        if isinstance(value, str):
            return any(pattern.search(_normalized(value)) for pattern in patterns)
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains(item) for item in value)
        return False

    result = {**context, "quiet_topic_policy": _POLICY}
    for field in ("facts", "events", "turns"):
        if field in result:
            result[field] = [entry for entry in result[field] if not contains(entry)]
    # assemble() returns a flattened sequence of complete turns; openings are
    # independent assistant messages. Never leave one half of a matching pair.
    if "recent" in result:
        turns = []
        for entry in result["recent"]:
            if isinstance(entry, (list, tuple)):
                turns.append(list(entry))
            elif (isinstance(entry, dict) and entry.get("role") == "assistant"
                  and turns and len(turns[-1]) == 1 and isinstance(turns[-1][0], dict)
                  and turns[-1][0].get("role") == "user"):
                turns[-1].append(entry)
            else:
                turns.append([entry])
        result["recent"] = [entry for turn in turns if not contains(turn) for entry in turn]
    for field in ("history_summary", "previous_summary", "event"):
        if field in result and contains(result[field]):
            if field == "previous_summary":
                result[field] = ""
            else:
                result.pop(field)
    message = result.get("message")
    if isinstance(message, str):
        active = set(topics)
        for start, end, topic in reversed(list(_requests(message))):
            if topic in active:
                message = message[:start] + _REDACTED + message[end:]
        if contains(message):
            message = _normalized(message)
            for pattern in patterns:
                message = pattern.sub(_REDACTED, message)
        result["message"] = message
    return result

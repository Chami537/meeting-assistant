"""Chinese numerals to digits in recognized speech ("inverse text normalization"). Fun-ASR-Nano's own ITN
is partial: one sentence comes back as "2026年…962万元", the next as "二零二五年…四千七百八十六万元", and
drug codes as "幺零零九". Left alone, the minutes model rewrites those codes wrongly (HY009, HY0009).

When in doubt a numeral stays a word: a wrong exact number in the minutes is worse than a spelled-out one.
Converted:
- a run with a unit (十百千) of two characters or more: 四千七百八十六 -> 4786, 一百零五 -> 105,
  一千七 -> 1700;
- a digit-by-digit run of three or more, or of two with 零/幺, zeros kept: 二零二五 -> 2025,
  幺零零九 -> 1009, 零二一 -> 021, 九零后 -> 90后; two digits before 年 (二七年 -> 27年);
- a lone 二…十 before 月 (七月 -> 7月, 十月 -> 10月), or a range of two numbers (十到十三 -> 10到13);
- 百分之八十九 -> 89%; 一点五万 -> 1.5万, 零点五亿 -> 0.5亿 (a decimal before a unit); 一万五千五百 -> 15500.
Kept: approximations and lists (两三年, 四五万, 七八十, 三十五六岁, 十四五, 百八十, 千儿八百, 一二三期,
三三两两, 百分之二三十, 十几); numerals in words (这一块, 千万, 万一, 统一到, 一个, 两月, 四期, 第一, 一季度,
两新一大, 五一, 十一假期, 零零散散, 十二分); clock times (九点零五分); 一万五 and 三万一个月 (a digit after 万
may begin the next word)."""

import re

_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"〇": 0, "幺": 1, "两": 2}
_UNITS = {"十": 10, "百": 100, "千": 1000}
_ZEROS = "零〇"
_N = "零〇一二三四五六七八九幺两十百千"
_D = "零〇一二三四五六七八九幺两"  # digits only, no units
_DECIMAL_UNITS = set("万亿%元块倍吨克亩")  # 一点五万; not 分/个: 九点零五分 is a time, 三点五个 stays
_PERCENT = re.compile(f"百分之([{_N}]+)(?:点([{_D}]+))?")
_WAN = re.compile(f"([{_N}]+)万([{_N}]+)(?![{_N}万亿])")
_DECIMAL = re.compile(f"([{_N}]+)点([{_D}]+)")
_RANGE = re.compile(f"([{_N}]+)([到至])([{_N}]+)")
_RUN = re.compile(f"[{_N}]+")
_IDIOMS = re.compile("(十万八千里|十二分|九九八十一)")  # left untouched
_MONTHS = "二三四五六七八九十"  # 一月 may be 一月一次, 两月 is "two months"
_HOLIDAY = set("假长黄前后之以期")  # 十一假期, 十一之前: National Day, not 11


def _approximate(run: str) -> bool:
    """Two digits side by side are a range (两三年, 七八十, 三十五六); so is 百八十 / 千八百. A digit-by-digit
    number (二零二五) is exact unless its digits count up (两三, 三五, 一二三) or pair up (三三两两)."""
    if run[0] in "百千":
        return True
    pairs = [(a, b) for a, b in zip(run, run[1:], strict=False) if a in _DIGITS and b in _DIGITS]
    if not any(a not in _ZEROS and b not in _ZEROS for a, b in pairs):
        return False
    if any(c in _UNITS for c in run):
        return True
    steps = [_DIGITS[b] - _DIGITS[a] for a, b in zip(run, run[1:], strict=False)]
    if len(run) == 2:
        return steps[0] in (1, 2)
    paired = len(run) == 4 and run[0] == run[1] and run[2] == run[3]
    return paired or all(step == 1 for step in steps)


def _value(run: str) -> str:
    if not any(c in _UNITS for c in run):  # digit by digit, leading zeros kept
        return "".join(str(_DIGITS[c]) for c in run)
    total = cur = last = 0
    for c in run:
        if c in _UNITS:
            total, cur, last = total + (cur or 1) * _UNITS[c], 0, _UNITS[c]
        else:
            cur = _DIGITS[c]
    if cur and last >= 100 and run[-2] in _UNITS:  # 一千七 is 1700, 两百五 is 250
        cur *= last // 10
    return str(total + cur)


def _exact(run: str) -> bool:
    return not _approximate(run) and not set(run) <= set(_ZEROS)


def _is_number(run: str, before: str, after: str) -> bool:
    if not _exact(run) or after == "几" or before in {"点", "儿"}:  # 十几; 三点零五 is a time; 千儿八百
        return False
    if len(run) == 1:
        return run in _MONTHS and after == "月"
    if run == "十一" and (after in _HOLIDAY or before in {"到", "至", "和", "、", "庆"}):
        return False  # 十一假期, 五一到十一, 国庆十一 (a range of two numbers was converted already)
    if any(c in _UNITS for c in run):
        return len(run) > 1
    if len(run) >= 3:
        return True
    return any(c in "零〇幺" for c in run) or after == "年"  # two digits


def _around(m: re.Match) -> tuple[str, str]:
    s = m.string
    return s[m.start() - 1 : m.start()], s[m.end() : m.end() + 1]


def _whole(run: str, fraction: str | None) -> bool:
    """The whole part of a number: 零 only before a fraction (零点五)."""
    return _exact(run) or (run in _ZEROS and fraction is not None)


def _percent(m: re.Match) -> str:
    whole, fraction = m.group(1), m.group(2)
    if not _whole(whole, fraction) or _around(m)[1] == "几":  # 百分之二三十, 百分之十几
        return m.group()
    return f"{_value(whole)}{'.' + _value(fraction) if fraction else ''}%"


def _wan(m: re.Match) -> str:
    """一万五千五百 -> 15500. Not 一万五 or 三万一个月: the digit after 万 may begin the next word."""
    high, low = m.group(1), m.group(2)
    if len(low) < 2 or not any(c in _UNITS for c in low) or not (_exact(high) and _exact(low)):
        return m.group()
    return str(int(_value(high)) * 10000 + int(_value(low)))


def _decimal(m: re.Match) -> str:
    whole, fraction = m.group(1), m.group(2)
    if _around(m)[1] not in _DECIMAL_UNITS or not _whole(whole, fraction):
        return m.group()
    return f"{_value(whole)}.{_value(fraction)}"


def _range_side(run: str) -> bool:
    """A number on its own: not 一 of 统一/万一/唯一, not 五一 (May Day)."""
    if not _exact(run):
        return False
    if any(c in _UNITS for c in run) or len(run) >= 3:
        return True
    if len(run) == 2:
        return any(c in "零〇幺" for c in run)
    return run not in "一两"


def _range(m: re.Match) -> str:
    low, word, high = m.groups()
    if not (_range_side(low) and _range_side(high)) or _around(m)[1] == "几":
        return m.group()
    return f"{_value(low)}{word}{_value(high)}"


def _run(m: re.Match) -> str:
    return _value(m.group()) if _is_number(m.group(), *_around(m)) else m.group()


def itn(text: str) -> str:
    parts = _IDIOMS.split(text)  # odd items are the idioms
    return "".join(part if i % 2 else _convert(part) for i, part in enumerate(parts))


def _convert(text: str) -> str:
    for pattern, convert in ((_PERCENT, _percent), (_WAN, _wan), (_DECIMAL, _decimal), (_RANGE, _range)):
        text = pattern.sub(convert, text)
    return _RUN.sub(_run, text)

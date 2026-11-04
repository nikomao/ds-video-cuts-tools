"""Dependency-free Cue validation, subtitle conversion and timeline editing.

Cue dictionaries require id/start_ms/end_ms/text. Times are finite numbers in
milliseconds, rounded to integers (Python's round); IDs become nonempty strings.
Optional speaker, confidence (0..1), words and unknown metadata are deep-copied.
Word dictionaries use start_ms/end_ms/text and absolute source timeline times.
All intervals are half-open: touching cues are allowed, overlapping cues are not.
Formats are plain-text SRT, WebVTT and ASS; styling is not an internal Cue field.
ASS has 10 ms precision; export rejects intervals collapsed by quantization.
"""

from collections.abc import Mapping
from copy import deepcopy
import math
from numbers import Real
import re
from urllib.parse import quote, unquote

__all__ = ["Cue", "validate_cues", "parse_subtitles", "serialize_subtitles",
           "retime_cues", "qc_cues", "apply_translations", "refine_subtitles"]
Cue = dict


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _ms(value, name):
    return int(round(_number(value, name)))


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("id must be a nonempty string or integer")
    value = str(value).strip()
    if not value or any(c in value for c in "\r\n\x00") or "-->" in value:
        raise ValueError("id must be nonempty and contain no line breaks, NUL or -->")
    return value


def _confidence(item):
    if "confidence" in item:
        confidence = _number(item["confidence"], "confidence")
        if confidence > 1:
            raise ValueError("confidence must be between 0 and 1")


def _cue(raw):
    if not isinstance(raw, Mapping):
        raise ValueError("cue must be a dictionary")
    if not all(key in raw for key in ("id", "start_ms", "end_ms", "text")):
        raise ValueError("cue requires id/start_ms/end_ms/text")
    result = deepcopy(dict(raw))
    result["id"] = _identifier(raw["id"])
    # Check before rounding as well, so negative fractions cannot become zero.
    start = _number(raw["start_ms"], "start_ms")
    end = _number(raw["end_ms"], "end_ms")
    if end <= start:
        raise ValueError("end_ms must be greater than start_ms")
    result["start_ms"], result["end_ms"] = _ms(start, "start_ms"), _ms(end, "end_ms")
    if result["end_ms"] <= result["start_ms"]:
        raise ValueError("cue collapses at millisecond precision")
    if not isinstance(raw["text"], str) or "\x00" in raw["text"]:
        raise ValueError("text must be a string without NUL")
    result["text"] = raw["text"].replace("\r\n", "\n").replace("\r", "\n")
    if "speaker" in raw and raw["speaker"] is not None and not isinstance(raw["speaker"], str):
        raise ValueError("speaker must be a string or None")
    _confidence(result)
    if "words" in raw:
        if not isinstance(raw["words"], (list, tuple)):
            raise ValueError("words must be a list of word dictionaries")
        words = []
        previous_end = result["start_ms"]
        previous_raw_end = start
        for word in raw["words"]:
            if not isinstance(word, Mapping) or not all(k in word for k in ("start_ms", "end_ms", "text")):
                raise ValueError("word requires start_ms/end_ms/text")
            value = deepcopy(dict(word))
            a = _number(word["start_ms"], "word.start_ms")
            b = _number(word["end_ms"], "word.end_ms")
            value["start_ms"], value["end_ms"] = _ms(a, "word.start_ms"), _ms(b, "word.end_ms")
            if b <= a or value["end_ms"] <= value["start_ms"]:
                raise ValueError("word end_ms must be greater than start_ms")
            if (a < previous_raw_end or b > end or
                    value["start_ms"] < previous_end or value["end_ms"] > result["end_ms"]):
                raise ValueError("words must be ordered, non-overlapping and inside their cue")
            if not isinstance(word["text"], str):
                raise ValueError("word text must be a string")
            _confidence(value)
            previous_end = value["end_ms"]
            previous_raw_end = b
            words.append(value)
        result["words"] = words
    return result


def validate_cues(cues, duration_ms=None):
    """Return deep-copied normalized cues; never sort or silently repair inputs.

    Raise ValueError on bad types, missing fields, duplicate normalized IDs,
    invalid intervals, unordered/overlapping cues, or media-duration overflow.
    Empty cue collections are valid. duration_ms must itself be finite and >= 0.
    """
    duration = None if duration_ms is None else _number(duration_ms, "duration_ms")
    if not isinstance(cues, (list, tuple)):
        raise ValueError("cues must be a list or tuple")
    result, seen, previous_end = [], set(), 0
    previous_raw_end = 0
    for index, raw in enumerate(cues):
        try:
            cue = _cue(raw)
            if cue["id"] in seen:
                raise ValueError("duplicate id")
            if raw["start_ms"] < previous_raw_end or cue["start_ms"] < previous_end:
                raise ValueError("cues must be ordered and non-overlapping")
            if duration is not None and max(raw["end_ms"], cue["end_ms"]) > duration:
                raise ValueError("cue exceeds duration_ms")
        except ValueError as exc:
            raise ValueError(f"cue {index}: {exc}") from exc
        seen.add(cue["id"])
        previous_end = cue["end_ms"]
        previous_raw_end = raw["end_ms"]
        result.append(cue)
    return result


def _format(value):
    if not isinstance(value, str) or value.lower() not in ("srt", "vtt", "ass"):
        raise ValueError("format must be srt, vtt or ass")
    return value.lower()


def _timestamp(value, ass=False):
    if ass:
        match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[.](\d{1,3})", value.strip())
    else:
        match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})", value.strip())
    if not match:
        raise ValueError(f"invalid timestamp: {value!r}")
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError(f"invalid timestamp: {value!r}")
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(fraction.ljust(3, "0"))


def _stamp(ms, fmt):
    if fmt == "ass":
        centis = int(round(ms / 10))
        seconds, fraction = divmod(centis, 100)
    else:
        seconds, fraction = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if fmt == "ass":
        return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"
    separator = "," if fmt == "srt" else "."
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{fraction:03}"


def _parse_blocks(text, fmt):
    if fmt == "vtt":
        lines = text.split("\n")
        if not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0]):
            raise ValueError("VTT must start with WEBVTT")
        # Header metadata ends at the first blank line.
        try:
            header_end = lines.index("")
        except ValueError:
            if len(lines) == 1:
                return []
            raise ValueError("VTT header must end with a blank line")
        text = "\n".join(lines[header_end + 1:])
    result = []
    used = set()
    for block in re.split(r"\n(?:[ \t]*\n)+", text.strip("\n")):
        if not block.strip():
            continue
        lines = block.split("\n")
        if fmt == "vtt" and re.match(r"^(NOTE(?:[ \t]|$)|STYLE$|REGION$)", lines[0]):
            continue
        if "-->" in lines[0]:
            cue_id, time_index = None, 0
        else:
            cue_id, time_index = lines[0].strip(), 1
        if len(lines) <= time_index:
            raise ValueError("subtitle block has no timing line")
        timing = re.fullmatch(r"([0-9:.,]+)[ \t]+-->[ \t]+([0-9:.,]+)(?:[ \t]+(.*))?", lines[time_index])
        if not timing or (fmt == "srt" and timing.group(3)):
            raise ValueError(f"invalid timing line: {lines[time_index]!r}")
        if cue_id is None:
            number = len(result) + 1
            while str(number) in used:
                number += 1
            cue_id = str(number)
        used.add(cue_id)
        result.append({"id": cue_id, "start_ms": _timestamp(timing.group(1)),
                       "end_ms": _timestamp(timing.group(2)),
                       "text": "\n".join(lines[time_index + 1:])})
    return result


def _ass_unescape(text):
    output, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            next_char = text[index + 1]
            replacements = {"N": "\n", "n": "\n", "h": " ", "\\": "\\", "{": "{", "}": "}"}
            if next_char in replacements:
                output.append(replacements[next_char])
                index += 2
                continue
        if char == "{":
            end = text.find("}", index + 1)
            if end != -1:
                index = end + 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _ass_escape(text):
    # ASS/libass does not reliably honor backslash-escaped braces or literal
    # backslashes. Fullwidth visual equivalents cannot execute override tags.
    # Exact original text is carried as percent-encoded Effect metadata.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def _parse_ass(text):
    section, fields, result = "", None, []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            continue
        if section != "[events]":
            continue
        if stripped.lower().startswith("format:"):
            fields = [field.strip().lower() for field in stripped.split(":", 1)[1].split(",")]
            if len(fields) != len(set(fields)) or not all(key in fields for key in ("start", "end", "text")) or fields[-1] != "text":
                raise ValueError("ASS event format requires unique fields and Text last")
        elif stripped.lower().startswith("dialogue:"):
            event_fields = fields or ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
            values = line.lstrip().split(":", 1)[1].lstrip().split(",", len(event_fields) - 1)
            if len(values) != len(event_fields):
                raise ValueError("ASS Dialogue field count does not match Format")
            event = dict(zip(event_fields, values))
            effect = event.get("effect", "")
            metadata = effect.split(";cue-text:", 1)
            cue_id = unquote(metadata[0][7:]) if effect.startswith("cue-id:") else str(len(result) + 1)
            original = unquote(metadata[1]) if effect.startswith("cue-id:") and len(metadata) == 2 else _ass_unescape(event["text"])
            cue = {"id": cue_id, "start_ms": _timestamp(event["start"], True),
                   "end_ms": _timestamp(event["end"], True), "text": original}
            if event.get("name"):
                cue["speaker"] = unquote(event["name"])
            result.append(cue)
    if "[events]" not in text.lower():
        raise ValueError("ASS requires an [Events] section")
    return result


def parse_subtitles(text, format):
    """Parse SRT/VTT/ASS to validated cues (BOM and CRLF accepted).

    Missing SRT/VTT identifiers and native ASS events receive sequential IDs.
    VTT settings/notes and ASS styling are deliberately not represented.
    ASS produced here preserves IDs through a cue-id Effect field.
    """
    fmt = _format(format)
    if not isinstance(text, str):
        raise ValueError("subtitle text must be a string")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues = _parse_ass(text) if fmt == "ass" else _parse_blocks(text, fmt)
    return validate_cues(cues)


def serialize_subtitles(cues, format):
    """Export validated cues; SRT/VTT retain exact ms, ASS rounds to 10 ms.

    SRT emits numeric 1..N sequence numbers for FFmpeg/player compatibility;
    true IDs stay in Cue JSON. VTT/ASS preserve IDs. Text survives round-trip
    (ASS times must be 10-ms aligned for exact timing); speaker survives ASS.
    Other metadata is not serialized.
    SRT/VTT reject internal blank lines, which would split subtitle blocks.
    ASS literal braces/backslashes render as fullwidth visual equivalents to
    prevent libass tag injection; exact text is retained in Effect metadata.
    """
    fmt = _format(format)
    values = validate_cues(cues)
    if fmt == "ass":
        quantized = [{**cue, "start_ms": int(round(cue["start_ms"] / 10)) * 10,
                      "end_ms": int(round(cue["end_ms"] / 10)) * 10} for cue in values]
        # Word precision is independent of the exported event precision.
        validate_cues([{key: value for key, value in cue.items() if key != "words"} for cue in quantized])
        header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
                  "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                  "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,40,1\n\n"
                  "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        rows = []
        for cue in values:
            speaker = quote(cue.get("speaker") or "", safe="")
            identity = quote(cue["id"], safe="")
            if any(char in cue["text"] for char in "\\{}"):
                identity += ";cue-text:" + quote(cue["text"], safe="")
            rows.append(f"Dialogue: 0,{_stamp(cue['start_ms'], fmt)},{_stamp(cue['end_ms'], fmt)},Default,{speaker},0,0,0,cue-id:{identity},{_ass_escape(cue['text'])}")
        return header + "\n".join(rows) + ("\n" if rows else "")
    blocks = []
    for sequence, cue in enumerate(values, 1):
        if re.search(r"\n[ \t]*\n", cue["text"]) or cue["text"].startswith("\n") or cue["text"].endswith("\n"):
            raise ValueError("SRT/VTT cue text cannot contain leading, trailing or blank lines")
        if fmt == "vtt" and (re.match(r"^(NOTE(?:[ \t]|$)|STYLE$|REGION$)", cue["id"])):
            raise ValueError("VTT id is reserved for a metadata block")
        identity = str(sequence) if fmt == "srt" else cue["id"]
        blocks.append(f"{identity}\n{_stamp(cue['start_ms'], fmt)} --> {_stamp(cue['end_ms'], fmt)}\n{cue['text']}")
    body = "\n\n".join(blocks) + ("\n" if blocks else "")
    return ("WEBVTT\n\n" if fmt == "vtt" else "") + body


def retime_cues(cues, segments):
    """Concatenate sourceIn/sourceOut second intervals in supplied order.

    Segments may reorder/repeat source ranges. Intersections are clipped, not
    stretched; text is retained whole, words are intersected and remapped too.
    A once-used cue keeps its ID. Repeated/split occurrences get collision-safe
    '~2', '~3', ... suffixes. Input objects are never changed.
    """
    values = validate_cues(cues)
    if not isinstance(segments, (list, tuple)):
        raise ValueError("segments must be a list or tuple")
    intervals = []
    for segment in segments:
        if not isinstance(segment, Mapping) or not all(k in segment for k in ("sourceIn", "sourceOut")):
            raise ValueError("segment requires sourceIn/sourceOut in seconds")
        a, b = _number(segment["sourceIn"], "sourceIn"), _number(segment["sourceOut"], "sourceOut")
        if b <= a:
            raise ValueError("sourceOut must be greater than sourceIn")
        start, end = _ms(a * 1000, "sourceIn"), _ms(b * 1000, "sourceOut")
        if end <= start:
            raise ValueError("segment collapses at millisecond precision")
        intervals.append((start, end))
    result, used = [], set()
    reserved = {cue["id"] for cue in values}
    offset = 0
    for start, end in intervals:
        for cue in values:
            a, b = max(start, cue["start_ms"]), min(end, cue["end_ms"])
            if a >= b:
                continue
            item = deepcopy(cue)
            identity, suffix = cue["id"], 2
            if identity in used:
                while f"{identity}~{suffix}" in used or f"{identity}~{suffix}" in reserved:
                    suffix += 1
                identity = f"{identity}~{suffix}"
            item.update(id=identity, start_ms=offset + a - start, end_ms=offset + b - start)
            used.add(identity)
            if "words" in item:
                words = []
                for word in item["words"]:
                    x, y = max(a, word["start_ms"]), min(b, word["end_ms"])
                    if x < y:
                        word.update(start_ms=offset + x - start, end_ms=offset + y - start)
                        words.append(word)
                item["words"] = words
            result.append(item)
        offset += end - start
    return validate_cues(result, offset)


def qc_cues(cues, duration_ms=None):
    """Return issue dictionaries; never repair or raise for invalid Cue data.

    Each issue contains code/severity/message/cue_id/index. Structural errors
    are severity='error'; editorial heuristics are 'warning': empty_text,
    short_duration (<500ms), long_duration (>7000ms), reading_speed (>20
    non-whitespace chars/s), line_count (>2), line_length (>42), low_confidence
    (<0.6). Structural codes: invalid_cues, invalid_duration, invalid_cue,
    duplicate_id, overlap, out_of_order, out_of_bounds.
    """
    issues = []
    def add(code, message, index=None, cue_id=None, severity="error"):
        issues.append({"code": code, "severity": severity, "message": message,
                       "cue_id": cue_id, "index": index})
    duration = None
    if duration_ms is not None:
        try:
            duration = _number(duration_ms, "duration_ms")
        except ValueError as exc:
            add("invalid_duration", str(exc))
    if not isinstance(cues, (list, tuple)):
        add("invalid_cues", "cues must be a list or tuple")
        return issues
    seen, previous_start, previous_end = set(), -1, 0
    previous_raw_start, previous_raw_end = -1, 0
    for index, raw in enumerate(cues):
        try:
            cue = _cue(raw)
        except ValueError as exc:
            add("invalid_cue", str(exc), index)
            continue
        identity = cue["id"]
        if identity in seen:
            add("duplicate_id", "Cue ID is not unique", index, identity)
        seen.add(identity)
        if cue["start_ms"] < previous_start or raw["start_ms"] < previous_raw_start:
            add("out_of_order", "Cue starts before preceding cue", index, identity)
        if cue["start_ms"] < previous_end or raw["start_ms"] < previous_raw_end:
            add("overlap", "Cue overlaps an earlier cue", index, identity)
        previous_start, previous_end = cue["start_ms"], max(previous_end, cue["end_ms"])
        previous_raw_start, previous_raw_end = raw["start_ms"], max(previous_raw_end, raw["end_ms"])
        if duration is not None and max(raw["end_ms"], cue["end_ms"]) > duration:
            add("out_of_bounds", "Cue exceeds media duration", index, identity)
        text, length = cue["text"], cue["end_ms"] - cue["start_ms"]
        warnings = []
        if not text.strip():
            warnings.append(("empty_text", "Cue text is empty"))
        if length < 500:
            warnings.append(("short_duration", "Cue lasts less than 500 ms"))
        if length > 7000:
            warnings.append(("long_duration", "Cue lasts more than 7000 ms"))
        if len(re.sub(r"\s", "", text)) * 1000 / length > 20:
            warnings.append(("reading_speed", "Reading speed exceeds 20 characters/second"))
        if len(text.split("\n")) > 2:
            warnings.append(("line_count", "Cue has more than two lines"))
        if any(len(line) > 42 for line in text.split("\n")):
            warnings.append(("line_length", "A line exceeds 42 characters"))
        if cue.get("confidence", 1) < 0.6 or any(word.get("confidence", 1) < 0.6 for word in cue.get("words", [])):
            warnings.append(("low_confidence", "Cue or word confidence is below 0.6"))
        for code, message in warnings:
            add(code, message, index, identity, "warning")
    return issues


def apply_translations(cues, translations):
    """Replace text using a complete one-to-one ID mapping, preserving metadata.

    translations is {id: text} or a list of {id, text} dictionaries. Missing,
    extra, duplicate IDs and non-string text raise ValueError. Timing fields
    supplied in translation records are rejected if they differ from source.
    Word metadata remains source-language alignment; it is not translated.
    """
    values = validate_cues(cues)
    if isinstance(translations, Mapping):
        records = [{"id": identity, "text": text} for identity, text in translations.items()]
    elif isinstance(translations, (list, tuple)):
        records = translations
    else:
        raise ValueError("translations must be an id-to-text mapping or a list")
    by_id = {}
    source = {cue["id"]: cue for cue in values}
    for record in records:
        if not isinstance(record, Mapping) or "id" not in record or "text" not in record:
            raise ValueError("translation requires id/text")
        identity = _identifier(record["id"])
        if identity in by_id:
            raise ValueError("duplicate translation id")
        if identity not in source:
            raise ValueError("unknown translation id")
        if not isinstance(record["text"], str):
            raise ValueError("translation text must be a string")
        for field in ("start_ms", "end_ms"):
            if field in record and _number(record[field], field) != source[identity][field]:
                raise ValueError("translations must not change cue timing")
        by_id[identity] = record["text"]
    if set(by_id) != set(source):
        raise ValueError("translations must contain every cue id exactly once")
    for cue in values:
        cue["text"] = by_id[cue["id"]]
    return validate_cues(values)


def refine_subtitles(cues: list, max_chars=20, split_punctuations="，。！？；,!?;") -> list[dict]:
    """Refine long subtitle cues into readable chunks for mobile and standard viewing.

    Splits cues exceeding max_chars by punctuation or word boundaries. If word-level
    timestamps exist, uses exact word bounds; otherwise applies monotonic linear
    interpolation. Output cues are guaranteed valid and non-overlapping.
    """
    values = validate_cues(cues)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 6 <= max_chars <= 200:
        raise ValueError("max_chars must be an integer between 6 and 200")
    if not isinstance(split_punctuations, str):
        raise ValueError("split_punctuations must be a string of punctuation characters")

    punct_set = set(split_punctuations)
    refined = []

    for cue in values:
        text = str(cue.get("text", "")).strip()
        words = cue.get("words")
        start_ms, end_ms = cue["start_ms"], cue["end_ms"]
        duration = end_ms - start_ms

        # If already short and doesn't end with internal strong break, keep as is
        if len(text) <= max_chars:
            refined.append(deepcopy(cue))
            continue

        # Strategy 1: Word-level splitting
        if isinstance(words, list) and words and len(words) > 1:
            chunks = []
            curr_chunk = []
            curr_len = 0

            for w in words:
                w_text = str(w.get("text", ""))
                curr_chunk.append(w)
                curr_len += len(w_text.strip())

                has_punct = any(ch in punct_set for ch in w_text)
                if curr_len >= max_chars or (has_punct and curr_len >= max_chars // 2):
                    chunks.append(curr_chunk)
                    curr_chunk = []
                    curr_len = 0

            if curr_chunk:
                if chunks and curr_len < 3:
                    # Merge very short trailing word into previous chunk
                    chunks[-1].extend(curr_chunk)
                else:
                    chunks.append(curr_chunk)

            if len(chunks) <= 1:
                refined.append(deepcopy(cue))
                continue

            last_end = start_ms
            for idx, chunk in enumerate(chunks, 1):
                chunk_start = max(last_end, int(round(chunk[0]["start_ms"])))
                chunk_end = max(chunk_start + 10, int(round(chunk[-1]["end_ms"])))
                chunk_end = min(end_ms, chunk_end)
                if chunk_end <= chunk_start:
                    chunk_end = chunk_start + 10

                chunk_text = " ".join(w.get("text", "").strip() for w in chunk).strip()
                new_cue = {
                    "id": f"{cue['id']}_{idx}",
                    "start_ms": chunk_start,
                    "end_ms": chunk_end,
                    "text": chunk_text,
                    "words": deepcopy(chunk)
                }
                for meta_key in ("speaker", "confidence"):
                    if meta_key in cue:
                        new_cue[meta_key] = cue[meta_key]
                refined.append(new_cue)
                last_end = chunk_end
            continue

        # Strategy 2: Text character / punctuation interpolation
        # Split text into tokens by punctuation or spaces
        parts = []
        cur_part = ""
        for ch in text:
            cur_part += ch
            if ch in punct_set or len(cur_part) >= max_chars:
                parts.append(cur_part.strip())
                cur_part = ""
        if cur_part.strip():
            if parts and len(cur_part.strip()) < 3:
                parts[-1] += cur_part
            else:
                parts.append(cur_part.strip())

        parts = [p for p in parts if p]
        if len(parts) <= 1:
            refined.append(deepcopy(cue))
            continue

        total_chars = max(1, sum(len(p) for p in parts))
        last_ms = start_ms

        for idx, part in enumerate(parts, 1):
            fraction = len(part) / total_chars
            part_dur = max(20, int(round(duration * fraction)))
            if idx == len(parts):
                part_end = end_ms
            else:
                part_end = min(end_ms - 10 * (len(parts) - idx), last_ms + part_dur)
            if part_end <= last_ms:
                part_end = last_ms + 10

            new_cue = {
                "id": f"{cue['id']}_{idx}",
                "start_ms": last_ms,
                "end_ms": part_end,
                "text": part
            }
            for meta_key in ("speaker", "confidence"):
                if meta_key in cue:
                    new_cue[meta_key] = cue[meta_key]
            refined.append(new_cue)
            last_ms = part_end

    return validate_cues(refined)

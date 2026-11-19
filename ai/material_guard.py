"""Identity checks shared by snippets, fetched pages and cached context."""
import re
from ai.result_kind import normalize_entity


def contains_entity(text, entity):
    if not entity:
        return False
    if re.fullmatch(r'[A-Za-z0-9]+', entity.strip()):
        return bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(entity.strip()) + r'(?![A-Za-z0-9])', text, re.I))
    return normalize_entity(entity) in normalize_entity(text)


def primary_artist(artist):
    # Split catalogue credit separators, not AC/DC or Simon & Garfunkel.
    return re.split(r'\s+/\s+|[,，、]|\s+feat\.?\s+', artist or '', maxsplit=1, flags=re.I)[0].strip()


def song_identity(text, title, artist):
    if not contains_entity(text, title):
        return False
    if not artist:
        return bool(re.search(r'歌曲|专辑|演唱|单曲|作曲|\bsong\b|\balbum\b|\brecording\b', text, re.I))
    return contains_entity(text, artist) or contains_entity(text, primary_artist(artist))


def context_blocks(text):
    text = (text or '').replace('【词条正文】', '').strip()
    return [p.strip() for p in re.split(r'(?m)(?=^\[\d+\] |^- )', text) if p.strip()]


def verified_context(text, title, artist, album=''):
    kept = []
    for part in context_blocks(text):
        if not song_identity(part, title, artist):
            continue
        # A platform item describing another release is not evidence for this release.
        albums = re.findall(r'收录于(?:其)?(?:专辑)?《([^》]+)》', part)
        albums += re.findall(r'所属专辑[：:]\s*([^。\n；;]+)',part)
        if album and albums and not any(normalize_entity(a) == normalize_entity(album) for a in albums):
            continue
        part = re.sub(r'^(?:\[\d+\]\s*|-\s*)', '', part)
        kept.append(f'[{len(kept)+1}] {part}')
    return '\n'.join(kept)


def environment_context(text, city):
    kept=[]
    for part in context_blocks(text):
        if city and not contains_entity(part, city):
            continue
        if re.search(r'权责|行政权力|印发|政务公开|国土空间总体规划|政府.*通知',part):
            continue
        if not re.search(r'街|巷|林|湖|河|公园|演出|音乐|唱片|剧院|市集|花期|夜市',part):
            continue
        kept.append(part)
    return '\n'.join(kept)

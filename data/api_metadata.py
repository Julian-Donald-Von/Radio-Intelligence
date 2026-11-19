"""Small, source-scoped metadata. Album facts are never song-level claims."""
import json
from datetime import datetime, timezone, timedelta


def decode(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def merge(old, new):
    result = decode(old)
    for key, value in decode(new).items():
        if value not in (None, '', [], {}):
            result[key] = value
    return result


def date_from_timestamp(value, milliseconds=False):
    try:
        value = float(value)
        if value <= 0:
            return None
        # Both platforms encode catalogue dates at China Standard Time midnight.
        # UTC conversion alone moves these dates to the previous calendar day.
        return datetime.fromtimestamp(value / (1000 if milliseconds else 1),
                                      timezone(timedelta(hours=8))).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def fact(value, source, scope, release_id):
    return {'value': value, 'source': source, 'scope': scope, 'release_id': str(release_id)}


def netease_metadata(song):
    album = song.get('al') or song.get('album') or {}
    result = {'album_id': str(album['id'])} if isinstance(album, dict) and album.get('id') else {}
    date = date_from_timestamp(song.get('publishTime'), milliseconds=True)
    if date:
        result['release_date'] = fact(date, 'netease:song_detail.publishTime', 'release', song.get('id'))
    return result


def qq_metadata(song):
    album = song.get('album') or {}
    mid = song.get('albummid') or (album.get('mid') if isinstance(album, dict) else '')
    result = {'album_id': str(mid)} if mid else {}
    date = date_from_timestamp(song.get('pubtime'))
    if date:
        result['release_date'] = fact(date, 'qqmusic:search.pubtime', 'release', song.get('songmid') or song.get('mid'))
    return result


def netease_album_metadata(payload, album_id, song_id):
    """An album ID and member ID must both match before accepting its prose."""
    album = payload.get('album') or {}
    songs = payload.get('songs') or []
    if str(album.get('id')) != str(album_id) or not any(str(s.get('id')) == str(song_id) for s in songs):
        return {}
    result = {'album_title': fact(album.get('name'), 'netease:album.name', 'album', album_id)}
    for key, value in [('album_date', date_from_timestamp(album.get('publishTime'), True)),
                       ('album_company', album.get('company'))]:
        if value:
            result[key] = fact(value, 'netease:album', 'album', album_id)
    description = (album.get('description') or album.get('briefDesc') or '').strip()
    if description:
        # The returned member list, not a guessed title, establishes single scope.
        scope = 'song' if len(songs) == 1 and album.get('size') == 1 else 'album'
        result['album_description'] = fact(description, 'netease:album.description', scope, album_id)
    return result


def background_facts(metadata, limit=1200, title=''):
    """Keep provenance and scope attached; this text is evidence, not instructions."""
    md = decode(metadata)
    item = md.get('album_description') or {}
    if not isinstance(item, dict) or not item.get('source') or not item.get('value'):
        return ''
    album_title = (md.get('album_title') or {}).get('value', '')
    scope = '当前歌曲的单曲专辑简介' if item.get('scope') == 'song' else '整张专辑简介，不代表每首歌曲'
    prose = str(item['value']).split('【著作权人声明')[0].strip()
    if title and item.get('scope') != 'song':
        import re
        from ai.material_guard import contains_entity
        sentences=re.split(r'(?<=[。！？])\s*|(?<=\.)\s+(?=[A-Z])',prose)
        targeted=[s.strip() for s in sentences if contains_entity(s,title)]
        if targeted:
            prose='\n'.join(targeted)
            scope='专辑评论中明确提及当前歌曲的原句；听感属于该评论的描述'
    # Prefer complete sentences over cutting an attribution or comparison in half.
    if len(prose) > limit:
        cut=max(prose.rfind(x,0,limit) for x in ('。','. ','！','\n'))
        prose=prose[:cut+1] if cut>limit//3 else prose[:limit]
    return f"范围：{scope}；专辑：{album_title}；来源：{item['source']}\n{prose}"


def prompt_facts(metadata):
    """Only compact verified API fields; never dump album promotional prose."""
    labels = {'release_date': '当前平台版本发行日期（不是歌曲首发日期）',
              'album_date': '当前专辑发行日期（不是歌曲首发日期）',
              'album_genre': '专辑分类（不能推出每首歌的曲风或编曲）',
              'album_company': '专辑发行公司', 'album_language': '专辑语言'}
    lines = []
    for key, label in labels.items():
        entry = decode(metadata).get(key)
        if isinstance(entry, dict) and entry.get('value') and entry.get('source'):
            lines.append(f"{label}：{entry['value']}；来源：{entry['source']}")
    return '\n'.join(lines)

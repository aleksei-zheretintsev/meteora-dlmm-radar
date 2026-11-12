# -*- coding: utf-8 -*-
"""Meteora DLMM Radar v46 SOCIAL LINKS + FRESH TOP WALLET TWO PANELS · TOP WALLET + X FOLLOWERS (based on verified v38) STANDALONE = v30 + fix for duplicate cards / dead Update button + real Bin Rent.
One Python file: original v11 monitoring + approved v18 card + v20 GMGN fix.
No imports of radar_v*.py. Existing .env and radar.sqlite3 remain in place.
Install: pip install requests python-dotenv pillow
Run: python radar_v32.py
V39 fixed: 30m source for both buttons; immutable original; InsightX cluster MAX/OTHER.
"""
"""Meteora DLMM Radar v11: GMGN full info, compact card, polling, SQLite (v11.0.2).
Install: pip install requests python-dotenv pillow
Run: python radar.py ; test: python radar.py --test
"""
import argparse
import html
import io
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
API = 'https://dlmm.datapi.meteora.ag'
DISCOVERY_API = 'https://pool-discovery-api.datapi.meteora.ag/pools'
INTERVAL = max(5, int(os.getenv('POLL_SECONDS', '10')))
PAGES = max(1, min(10, int(os.getenv('SCAN_PAGES', '2'))))
PAGE_SIZE = max(10, min(1000, int(os.getenv('PAGE_SIZE', '500'))))  # .env may override
MIN_MC = float(os.getenv('MIN_MARKET_CAP_USD', '150000'))
MIN_BASE_FEE = float(os.getenv('MIN_BASE_FEE_PCT', '0.8'))
MIN_FEES_30M = float(os.getenv('MIN_FEES_30M_USD', '10'))
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT = os.getenv('TELEGRAM_CHAT_ID', '').strip()
DB = ROOT / 'radar.sqlite3'
WATCH_MINUTES = max(30, int(os.getenv('WATCH_MINUTES', '180')))
DETAIL_BUDGET = max(1, int(os.getenv('DETAIL_BUDGET', '20')))
STARTUP_GRACE = max(1, int(os.getenv('STARTUP_GRACE_SECONDS', '60')))
NEW_POOL_MAX_AGE_MINUTES = max(1, int(os.getenv('NEW_POOL_MAX_AGE_MINUTES', '3')))
LIST_TIMEOUT_SECONDS = max(3, int(os.getenv('METEORA_LIST_TIMEOUT_SECONDS', '8')))
# Automatic fallback is partial: Discovery 'top' is not a complete newest-pools feed.
# v28: default 'both' = Discovery (rich 30m metrics) + small legacy page (complete newest DLMM list).
LIST_SOURCE = os.getenv('METEORA_LIST_SOURCE', 'both').strip().lower()
if LIST_SOURCE not in ('auto', 'legacy', 'discovery', 'both'):
    LIST_SOURCE = 'both'
if LIST_SOURCE == 'auto':
    LIST_SOURCE = 'both'
LEGACY_PAGE_SIZE = max(10, min(200, int(os.getenv('LEGACY_PAGE_SIZE', '100'))))  # one small page, no more 500x2 timeouts
DISCOVERY_FILTER_BY = os.getenv('DISCOVERY_FILTER_BY', 'pool_type=dlmm').strip()  # '' disables the server-side filter
LATE_LOG_MINUTES = max(1, int(os.getenv('LATE_LOG_MINUTES', '180')))  # log DLMM pools baselined for being 'too old' on first sight
# v29: freshness is a BUSINESS window (fees are earned in the first 1-2 h), not a 3-minute detection window.
# A pool first seen older than the window becomes 'baseline'; a younger one is tracked and sent once it passes filters.
SIGNAL_MAX_AGE_MINUTES = max(1, int(os.getenv('SIGNAL_MAX_AGE_MINUTES', '60')))
SEND_MAX_AGE_MINUTES = max(SIGNAL_MAX_AGE_MINUTES, int(os.getenv('SEND_MAX_AGE_MINUTES', '120')))  # never send/track beyond this
MAX_SENDS_PER_SCAN = max(1, int(os.getenv('MAX_SENDS_PER_SCAN', '5')))  # flood guard after a restart; rest stay 'pending'


SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'Meteora-DLMM-Radar/1.0'})
GMGN_URL = 'https://gmgn.ai/mrwapi/v1/multi_token_full_info'
GMGN_ENABLED = os.getenv('GMGN_ENABLED', '1').strip().lower() not in ('0', 'false', 'no')


def num(value):
    try:
        if value is None or value == '': return None
        return float(value)
    except (TypeError, ValueError): return None


def at(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict): return None
        obj = obj.get(k)
    return obj


def first_num(obj, paths):
    for path in paths:
        v = num(at(obj, *path))
        if v is not None: return v
    return None


def window(obj, key, span='30m'):
    # Strictly use a matching window, never mislabel 24h or cumulative metrics.
    aliases = {'30m': ('30m', '30min', '30_min', 'm30'), '5m': ('5m','5min'), '1h': ('1h','60m','h1')}
    candidates = [(key, w) for w in aliases.get(span, (span,))]
    candidates += [(f'{key}_{w}',) for w in aliases.get(span, (span,))]
    return first_num(obj, candidates)


def addr(pool): return pool.get('address') or pool.get('pool_address')


def pos(v):
    # 0 / negative / None from an API means "no data", never a real market cap or base fee.
    return v if v is not None and v > 0 else None


def merge_pool(base, top):
    """Deep merge: `top` (Discovery) wins, but None never erases data and a 0 market cap never erases a valid one."""
    out = dict(base)
    for k, v in top.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_pool(out[k], v)
        elif k in ('market_cap', 'market_cap_usd') and num(v) is not None and num(v) <= 0 and pos(num(out.get(k))):
            continue
        else:
            out[k] = v
    return out


def best_fees30(pool):
    # Same metric from two services: take the larger non-null value so a lagging/zero snapshot cannot hide real fees.
    vals = [first_num(pool.get('_discovery') or {}, [('fee',)]), window(pool, 'fees', '30m')]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def is_dlmm(pool):
    # Legacy DLMM API items have no pool_type -> DLMM. Discovery items carry pool_type.
    return str(pool.get('pool_type') or 'dlmm').lower() == 'dlmm'


def symbol(pool):
    x = at(pool, 'token_x', 'symbol') or 'TOKEN'
    y = at(pool, 'token_y', 'symbol') or 'SOL'
    return f'{x}-{y}'


def token_mint(pool):
    # If token X is SOL/WSOL, select Y instead.
    x = pool.get('token_x') or {}; y = pool.get('token_y') or {}
    sol = {'SOL', 'WSOL'}
    chosen = y if str(x.get('symbol','')).upper() in sol else x
    for k in ('address','mint','mint_address','token_address'):
        if chosen.get(k): return str(chosen[k])
    return None


def market_cap(pool):
    # Only explicit market cap fields; never treat FDV or TVL as MC.
    # Market cap must belong to the non-SOL token; never take SOL market cap.
    x = pool.get('token_x') or {}
    y = pool.get('token_y') or {}
    base = y if str(x.get('symbol', '')).upper() in ('SOL', 'WSOL') else x
    return (pos(first_num(base, [('market_cap',), ('market_cap_usd',)]))
            or pos(first_num(pool, [('market_cap',), ('market_cap_usd',)])))


def age_minutes(timestamp):
    if timestamp is None: return None
    try:
        if isinstance(timestamp, (int,float)) or str(timestamp).replace('.','',1).isdigit():
            value = float(timestamp)
            if value > 1e12: value /= 1000
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(timestamp).replace('Z','+00:00'))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc)-dt).total_seconds()/60))
    except (ValueError, OverflowError, OSError, TypeError): return None


def human_age(m):
    if m is None: return 'н/д'
    if m < 60: return f'{m}m'
    if m < 1440: return f'{m//60}h {m%60}m'
    return f'{m//1440}d'


def money(v):
    if v is None: return 'н/д'
    if abs(v)>=1e9: return f'${v/1e9:,.2f}B'
    if abs(v)>=1e6: return f'${v/1e6:,.2f}M'
    if abs(v)>=1e3: return f'${v/1e3:,.1f}K'
    return f'${v:,.2f}'


def count(v): return 'н/д' if v is None else f'{v:,.0f}'.replace(',', ' ')

def percent(v): return 'н/д' if v is None else f'{v:,.1f}%'


def volatility_30m(pool):
    """30m high-low price range / low * 100, only from explicitly 30m OHLC data.

    A price-change percentage is NOT volatility. Missing OHLC -> None.
    """
    paths = [
        (('ohlc', '30m', 'high'), ('ohlc', '30m', 'low')),
        (('ohlcv', '30m', 'high'), ('ohlcv', '30m', 'low')),
        (('price_high_30m',), ('price_low_30m',)),
        (('high_30m',), ('low_30m',)),
    ]
    for high_path, low_path in paths:
        hi, lo = num(at(pool, *high_path)), num(at(pool, *low_path))
        if hi is not None and lo is not None and lo > 0 and hi >= lo:
            return (hi - lo) / lo * 100
    return None


def metrics(pool):
    conf = pool.get('pool_config') or {}
    discovery = pool.get('_discovery') or {}
    x = pool.get('token_x') or {}
    y = pool.get('token_y') or {}
    selected = y if str(x.get('symbol', '')).upper() in ('SOL', 'WSOL') else x
    # Discovery figures are for the explicitly requested 30-minute timeframe.
    # Do not use old REST snapshots to overwrite them.
    tvl = first_num(discovery, [('tvl',)])
    if tvl is None: tvl = first_num(pool, [('tvl',), ('tvl_usd',)])
    active = first_num(discovery, [('active_tvl',)])
    if active is None: active = first_num(pool, [('active_tvl',), ('active_tvl_usd',)])
    fees = first_num(discovery, [('fee',)])
    if fees is None: fees = window(pool, 'fees', '30m')
    volume = first_num(discovery, [('volume',)])
    if volume is None: volume = window(pool, 'volume', '30m')
    fee_ratio = first_num(discovery, [('fee_active_tvl_ratio',)])
    if fee_ratio is None and active and active > 0 and fees is not None:
        fee_ratio = fees / active * 100
    volume_ratio = first_num(discovery, [('volume_active_tvl_ratio',)])
    if volume_ratio is None and active and active > 0 and volume is not None:
        volume_ratio = volume / active * 100
    fee_pct = first_num(discovery, [('fee_pct',)])
    if fee_pct is None: fee_pct = first_num(conf, [('base_fee_pct',)])
    bin_step = at(discovery, 'dlmm_params', 'bin_step') or conf.get('bin_step') or pool.get('bin_step')
    swaps = first_num(discovery, [('swap_count',)])
    if swaps is None: swaps = window(pool, 'swaps', '30m')
    traders = first_num(discovery, [('unique_traders',)])
    if traders is None: traders = window(pool, 'traders', '30m')
    holders = first_num(discovery, [('base_token_holders',)])
    if holders is None: holders = first_num(selected, [('holders',)])
    if holders is None: holders = first_num(pool, [('holders',), ('holder_count',), ('_gmgn','holder_count')])
    top10 = first_num(discovery, [('token_x','top_holders_pct')]) if selected is x else first_num(discovery, [('token_y','top_holders_pct')])
    if top10 is None: top10 = first_num(selected, [('top_holders_pct',)])
    if top10 is None: top10 = first_num(pool, [('top10_pct',), ('top_10_pct',)])
    if top10 is None:
        gmgn_top10 = num(at(pool, '_gmgn', 'top_10_holder_rate'))
        if gmgn_top10 is not None: top10 = gmgn_top10 * 100
    lps = first_num(discovery, [('total_lps',)])
    if lps is None: lps = first_num(pool, [('total_lps',), ('lp_count',)])
    vol = first_num(discovery, [('volatility',)])
    if vol is None: vol = volatility_30m(pool)
    return {
        'name':symbol(pool), 'mc':market_cap(discovery) if discovery else market_cap(pool),
        'fee':fee_pct, 'bin':bin_step,
        'token_age':human_age(age_minutes(at(pool,'_gmgn','creation_timestamp') or selected.get('created_at') or selected.get('creation_time'))),
        'pool_age':human_age(age_minutes(pool.get('pool_created_at') or pool.get('created_at') or discovery.get('pool_created_at'))),
        'tvl':money(tvl), 'active':money(active), 'fees':money(fees), 'volume':money(volume),
        'fee_ratio':percent(fee_ratio), 'vol_ratio':percent(volume_ratio),
        'swaps':count(swaps), 'traders':count(traders), 'holders':count(holders),
        'top10':percent(top10), 'lps':count(lps), 'volatility':percent(vol),
        'gmgn_views':count(num(at(pool,'_gmgn','visiting_count'))),
        'gmgn_total_fees':(f"{num(at(pool,'_gmgn','total_fee')):,.0f} SOL" if num(at(pool,'_gmgn','total_fee')) is not None else 'н/д'),
    }


def fetch_gmgn(mints):
    """Fetch GMGN token metadata in one batch; return mapping by token mint.

    This is a browser-observed endpoint, not a guaranteed public API contract.
    Never forward browser cookies/device identifiers or credentials.
    """
    if not GMGN_ENABLED: return {}
    addresses = list(dict.fromkeys(str(m) for m in mints if m))[:20]
    if not addresses: return {}
    try:
        response = requests.post(
            GMGN_URL,
            json={'chain': 'sol', 'addresses': addresses},
            headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                     'User-Agent': 'Meteora-DLMM-Radar-Test/1.0'},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('code') != 0 or not isinstance(payload.get('data'), list):
            raise ValueError('GMGN вернул неожиданный формат или код ошибки')
        return {str(item['address']): item for item in payload['data']
                if isinstance(item, dict) and item.get('address')}
    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.warning('GMGN недоступен: %s; показатели GMGN будут н/д (веб-эндпоинт неофициальный)', exc)
        return {}


def enrich_gmgn(pool):
    mint = token_mint(pool)
    if not mint: return pool
    data = fetch_gmgn([mint]).get(mint)
    if not data: return pool
    enriched = dict(pool)
    enriched['_gmgn'] = {
        'visiting_count': num(data.get('visiting_count')),
        'total_fee': num(data.get('total_fee')),
        'holder_count': num(data.get('holder_count')),
        'top_10_holder_rate': num(data.get('top_10_holder_rate')),
        'creation_timestamp': data.get('creation_timestamp'),
    }
    return enriched

def font(size, bold=False):
    paths = ([r'C:\Windows\Fonts\arialbd.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'] if bold else
             [r'C:\Windows\Fonts\arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'])
    for path in paths:
        try: return ImageFont.truetype(path, size)
        except OSError: pass
    return ImageFont.load_default()


def render(pool):
    """Compact, desktop-friendly single-panel card, no browser screenshots."""
    m=metrics(pool)
    W,H=1250,840
    im=Image.new('RGB',(W,H),'#10111d'); d=ImageDraw.Draw(im)
    white='#f8f9ff'; muted='#b6b4c9'; green='#57f49b'; line='#48455f'
    d.rounded_rectangle((12,12,W-12,H-12),radius=34,fill='#151522',outline='#414058',width=3)
    d.text((48,44),m['name'][:23],font=font(45,True),fill=white)
    d.text((858,48),'Market Cap',font=font(26),fill=muted)
    d.text((W-48,38),money(m['mc']),font=font(42,True),fill=white,anchor='ra')
    fee='н/д' if m['fee'] is None else f"{m['fee']:g}%"
    d.text((50,117),'Fee:',font=font(25),fill=muted)
    d.text((110,116),fee,font=font(26,True),fill=white)
    d.text((185,117),'•   Bin Step:',font=font(25),fill=muted)
    d.text((350,116),str(m['bin'] or 'н/д'),font=font(26,True),fill=white)
    # Labels are muted; age values remain bright and bold, like Fee and Bin Step.
    age_parts = [('Tkn:', muted, False), (str(m['token_age']), white, True),
                 ('  •  Pool:', muted, False), (str(m['pool_age']), white, True)]
    age_fonts = [font(25, bold) for _, _, bold in age_parts]
    widths = [d.textlength(part, font=fnt) for (part, _, _), fnt in zip(age_parts, age_fonts)]
    x = W - 48 - sum(widths)
    for (part, color, _), fnt, width in zip(age_parts, age_fonts, widths):
        d.text((x, 117), part, font=fnt, fill=color)
        x += width
    d.line((45,174,W-45,174),fill=line,width=2)
    rows=[('Total TVL',m['tvl'],'Active TVL',m['active'],True),
          ('Fees',m['fees'],'Fees / Active TVL',m['fee_ratio'],True),
          ('Volume',m['volume'],'Volume / Active TVL',m['vol_ratio'],True),
          ('Swaps',m['swaps'],'Traders',m['traders'],False),
          ('Holders',m['holders'],'Top 10',m['top10'],False),
          ('Total LPs',m['lps'],'Volatility',m['volatility'],False),
          ('GMGN Views',m['gmgn_views'],'GMGN Total Fees',m['gmgn_total_fees'],False)]
    top=191; rh=88; mid=625
    d.line((mid,top,mid,top+rh*len(rows)),fill=line,width=2)
    for i,(ll,lv,rl,rv,hi) in enumerate(rows):
        y=top+i*rh+23
        d.text((52,y),ll,font=font(26),fill=muted)
        d.text((mid-24,y-2),lv,font=font(31,True),fill=green if ll=='Volume' else white,anchor='ra')
        d.text((mid+30,y),rl,font=font(24),fill=muted)
        d.text((W-52,y-2),rv,font=font(31,True),fill=green if hi else white,anchor='ra')
        if i<len(rows)-1: d.line((48,top+(i+1)*rh,W-48,top+(i+1)*rh),fill=line,width=1)
    out=io.BytesIO(); im.save(out,format='PNG'); out.seek(0)
    return out


def _x_social_url(value):
    """Return (type, canonical URL) for explicit X profile/community/post URLs."""
    from urllib.parse import urlparse
    import re
    if not isinstance(value, str):
        return None, None
    value = value.strip()
    if value.startswith('@'):
        value = 'https://x.com/' + value[1:]
    elif re.fullmatch(r'[A-Za-z0-9_]{1,15}', value):
        value = 'https://x.com/' + value
    elif value.startswith(('x.com/', 'twitter.com/', 'www.x.com/', 'www.twitter.com/')):
        value = 'https://' + value
    try:
        parsed = urlparse(value)
    except ValueError:
        return None, None
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in ('x.com','twitter.com','www.x.com','www.twitter.com'):
        return None, None
    chunks = parsed.path.strip('/').split('/')
    if (len(chunks) >= 3 and chunks[0].lower() == 'i' and chunks[1].lower() == 'communities'
            and chunks[2].isdigit()):
        return 'community', 'https://x.com/i/communities/' + chunks[2]
    if (len(chunks) >= 3 and chunks[1].lower() == 'status' and chunks[2].isdigit()
            and re.fullmatch(r'[A-Za-z0-9_]{1,15}', chunks[0])):
        return 'mention', 'https://x.com/' + chunks[0] + '/status/' + chunks[2]
    if len(chunks) == 1 and re.fullmatch(r'[A-Za-z0-9_]{1,15}', chunks[0]) and chunks[0].lower() not in ('home','search','intent','share','i','hashtag','explore','settings'):
        return 'profile', 'https://x.com/' + chunks[0]
    return None, None


def verified_social(pool):
    """Read explicit official X profile/community and individual mentions.

    Only social fields from token/GMGN/DEX metadata are trusted as project links.
    No account is inferred from the token symbol or an arbitrary mention.
    """
    profile = community = mention = None
    followers = members = None
    profile_fields = {'twitter', 'twitter_url', 'twitter_link', 'twitter_username',
                      'twitter_handle', 'twitter_account', 'twitter_profile',
                      'twitterurl', 'twitterusername', 'x_url', 'x', 'official_url',
                      'account_url', 'twitter_link_url', 'social_url', 'url', 'twitter_status_url', 'twitter_post', 'tweet_link'}
    community_fields = {'community', 'community_url', 'twitter_community',
                        'twitter_community_url', 'x_community', 'x_community_url'}
    mention_fields = {'mention_url', 'tweet_url', 'twitter_post_url', 'status_url', 'tweeturl', 'twitter_status_url', 'tweet_link', 'tweet_link_url', 'post_url', 'twitter_post'}
    follower_fields = {'twitter_followers', 'twitter_followers_count', 'twitterfollowers',
                       'followerscount', 'followers_count', 'follower_count', 'followers'}
    member_fields = {'community_members', 'community_member_count', 'members_count', 'member_count'}
    def take(value, hint=''):
        nonlocal profile, community, mention
        kind, url = _x_social_url(value)
        if kind == 'profile' and hint != 'mention':
            profile = profile or url
        elif kind == 'community':
            community = community or url
        elif kind == 'mention':
            mention = mention or url
    def walk(node, depth=0, context=''):
        nonlocal followers, members
        if depth > 6:
            return
        if isinstance(node, list):
            for entry in node[:25]:
                walk(entry, depth + 1, context)
        elif isinstance(node, dict):
            # DexScreener's info.socials = [{type/platform, url/handle}, ...].
            platform = str(node.get('type') or node.get('platform') or '').lower()
            if platform in ('twitter','x'):
                take(node.get('url') or node.get('link') or node.get('handle'))
            for key, value in node.items():
                name = str(key).lower()
                next_context = (context + ' ' + name).strip()
                # Creator/developer social accounts are not automatically the token's official account.
                if any(part in name for part in ('creator_stat', 'developer_social', 'dev_social')):
                    continue
                if isinstance(value, str):
                    if name in community_fields or name in profile_fields:
                        take(value)
                    elif name in mention_fields:
                        take(value, 'mention')
                if isinstance(value, (str, int, float)):
                    if name in follower_fields and followers is None and ('twitter' in context or 'social' in context or 'twitter' in name):
                        try: followers = max(0, int(float(value)))
                        except (TypeError, ValueError): pass
                    if name in member_fields and members is None and 'community' in context:
                        try: members = max(0, int(float(value)))
                        except (TypeError, ValueError): pass
                if isinstance(value, (dict, list)) and (depth == 0 or any(w in next_context for w in ('social','twitter','link','community','token','info','metadata','profile','website'))):
                    walk(value, depth + 1, next_context)
    # GMGN usually knows token's official social link first; DEX enriches missing ones.
    for candidate in (pool.get('_gmgn'), pool.get('_twitter'), pool.get('token_x'),
                      pool.get('token_y'), pool.get('_discovery'), pool.get('_dex_social'), pool):
        if isinstance(candidate, (dict, list)):
            walk(candidate)
    out = {}
    if profile: out['official_url'] = profile
    if community: out['community_url'] = community
    if mention: out['mention_url'] = mention
    if followers is not None and profile: out['followers'] = followers
    if members is not None and community: out['members'] = members
    return out


_DEX_SOCIAL_CACHE = {}

def fetch_dex_social(mint):
    """Collect explicitly published X links from DEX Screener (no guessed mentions).

    Search all matching pairs; a profile is preferred to a community, and a
    community to a linked post. The caller still gives GMGN priority.
    """
    if not mint:
        return {}
    cached_item = _DEX_SOCIAL_CACHE.get(mint)
    if cached_item and time.time() - cached_item[0] < 1800:
        return cached_item[1]
    result = {}
    options = {'profile': None, 'community': None, 'mention': None}
    try:
        response = SESSION.get('https://api.dexscreener.com/token-pairs/v1/solana/' + quote(mint, safe=''), timeout=7)
        response.raise_for_status()
        pairs = response.json()
        if isinstance(pairs, list):
            for pair in pairs[:50]:
                if not isinstance(pair, dict):
                    continue
                base = (pair.get('baseToken') or {}).get('address')
                quote_token = (pair.get('quoteToken') or {}).get('address')
                if mint not in (base, quote_token):
                    continue
                for social in ((pair.get('info') or {}).get('socials') or []):
                    if not isinstance(social, dict):
                        continue
                    if str(social.get('type') or social.get('platform') or '').lower() not in ('twitter', 'x'):
                        continue
                    kind, url = _x_social_url(social.get('url') or social.get('link') or social.get('handle'))
                    if kind in options and not options[kind]:
                        options[kind] = url
            if options['profile']:
                result['twitter'] = options['profile']
            if options['community']:
                result['community_url'] = options['community']
            if options['mention']:
                result['mention_url'] = options['mention']
    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.debug('DEX Screener social metadata unavailable: %s', exc)
    _DEX_SOCIAL_CACHE[mint] = (time.time(), result)
    return result


def twitter_diagnostic(pool_address):
    """Print only social-related metadata for a pool, no credentials or tokens."""
    address=pool_address.strip()
    detail=fetch_detail(address)
    pool=dict(detail or {})
    pool['address']=address
    try:
        discovery=discovery_record(address)
        if discovery: pool['_discovery']=discovery
    except Exception as exc:
        print('Discovery:',type(exc).__name__,str(exc)[:160])
    mint=token_mint(pool)
    print('Pool:',address,'| token mint:',mint or 'not found')
    if mint:
        gmgn=fetch_gmgn([mint]).get(mint)
        if gmgn: pool['_gmgn']=gmgn
        else: print('GMGN: no token metadata returned')
    print('Verified X:',json.dumps(verified_social(pool),ensure_ascii=False))
    interesting=('twitter','social','website','follower','mention','tweet','community','link','profile')
    def walk(obj,path='',depth=0):
        if depth>5: return
        if isinstance(obj,dict):
            for key,value in obj.items():
                nextpath=(path+'.'+str(key)).strip('.')
                if any(word in str(key).lower() for word in interesting):
                    val=json.dumps(value,ensure_ascii=False,default=str)
                    print(nextpath,'=',val[:350])
                elif isinstance(value,(dict,list)): walk(value,nextpath,depth+1)
        elif isinstance(obj,list):
            for i,value in enumerate(obj[:10]): walk(value,f'{path}[{i}]',depth+1)
    for section in ('token_x','token_y','_discovery','_gmgn'):
        if section in pool: walk(pool[section],section)
    print('If X is empty but GMGN website shows a profile, its social data comes from another endpoint.')

def links(pool):
    a=addr(pool); mint=token_mint(pool)
    buttons=[]
    if a: buttons.append({'text':'Meteora','url':f'https://www.meteora.ag/dlmm/{quote(a,safe="")}'})
    if mint:
        buttons.append({'text':'GMGN','url':f'https://gmgn.ai/sol/token/{quote(mint,safe="")}'})
        buttons.append({'text':'Bubblemaps','url':f'https://app.bubblemaps.io/sol/token/{quote(mint,safe="")}'})
    social=verified_social(pool)
    # Priority: official account OR community > explicit post > mint search.
    url = social.get('official_url') or social.get('community_url') or social.get('mention_url')
    if url:
        kind = 'Community' if url == social.get('community_url') else 'Twitter'
        buttons.append({'text':'X / '+kind,'url':url})
    elif mint:
        buttons.append({'text':'X / Twitter (поиск)','url':'https://x.com/search?q='+quote(mint,safe='')})
    return {'inline_keyboard':[buttons]} if buttons else None


def send_card(pool, test=False):
    image=render(pool)
    if not TOKEN or not CHAT:
        path=ROOT / ('radar_test.png' if test else 'radar_preview.png')
        path.write_bytes(image.getvalue())
        logging.warning('Telegram не настроен; карточка сохранена: %s',path)
        return True
    caption=('🧪 ТЕСТ · ' if test else '🆕 Новый пул · ') + symbol(pool)
    markup=links(pool)
    payload={'chat_id':CHAT,'caption':caption}
    if markup: payload['reply_markup']=json.dumps(markup)
    try:
        r=SESSION.post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto',data=payload,
                       files={'photo':('radar.png',image,'image/png')},timeout=25)
        r.raise_for_status()
        result=r.json()
        if not result.get('ok'): raise RuntimeError(str(result))
        return True
    except Exception as exc:
        logging.error('Telegram sendPhoto: %s',exc)
        return False


def fetch_recent_legacy(pages=None, size=None):
    """Scan newest pools with deeper pages so 200-result truncation loses fewer pools."""
    pages = PAGES if pages is None else pages
    size = PAGE_SIZE if size is None else size
    pools=[]; seen=set()
    for page in range(1,pages+1):
        r=SESSION.get(f'{API}/pools',params={'page':page,'page_size':size,
                        'sort_by':'pool_created_at:desc'},timeout=LIST_TIMEOUT_SECONDS)
        r.raise_for_status(); data=r.json()
        batch=data.get('data') if isinstance(data,dict) else None
        if not isinstance(batch,list): raise ValueError('Неожиданный формат Meteora /pools')
        for p in batch:
            if isinstance(p,dict) and addr(p) and addr(p) not in seen:
                seen.add(addr(p)); pools.append(p)
        if len(batch)<size: break
    return pools



def fetch_recent_discovery(quiet=False):
    """Cursor pagination. API ignores page=2; only after_key advances.

    Scan configured cursor pages without stopping on the age of non-DLMM pools.
    Diagnostic mode: keep all downstream filters, Telegram, and SQLite unchanged.
    """
    result = []
    seen = set()
    after_key = None
    page_limit = max(1, int(os.getenv('DISCOVERY_SCAN_PAGES', '4')))
    page_limit = min(page_limit, 20)
    page_size = min(PAGE_SIZE, 100)
    use_filter = bool(DISCOVERY_FILTER_BY)
    for page in range(1, page_limit + 1):
        params = {'page_size': page_size, 'timeframe': '30m', 'category': 'new'}
        if use_filter:
            params['filter_by'] = DISCOVERY_FILTER_BY
        if after_key:
            params['after_key'] = after_key
        try:
            r = SESSION.get(DISCOVERY_API, params=params, timeout=max(LIST_TIMEOUT_SECONDS, 15))
            r.raise_for_status()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if use_filter and page == 1 and code in (400, 422):
                logging.warning('Discovery отклонил filter_by=%s (HTTP %s); повторяем без фильтра', DISCOVERY_FILTER_BY, code)
                use_filter = False
                params.pop('filter_by', None)
                r = SESSION.get(DISCOVERY_API, params=params, timeout=max(LIST_TIMEOUT_SECONDS, 15))
                r.raise_for_status()
            else:
                raise
        payload = r.json()
        batch = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise ValueError('Неожиданный формат Discovery /pools')
        new_count = 0
        dlmm_count = 0
        young_dlmm = 0
        ages = []
        for item in batch:
            if not isinstance(item, dict):
                continue
            a = addr(item)
            if not a or a in seen:
                continue
            seen.add(a)
            new_count += 1
            age = age_minutes(item.get('pool_created_at') or item.get('created_at'))
            if age is not None:
                ages.append(age)
            if is_dlmm(item):
                dlmm_count += 1
                if age is not None and age <= NEW_POOL_MAX_AGE_MINUTES:
                    young_dlmm += 1
                # Diagnostic only: never change filtering, SQLite or Telegram behavior.
                # Log actual DLMM age separately from all-pool page age.
                if not quiet and os.getenv('LOG_DLMM_DETAILS', '1').strip().lower() not in ('0', 'false', 'no'):
                    mc = market_cap(item)
                    fee = num(item.get('fee_pct'))
                    fee30 = num(item.get('fee'))
                    logging.info('DLMM %s | age=%s min | MC=%s | BaseFee=%s%% | Fees30m=%s | addr=%s',
                                 symbol(item), age if age is not None else 'н/д',
                                 f'${mc:,.0f}' if mc is not None else 'н/д',
                                 fee if fee is not None else 'н/д',
                                 f'${fee30:,.2f}' if fee30 is not None else 'н/д', a)
            result.append({**item, '_discovery': item})
        if not quiet:
            logging.info('Discovery стр.%s: новых %s | DLMM %s | DLMM <=%sмин %s | возраст %s..%s мин',
                         page, new_count, dlmm_count, NEW_POOL_MAX_AGE_MINUTES, young_dlmm,
                         min(ages) if ages else '?', max(ages) if ages else '?')
        next_key = payload.get('after_key') if isinstance(payload, dict) else None
        # has_more is undocumented: stop only when it is explicitly False, or the cursor is missing/repeated.
        if not batch or not new_count or payload.get('has_more') is False or not next_key or next_key == after_key:
            break
        # A fully old page means newer pools have already been scanned.
        # Do not use this optimization unless every entry has a valid timestamp.
        if len(ages) == new_count and min(ages) > NEW_POOL_MAX_AGE_MINUTES + 2:
            logging.info('Discovery: страница старше окна, но продолжаем до лимита страниц для проверки DLMM')
        after_key = next_key
    return result


def fetch_recent_both():
    """Discovery (metrics) + one small legacy page (complete newest-DLMM list). Either may fail alone."""
    disc, leg = [], []
    try:
        disc = fetch_recent_discovery()
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Discovery не ответил: %s', exc)
    try:
        leg = fetch_recent_legacy(pages=1, size=LEGACY_PAGE_SIZE)
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Legacy Meteora не ответил: %s', exc)
    if not disc and not leg:
        raise ValueError('Ни Discovery, ни legacy не вернули список пулов')
    merged = {}
    for item in leg:
        merged[addr(item)] = item
    disc_addrs = set()
    for item in disc:
        a = addr(item); disc_addrs.add(a)
        merged[a] = merge_pool(merged[a], item) if a in merged else item   # Discovery wins; None/0-MC never erase legacy data
    only_legacy = [a for a in (addr(i) for i in leg) if a not in disc_addrs]
    logging.info('Источники: Discovery %s | legacy %s | DLMM только в legacy (Discovery их не показал) %s',
                 len(disc), len(leg), len(only_legacy))
    return list(merged.values())


def fetch_recent():
    if LIST_SOURCE == 'both':
        return fetch_recent_both()
    if LIST_SOURCE != 'discovery':
        try:
            pools = fetch_recent_legacy()
            logging.info('Источник списка: legacy Meteora (%s пулов)', len(pools))
            return pools
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Legacy Meteora не ответил: %s', exc)
            if LIST_SOURCE == 'legacy':
                raise
    logging.info('Discovery category=new: получаем новые пулы')
    pools = fetch_recent_discovery()
    logging.info('Источник списка: Discovery new (%s пулов)', len(pools))
    return pools

def fetch_discovery_index():
    """Get all available matching 30m top pools using Discovery's after_key cursor."""
    found = {}
    after_key = None
    seen_keys = set()
    for page in range(1, 21):
        params = {
            'page_size': 50, 'timeframe': '30m', 'category': 'top',
            'filter_by': 'pool_type=dlmm&&fee>=10&&fee_pct>=0.8',
        }
        if after_key:
            params['after_key'] = after_key
        r = SESSION.get(DISCOVERY_API, params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        batch = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise ValueError('Неожиданный формат Meteora Discovery /pools')
        for item in batch:
            if isinstance(item, dict) and addr(item):
                found[addr(item)] = item
        next_key = payload.get('after_key')
        if (not batch or payload.get('has_more') is False or not next_key
                or next_key == after_key or next_key in seen_keys):
            break
        seen_keys.add(next_key)
        after_key = next_key
    return found


def enrich_discovery(pool, index):
    a = addr(pool)
    if a and a in index:
        result = dict(pool)
        result['_discovery'] = index[a]
        return result
    return pool

def fetch_detail(address):
    r=SESSION.get(f'{API}/pools/{quote(address,safe="")}',timeout=15)
    r.raise_for_status()
    data=r.json()
    if isinstance(data,dict) and isinstance(data.get('data'),dict): return data['data']
    if isinstance(data,dict): return data
    raise ValueError('Неверный формат детального ответа')


def merge_detail(list_pool, detail):
    # Preserve list metrics when detail omits them; do not overwrite with null.
    merged=dict(list_pool)
    for k,v in detail.items():
        if v is None: continue
        if isinstance(v,dict) and isinstance(merged.get(k),dict):
            merged[k]={**merged[k],**{sub:val for sub,val in v.items() if val is not None}}
        else: merged[k]=v
    return merged


def enrich_volatility(pool):
    if volatility_30m(pool) is not None: return pool
    a=addr(pool)
    if not a: return pool
    try:
        r=SESSION.get(f'{API}/pools/{quote(a,safe="")}/ohlcv',
                      params={'timeframe':'5m'},timeout=12)
        r.raise_for_status(); payload=r.json()
        candles=payload.get('data',[]) if isinstance(payload,dict) else []
        now=time.time(); recent=[]
        for c in candles:
            if not isinstance(c,dict): continue
            ts=num(c.get('timestamp'))
            if ts is None: continue
            if ts>1e12: ts/=1000
            if now-1800 <= ts <= now+300: recent.append(c)
        if recent:
            hi=max((num(c.get('high')) for c in recent if num(c.get('high')) is not None),default=None)
            lo=min((num(c.get('low')) for c in recent if num(c.get('low')) is not None),default=None)
            if hi is not None and lo is not None and lo>0 and hi>=lo:
                pool=dict(pool); pool['ohlc']={**(pool.get('ohlc') or {}),'30m':{'high':hi,'low':lo}}
    except (requests.RequestException,ValueError,TypeError) as exc:
        logging.debug('OHLCV %s: %s',a,exc)
    return pool


def db_open():
    db=sqlite3.connect(DB, timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS pools (address TEXT PRIMARY KEY, first_seen TEXT NOT NULL, status TEXT NOT NULL, market_cap REAL, data_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS radar_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.commit(); return db


def save_record(db,p,status,first_seen=None):
    a=addr(p)
    if not a: return
    db.execute('INSERT INTO pools(address,first_seen,status,market_cap,data_json) VALUES(?,?,?,?,?) '
               'ON CONFLICT(address) DO UPDATE SET status=excluded.status,market_cap=excluded.market_cap,data_json=excluded.data_json',
               (a,first_seen or datetime.now(timezone.utc).isoformat(),status,market_cap(p),json.dumps(p,ensure_ascii=False)))
    # v31: commit at once. In v30 this write transaction stayed open until the END of the scan, so the
    # v30_cards INSERT made by send_card() (another connection) failed with "database is locked" AFTER the
    # photo had already been sent -> status stayed 'pending' -> the same pool was sent again on every scan,
    # and the Update button found no card in the database.
    db.commit()


def run():
    db=db_open()
    # One-time upgrade baseline: do not send hours-old unseen pools on first launch.
    bootstrapping=db.execute("SELECT value FROM radar_meta WHERE key='v7_initialized'").fetchone() is None
    # Migration from v8: previously tracked old pools must not be sent as new.
    migrated = db.execute("SELECT value FROM radar_meta WHERE key='v9_initialized'").fetchone() is None
    if migrated:
        rows = db.execute("SELECT address,first_seen,status,data_json FROM pools WHERE status NOT IN ('sent','baseline','expired')").fetchall()
        old_count = 0
        for address, first_seen, status, raw in rows:
            try:
                old_pool = json.loads(raw)
                created_age = age_minutes(old_pool.get('pool_created_at') or old_pool.get('created_at'))
                seen_dt = datetime.fromisoformat(first_seen)
                if seen_dt.tzinfo is None: seen_dt = seen_dt.replace(tzinfo=timezone.utc)
                observed_age = (datetime.now(timezone.utc)-seen_dt).total_seconds()/60
                if created_age is None or created_age > WATCH_MINUTES or observed_age > WATCH_MINUTES:
                    db.execute("UPDATE pools SET status='baseline' WHERE address=?",(address,))
                    old_count += 1
            except (ValueError,TypeError,OverflowError):
                db.execute("UPDATE pools SET status='baseline' WHERE address=?",(address,))
                old_count += 1
        db.execute("INSERT INTO radar_meta(key,value) VALUES('v9_initialized',?)",(datetime.now(timezone.utc).isoformat(),))
        db.commit()
        logging.info('v9: исключены старые записи из очереди: %s',old_count)
    logging.info('Meteora Radar v11 | %ss | %s pages x %s | MC >= $%.0f | Base Fee >= %.2f%% | Fees30m >= $%.2f',
                 INTERVAL,PAGES,PAGE_SIZE,MIN_MC,MIN_BASE_FEE,MIN_FEES_30M)
    logging.info('Настройки: DISCOVERY_SCAN_PAGES=%s (cursor after_key), PAGE_SIZE<=100, DETAIL_BUDGET=%s, NEW_POOL_MAX_AGE_MINUTES=%s', os.getenv('DISCOVERY_SCAN_PAGES','4'), DETAIL_BUDGET, NEW_POOL_MAX_AGE_MINUTES)
    if bootstrapping: logging.info('Первый запуск v7: текущая выдача будет базовой; старые пулы не рассылаем')
    if not TOKEN or not CHAT: logging.warning('Telegram credentials absent; cards saved locally')
    while True:
        try:
            logging.info('Начало сканирования списка Meteora...')
            pools=fetch_recent(); discovery_index=None; now=datetime.now(timezone.utc); sent=0; candidates=0; details=0; detail_attempts=0; detail_errors=0; fresh=0; skip_mc=0; skip_fee=0; skip_fees30=0; skipped_type=0; skipped_old=0; skipped_seen=0; unknown_age=0
            # First-run baseline: mark only unseen pools as historical. Preserve already-sent records.
            if bootstrapping:
                db.execute("UPDATE pools SET status='baseline' WHERE status NOT IN ('sent','baseline')")
                for p in pools:
                    a=addr(p)
                    if not db.execute('SELECT 1 FROM pools WHERE address=?',(a,)).fetchone():
                        save_record(db,p,'baseline')
                db.execute("INSERT OR REPLACE INTO radar_meta(key,value) VALUES('v7_initialized',?)",(now.isoformat(),))
                db.commit(); bootstrapping=False
                logging.info('База v7 создана: %s пулов; дальше новые будут отслеживаться сразу',len(pools))
                time.sleep(INTERVAL); continue
            # Fresh list first, but do not forget previously discovered pools that fell off the first pages.
            indexed={addr(p):p for p in pools}
            tracked=db.execute("SELECT address,first_seen,status,data_json FROM pools WHERE status NOT IN ('sent','baseline','expired','send_failed') ORDER BY first_seen DESC LIMIT 1500").fetchall()
            for a,first_seen,status,raw in tracked:
                if a not in indexed:
                    try: indexed[a]=json.loads(raw)
                    except (ValueError,TypeError): continue
            for a,p in indexed.items():
                if not is_dlmm(p):
                    skipped_type+=1
                    continue
                record=db.execute('SELECT first_seen,status FROM pools WHERE address=?',(a,)).fetchone()
                if record and record[1] in ('sent','baseline','send_failed'):
                    skipped_seen+=1
                    continue
                if not record:
                    age=age_minutes(p.get('pool_created_at') or p.get('created_at'))
                    # New to SQLite is NOT the same as newly created on Solana.
                    if age is None or age > SIGNAL_MAX_AGE_MINUTES:
                        if age is None: unknown_age+=1
                        else:
                            skipped_old+=1
                            if age <= LATE_LOG_MINUTES:
                                # Evidence for 'late detection': pool first seen older than the window -> never sent.
                                logging.info('БАЗА (старше окна) %s | возраст при первом обнаружении %s мин > окна %s | %s',
                                             symbol(p), age, SIGNAL_MAX_AGE_MINUTES, a)
                        save_record(db,p,'baseline')
                        continue
                    fresh+=1
                    logging.info('НОВЫЙ %s | возраст %s | %s',symbol(p),human_age(age),a)
                else:
                    try:
                        seen=datetime.fromisoformat(record[0]); seen=seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
                        if (now-seen).total_seconds()>WATCH_MINUTES*60:
                            save_record(db,p,'expired',record[0]); continue
                    except ValueError: pass
                    cap_age=age_minutes(p.get('pool_created_at') or p.get('created_at'))
                    if cap_age is not None and cap_age > SEND_MAX_AGE_MINUTES:
                        save_record(db,p,'expired',record[0]); continue
                # Do not promote old v8 discoveries to fresh signals.
                if record:
                    created_age=age_minutes(p.get('pool_created_at') or p.get('created_at'))
                    try:
                        seen_dt=datetime.fromisoformat(record[0])
                        if seen_dt.tzinfo is None: seen_dt=seen_dt.replace(tzinfo=timezone.utc)
                        minutes_since_seen=(now-seen_dt).total_seconds()/60
                        if created_age is None or created_age-minutes_since_seen > SIGNAL_MAX_AGE_MINUTES + 1:
                            save_record(db,p,'baseline',record[0]); continue
                    except (ValueError,TypeError):
                        save_record(db,p,'baseline',record[0]); continue
                mc=market_cap(p)
                base_fee=pos(first_num(p.get('_discovery') or {}, [('fee_pct',)]))
                if base_fee is None: base_fee=pos(first_num(p.get('pool_config') or {}, [('base_fee_pct',)]))
                fees30=best_fees30(p)
                # Enrich only likely candidates or data-missing pools, limited per scan.
                promising=(mc is None or mc>=MIN_MC) and (base_fee is None or base_fee>=MIN_BASE_FEE)
                if promising and not p.get('_discovery') and details<DETAIL_BUDGET and (fees30 is None or fees30>=MIN_FEES_30M or record is None):
                    try:
                        detail_attempts+=1
                        p=merge_detail(p,fetch_detail(a)); details+=1
                        mc=market_cap(p)
                        base_fee=pos(first_num(p.get('_discovery') or {}, [('fee_pct',)])) or pos(first_num(p.get('pool_config') or {}, [('base_fee_pct',)]))
                        fees30=best_fees30(p)
                    except (requests.RequestException,ValueError) as exc:
                        detail_errors+=1
                        if detail_errors<=3: logging.warning('Детали %s: %s',a,exc)
                if mc is None: status='mc_unknown'; skip_mc+=1
                elif mc<MIN_MC: status='below_mc'; skip_mc+=1
                elif base_fee is None: status='fee_unknown'; skip_fee+=1
                elif base_fee<MIN_BASE_FEE: status='below_base_fee'; skip_fee+=1
                elif fees30 is None or fees30<MIN_FEES_30M: status='below_fees30'; skip_fees30+=1
                else: status='pending'; candidates+=1
                save_record(db,p,status,record[0] if record else None)
                if status=='pending':
                    if sent>=MAX_SENDS_PER_SCAN:
                        logging.info('Лимит %s карточек за скан: %s остаётся pending до следующего скана', MAX_SENDS_PER_SCAN, symbol(p))
                        continue
                    if discovery_index is None and p.get('_discovery'):
                        discovery_index = {}  # Discovery new already includes the correct pool snapshot.
                    if discovery_index is None:
                        try:
                            discovery_index = fetch_discovery_index()
                            logging.info('Discovery 30m: %s пулов в категории top', len(discovery_index))
                        except (requests.RequestException, ValueError) as exc:
                            logging.warning('Discovery API недоступен: %s; используем доступные показатели старого API', exc)
                            discovery_index = {}
                    p = enrich_discovery(p, discovery_index)
                    if not p.get('_discovery'):
                        logging.info('Discovery 30m: %s отсутствует в категории top; недостающие поля будут н/д', a)
                    # GMGN and optional volatility are independent network tasks.
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        gmgn_future=executor.submit(enrich_gmgn,p)
                        vol_future=executor.submit(enrich_volatility,p) if not p.get('_discovery') else None
                        gmgn_result=gmgn_future.result()
                        vol_result=vol_future.result() if vol_future else p
                    p=dict(vol_result)
                    if gmgn_result.get('_gmgn'):
                        p['_gmgn']=gmgn_result['_gmgn']
                    if send_card(p):
                        age=age_minutes(p.get('pool_created_at') or p.get('created_at'))
                        logging.info('ОТПРАВЛЕН %s | возраст пула %s | ранее обнаружен=%s',symbol(p),human_age(age),bool(record))
                        save_record(db,p,'sent',record[0] if record else None);sent+=1
                        SEND_ATTEMPTS.pop(a,None)
                    else:
                        SEND_ATTEMPTS[a]=SEND_ATTEMPTS.get(a,0)+1
                        if SEND_ATTEMPTS[a]>=MAX_SEND_ATTEMPTS:
                            logging.error('Отправка %s не удалась %s раза подряд: статус send_failed, больше не повторяю',symbol(p),SEND_ATTEMPTS[a])
                            save_record(db,p,'send_failed',record[0] if record else None)
            db.commit()
            logging.info('Список %s | отслеживается %s | детали %s | прошли фильтры %s | отправлено %s',
                         len(pools),len(indexed),details,candidates,sent)
            logging.info('Диагностика: не-DLMM=%s | уже sent/baseline=%s | старые=%s | без даты=%s | новые=%s | MC=%s | Fee=%s | Fees30m=%s | ошибки деталей=%s',
                         skipped_type,skipped_seen,skipped_old,unknown_age,fresh,skip_mc,skip_fee,skip_fees30,detail_errors)
        except (requests.RequestException,ValueError,sqlite3.Error) as exc: logging.error('Ошибка цикла: %s',exc)
        time.sleep(INTERVAL)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    ap=argparse.ArgumentParser(); ap.add_argument('--check-twitter',metavar='POOL_ADDRESS'); ap.add_argument('--test',action='store_true'); ap.add_argument('--test-gmgn',action='store_true'); ap.add_argument('--test-live',action='store_true'); ap.add_argument('--test-card-live',action='store_true'); args=ap.parse_args()
    if args.check_twitter:
        twitter_diagnostic(args.check_twitter)
        return
    if args.test_card_live:
        index = fetch_discovery_index()
        if not index:
            raise SystemExit('Discovery вернул пустой список')
        sample = next(iter(index.values()))
        # Use the same real pool object for both rendering and button URLs.
        # Real data integration test, including GMGN.
        pool = dict(sample)
        pool['address'] = addr(sample)
        pool['_discovery'] = sample
        pool=enrich_gmgn(pool)
        print('Реальный пул:', symbol(pool), '| адрес:', addr(pool))
        m = metrics(pool)
        for key in ('tvl','active','fees','fee_ratio','volume','vol_ratio','swaps','traders','holders','top10','lps','volatility','gmgn_views','gmgn_total_fees','token_age'):
            print(key, '=', m[key])
        if not send_card(pool, test=True):
            raise SystemExit('Не удалось отправить реальную тестовую карточку')
        print('Реальная тестовая карточка отправлена в Telegram (или сохранена локально при отсутствии настроек).')
        return
    if args.test_live:
        index = fetch_discovery_index()
        if not index: raise SystemExit('Discovery вернул пустой список')
        sample = next(iter(index.values()))
        print('Discovery HTTP OK | пулов:', len(index), '| пример:', sample.get('name'))
        print('Поля:')
        for key in ('active_tvl','fee','fee_active_tvl_ratio','volume','volume_active_tvl_ratio',
                    'swap_count','unique_traders','total_lps','volatility'):
            print(' ', key, '=', sample.get(key))
        return
    if args.test_gmgn:
        mint='SCGBBnSwnekytGFHgyeHHxPMiN7xDxsWFGg9JgFSTNK'
        data=fetch_gmgn([mint]).get(mint)
        if data:
            print('GMGN OK:', 'Views =',data.get('visiting_count'),'Total Fees =',data.get('total_fee'),'SOL')
        else:
            raise SystemExit('GMGN не вернул данные. Проверь лог и доступность API.')
        return
    if args.test:
        example={'address':'TEST_POOL','token_x':{'symbol':'REGRETAMINE'},'token_y':{'symbol':'SOL'},
                 'market_cap':250000,'tvl':35000,'active_tvl':12000,
                 'fees':{'30m':2800},'volume':{'30m':160000},'swaps':{'30m':1100},
                 'traders':{'30m':570},'holders':1250,'top10_pct':18.5,'total_lps':48,'ohlc':{'30m':{'high':1.3,'low':1.0}},
                 'pool_config':{'base_fee_pct':3,'bin_step':100}}
        # Real MAMESUKE token used only for a deterministic integration test.
        example['token_x']['address']='SCGBBnSwnekytGFHgyeHHxPMiN7xDxsWFGg9JgFSTNK'
        example=enrich_gmgn(example)
        if send_card(example,test=True): print('Тестовая карточка создана/отправлена. Ссылка Meteora в тесте условная.')
        else: raise SystemExit('Не удалось отправить тестовую карточку')
    else: run()



# Snapshot original v11 helper functions for approved card rendering.
# This is NOT an import of another radar version.
from types import SimpleNamespace
v11 = SimpleNamespace(**globals())

WHITE='#f8f7ff'; MUTED='#bcb8d6'; GREEN='#00ef83'; YELLOW='#fff01b'; RED='#ff6469'
LINE='#686481'; BG='#090914'; PURPLE='#a48bf8'; LIGHT_BLUE='#77cfff'


def raw_num(v):
    return v11.num(v)


def number(v, prefix='', suffix=''):
    n=raw_num(v)
    return 'н/д' if n is None else f'{prefix}{round(n):,}{suffix}'.replace(',', ' ')


def dollars(v):
    n=raw_num(v)
    if n is None: return 'н/д'
    if abs(n)>=1_000_000_000: return f'${round(n/1_000_000_000)}B'
    if abs(n)>=1_000_000: return f'${round(n/1_000_000)}M'
    if abs(n)>=1000: return f'${round(n/1000)}K'
    return f'${round(n)}'


def dollars_precise(v):
    """One decimal for K/M/B; retain dollar precision for small values."""
    n=raw_num(v)
    if n is None: return 'н/д'
    if abs(n)>=1_000_000_000: return f'${n/1_000_000_000:.1f}B'
    if abs(n)>=1_000_000: return f'${n/1_000_000:.1f}M'
    if abs(n)>=1000: return f'${n/1000:.1f}K'
    return f'${n:,.1f}'


def pct(v, digits=0):
    n=raw_num(v)
    return 'н/д' if n is None else f'{n:.{digits}f}%'


def gmgn_pct(v):
    n=raw_num(v)
    return 'н/д' if n is None else pct(n*100)


def value_percent_from_metrics(v, digits=0):
    # v11 metrics provides formatted percentage strings such as '7.9%'.
    if v is None or v=='н/д': return 'н/д'
    try: return pct(float(str(v).replace('%','').replace(',','')),digits)
    except ValueError: return 'н/д'


def gmgn_fees(v):
    # Keep whole SOL, with no decimal or thousands separator.
    import re
    match=re.search(r'[-+]?\d[\d, .]*',str(v))
    if not match: return 'н/д'
    try:
        n=float(match.group().replace(' ','').replace(',',''))
        return f'{round(n)} SOL'
    except ValueError: return 'н/д'


def status(renounced):
    return 'Disabled' if renounced is True else ('Enabled' if renounced is False else 'н/д')


def threshold(name, default):
    try: return float(os.getenv(name, str(default)))
    except (ValueError, TypeError): return float(default)


def risk_color(value, green_max, yellow_max):
    n=raw_num(str(value).replace('%',''))
    if n is None: return MUTED
    if n<=green_max: return GREEN
    if n<=yellow_max: return YELLOW
    return RED


def f(sz,bold=False): return v11.font(sz,bold)


def txt(d,xy,s,size=28,color=WHITE,bold=False,anchor=None):
    d.text(xy,str(s),font=f(size,bold),fill=color,anchor=anchor)


def icon(d,kind,x,y,color):
    # Small vector icons only in column 3; no external icon/font dependencies.
    if kind=='top':
        for dx,dy,r in ((0,2,9),(-14,9,7),(14,9,7)):
            d.ellipse((x+dx-r,y+dy-r,x+dx+r,y+dy+r),fill=color)
        d.rounded_rectangle((x-22,y+17,x+22,y+29),radius=7,fill=color)
    elif kind=='bundler':
        for dy in (0,10,20):
            d.line([(x-19,y+dy+7),(x,y+dy-2),(x+19,y+dy+7),(x,y+dy+16),(x-19,y+dy+7)],fill=color,width=3)
    elif kind=='phishing':
        d.polygon([(x,y-8),(x+18,y-1),(x+15,y+19),(x,y+29),(x-15,y+19),(x-18,y-1)],outline=color,width=4)
        d.line((x,y+1,x,y+13),fill=color,width=5);d.ellipse((x-3,y+18,x+3,y+24),fill=color)
    elif kind=='dev':
        d.ellipse((x-10,y-5,x+10,y+15),fill=color)
        d.rounded_rectangle((x-20,y+16,x+20,y+30),radius=8,fill=color)
    elif kind=='organic':
        d.ellipse((x-18,y-4,x+18,y+25),fill=color)
        d.line((x-19,y+30,x+15,y+1),fill=BG,width=4)
    elif kind=='transfer':
        d.ellipse((x-18,y-6,x+18,y+30),outline=color,width=4)
        d.line((x-8,y+7,x+9,y+7),fill=color,width=4)
        d.polygon([(x+6,y+1),(x+15,y+7),(x+6,y+13)],fill=color)
    elif kind=='rug':
        d.polygon([(x,y-10),(x+22,y+29),(x-22,y+29)],outline=color,width=4)
        d.line((x,y+2,x,y+16),fill=color,width=5);d.ellipse((x-3,y+20,x+3,y+26),fill=color)


def frog(d,x,y):
    # Local vector frog symbol, independent of platform emoji rendering.
    c='#7be82e'; dark='#213e0a'
    d.ellipse((x-13,y-9,x+13,y+15),fill=c)
    d.ellipse((x-14,y-13,x-2,y-1),fill=c);d.ellipse((x+2,y-13,x+14,y-1),fill=c)
    d.ellipse((x-10,y-10,x-5,y-5),fill=dark);d.ellipse((x+5,y-10,x+10,y-5),fill=dark)
    d.arc((x-8,y-2,x+8,y+10),0,180,fill=dark,width=2)


def metric_row(d,x0,x1,y,label,value,color=WHITE,icon_name=None,frog_icon=False,delta=None):
    # No icons in any column; consistent text alignment.
    lx=x0
    size=39
    if len(label)>19: size=35
    if len(label)>23: size=32
    vs=49 if len(str(value))<9 else 42
    if label in ('Mint Authority','Freeze Authority'): vs=43
    # Keep a real gap between labels and values on compact Telegram previews.
    while size>28 and d.textbbox((0,0),label,font=f(size))[2]+lx > x1-d.textbbox((0,0),str(value),font=f(vs,True))[2]-13:
        size-=1
    txt(d,(lx,y+16),label,size,MUTED)
    if delta is not None and str(value).strip():
        ds, dc = delta
        ds_font = 27
        # Keep original value aligned at right; fit delta into remaining space.
        value_width = d.textbbox((0,0),str(value),font=f(vs,True))[2]
        available = x1-value_width-18
        label_width = d.textbbox((0,0),label,font=f(size))[2]
        if available-lx-label_width > 44:
            delta_width = d.textbbox((0,0),ds,font=f(ds_font,True))[2]
            if available-delta_width-10 >= lx+label_width:
                txt(d,(available,y+17),ds,ds_font,dc,True,'ra')
        elif label == 'Fees/A TVL':
            # Keep the delta visible even when the ratio value is wide.
            # Place it in the upper part of this row, avoiding label/value collision.
            txt(d,(x1-8,y-10),ds,23,dc,True,'ra')
    txt(d,(x1,y+9),value,vs,color,True,'ra')
    d.line((x0,y+65,x1,y+65),fill=LINE,width=2)


def transfer_fee(pool):
    """Read SPL Token-2022 TransferFeeConfig from Solana RPC (not Meteora).

    None means unknown, 0 means confirmed no transfer fee for classic SPL Token.
    """
    import os
    mint=v11.token_mint(pool)
    if not mint: return 'н/д'
    rpc=os.getenv('SOLANA_RPC_URL','https://api.mainnet-beta.solana.com')
    try:
        r=v11.SESSION.post(rpc,json={'jsonrpc':'2.0','id':1,'method':'getAccountInfo',
            'params':[mint,{'encoding':'jsonParsed','commitment':'confirmed'}]},timeout=12)
        r.raise_for_status()
        result=r.json()
        if result.get('error'): return 'н/д'
        info=(result.get('result') or {}).get('value') or {}
        owner=info.get('owner','')
        if owner=='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA': return '0%'
        if owner!='TokenzQdBNbLqP5VEhdkAS6EPF4N3g2D9d5GXJ7cC2':
            # Token-2022 program ID is checked separately below; unknown owner is not 0%.
            if not owner.startswith('TokenzQd'): return 'н/д'
        parsed=(info.get('data') or {}).get('parsed') or {}
        extensions=(parsed.get('info') or {}).get('extensions') or []
        for ext in extensions:
            if str(ext.get('extension','')).lower()=='transferfeeconfig':
                state=ext.get('state') or {}
                newer=state.get('newerTransferFee') or {}
                older=state.get('olderTransferFee') or {}
                basis=newer.get('transferFeeBasisPoints',older.get('transferFeeBasisPoints'))
                if basis is None:return 'н/д'
                return f'{float(basis)/100:g}%'
        return '0%' if isinstance(extensions,list) else 'н/д'
    except Exception as e:
        print('Transfer Fee: RPC недоступен:',str(e)[:100])
        return 'н/д'


def render_panel(pool, blank=False, changes=None, lower=False, initial_mc=None):
    m=v11.metrics(pool)
    g=pool.get('_gmgn') or {}; security=g.get('security') or {}
    W,H=1500,735
    im=Image.new('RGB',(W,H),BG);d=ImageDraw.Draw(im)
    d.rounded_rectangle((11,10,W-11,H-10),radius=35,outline=PURPLE,width=4)
    # Header
    d.polygon([(48,42),(95,42),(82,55),(35,55)],fill='#00ebd4')
    d.polygon([(35,61),(82,61),(95,74),(48,74)],fill='#9b4ef6')
    title=m['name'][:21]
    title_size=59
    while title_size>36 and d.textbbox((0,0),title,font=f(title_size,True))[2]>650:
        title_size-=2
    txt(d,(112,25),title,title_size,WHITE,True)
    badge_x=max(625, min(810, 112+d.textbbox((0,0),title,font=f(title_size,True))[2]+22))
    d.rounded_rectangle((badge_x,41,badge_x+130,90),radius=13,fill='#831094',outline='#df42e9',width=2)
    txt(d,(badge_x+23,46),'DLMM',31,WHITE,True)
    if not lower:
        if initial_mc is None:
            txt(d,(1090,43),'Market Cap',29,MUTED)
            txt(d,(1455,22),dollars(m['mc']),54,WHITE,True,'ra')
        else:
            initial = m['mc']
            current = v11.metrics(initial_mc)['mc']
            change = (current-initial)/initial*100 if initial and current is not None else None
            label = f'MC: {dollars(initial)}  →  {dollars(current)}'
            label_size = 40
            while label_size > 25 and d.textlength(label,font=f(label_size,True)) > 560:
                label_size -= 1
            txt(d,(1320,39),label,label_size,WHITE,True,'ra')
            if change is not None:
                color = GREEN if change>0 else RED if change<0 else MUTED
                txt(d,(1455,42),f'{change:+.0f}%',30,color,True,'ra')
    fee='н/д' if m['fee'] is None else f'{m["fee"]:g}%'
    # Compact two-sided header: labels muted, values white and bold.
    # Dynamic text widths prevent gaps/overlaps when values vary.
    label_y=119; value_y=115
    txt(d,(46,label_y),'Fee:',32,MUTED)
    txt(d,(125,value_y),fee,37,WHITE,True)
    fee_w=d.textbbox((0,0),fee,font=f(37,True))[2]
    bx=125+fee_w+28
    txt(d,(bx,label_y),'•',30,MUTED)
    txt(d,(bx+35,label_y),'Bin Step:',32,MUTED)
    txt(d,(bx+195,value_y),number(m['bin']),37,WHITE,True)
    # Ages: use separate muted labels and bold white values; align to the right.
    pool_age=str(m['pool_age']); token_age=str(m['token_age'])
    chunks=[('Tkn:',MUTED,False,31),(token_age,WHITE,True,36),('•',MUTED,False,29),
            ('Pool:',MUTED,False,31),(pool_age,WHITE,True,36)]
    widths=[d.textbbox((0,0),t,font=f(sz,bold))[2] for t,_,bold,sz in chunks]
    gaps=[10,23,23,10]
    xx=1453-sum(widths)-sum(gaps)
    for i,(t,c,bold,sz) in enumerate(chunks):
        txt(d,(xx,115 if bold else 119),t,sz,c,bold)
        xx+=widths[i]+(gaps[i] if i<len(gaps) else 0)
    d.line((45,190,1455,190),fill=LINE,width=3)
    # 3 columns, all text-only.
    for x in (505,1000):d.line((x,195,x,716),fill=LINE,width=2)
    left=[
        ('Total TVL',dollars(v11.first_num(pool.get('_discovery') or {},[('tvl',)]) or v11.first_num(pool,[('tvl',)])),WHITE,False),
        ('Fees',dollars(v11.first_num(pool.get('_discovery') or {},[('fee',)]) if v11.first_num(pool.get('_discovery') or {},[('fee',)]) is not None else v11.first_num(pool,[('fees','30m')])),WHITE,False),
        ('Volume',dollars_precise(v11.first_num(pool.get('_discovery') or {},[('volume',)]) if v11.first_num(pool.get('_discovery') or {},[('volume',)]) is not None else v11.first_num(pool,[('volume','30m')])),GREEN,False),
        ('Swaps',m['swaps'],WHITE,False),('Holders',m['holders'],WHITE,False),
        ('GMGN Views',m['gmgn_views'],LIGHT_BLUE,False),('Mint Authority',status(security.get('renounced_mint')),None,False),
    ]
    middle=[
        ('Active TVL',dollars_precise(v11.first_num(pool.get('_discovery') or {},[('active_tvl',)])) if v11.first_num(pool.get('_discovery') or {},[('active_tvl',)]) is not None else m['active'],GREEN,False),
        ('Fees/A TVL',value_percent_from_metrics(m['fee_ratio'],1),GREEN,False),
        ('Vol/A TVL',value_percent_from_metrics(m['vol_ratio']),GREEN,False),
        ('Traders',m['traders'],WHITE,False),('Total LPs',m['lps'],WHITE,False),
        ('GMGN Total Fees',gmgn_fees(m['gmgn_total_fees']),LIGHT_BLUE,False),
        ('Freeze Authority',status(security.get('renounced_freeze_account')),None,False),
    ]
    top10=gmgn_pct(g.get('top_10_holder_rate')) if g.get('top_10_holder_rate') is not None else value_percent_from_metrics(m['top10'])
    bundler=gmgn_pct(g.get('top_bundler_trader_percentage'))
    phishing=gmgn_pct(g.get('top_entrapment_trader_percentage'))
    dev=gmgn_pct(g.get('dev_team_hold_rate'))
    rug=gmgn_pct(g.get('rug_ratio'))
    right=[('Top 10',top10,risk_color(top10,threshold('TOP10_GREEN_MAX',15),threshold('TOP10_YELLOW_MAX',25)),'top'),
           ('Bundler',bundler,risk_color(bundler,threshold('BUNDLER_GREEN_MAX',10),threshold('BUNDLER_YELLOW_MAX',20)),'bundler'),
           ('Phishing*',phishing,risk_color(phishing,threshold('PHISHING_GREEN_MAX',10),threshold('PHISHING_YELLOW_MAX',20)),'phishing'),
           ('DEV',dev,risk_color(dev,threshold('DEV_GREEN_MAX',3),threshold('DEV_YELLOW_MAX',5)),'dev'),
           ('Rug %',rug,risk_color(rug,threshold('RUG_GREEN_MAX',5),threshold('RUG_YELLOW_MAX',15)),'rug'),
           ('Bin Rent (SOL)',pool.get('_bin_rent','н/д'),bin_rent_color(pool.get('_bin_rent')),'organic'),
           ('Transfer Fee',pool.get('_transfer_fee','н/д'),GREEN if pool.get('_transfer_fee')=='0%' else (RED if pool.get('_transfer_fee') not in (None,'н/д','0%') else MUTED),'transfer')]
    if lower:
        right = right[:3] + [
            ('Bubble MAX',bubble_display(pool,'max'),bubble_color(pool,'max'),None),
            ('Bubble OTHER',bubble_display(pool,'other'),bubble_color(pool,'other'),None),
            ('Top Wallet',bubble_display(pool,'top_wallet'),bubble_color(pool,'top_wallet'),None),
            ('Honeypot',honeypot_display(pool),honeypot_color(pool),None),
        ]
    y0=204;step=73
    for i,(label,val,color,fg) in enumerate(left):
        if color is None: color=GREEN if val=='Disabled' else (RED if val=='Enabled' else MUTED)
        metric_row(d,48,480,y0+i*step,label,'' if blank else val,color,frog_icon=fg,delta=(changes or {}).get(label))
    for i,(label,val,color,fg) in enumerate(middle):
        if color is None: color=GREEN if val=='Disabled' else (RED if val=='Enabled' else MUTED)
        metric_row(d,528,976,y0+i*step,label,'' if blank else val,color,frog_icon=fg,delta=(changes or {}).get(label))
    for i,(label,val,color,kind) in enumerate(right):
        # On a NEW card, the second panel is blank except wallet metrics:
        # these are independent token facts and must appear without Update.
        wallet_metric = lower and label in ('Bubble MAX','Bubble OTHER','Top Wallet','Honeypot')
        display_value = val if (not blank or wallet_metric) else ''
        metric_row(d,1025,1454,y0+i*step,label,display_value,color,icon_name=kind,delta=(changes or {}).get(label))
    return im


def render_twitter_placeholder(pool=None):
    """Three equal columns: two for X, one for wallet analytics. No fabricated social data."""
    W,H=1500,286
    im=Image.new('RGB',(W,H),BG); d=ImageDraw.Draw(im)
    d.rounded_rectangle((11,7,W-11,H-8),radius=28,outline=PURPLE,width=4)
    for x in (505,1000): d.line((x,16,x,H-17),fill=LINE,width=2)
    txt(d,(45,20),'X / Twitter Analysis',35,WHITE,True)
    txt(d,(540,20),'X / Activity',35,WHITE,True)
    txt(d,(1030,20),'Wallet Analysis',34,WHITE,True)
    d.line((40,75,1455,75),fill=LINE,width=2)
    social=verified_social(pool or {})
    columns=[[('Account',55,88),('Followers',270,88),('Official Posts',55,145),('Mentions',270,145)],
             [('Unique Authors',530,88),('Influencers',775,88),('Likes / Reposts',530,145),('Organic / Spam',775,145)],
             [('Top Wallet',1025,88),('Linked Wallets',1250,88),('Bubble MAX',1025,145),('Bubble OTHER',1250,145)]]
    for column in columns:
        for label,x,y in column:
            txt(d,(x,y),label,20,MUTED)
            value='—'
            if label=='Account' and social.get('official_url'):
                from urllib.parse import urlparse
                value='@'+urlparse(social['official_url']).path.strip('/').split('/')[0][:14]
            elif label=='Followers' and social.get('followers') is not None:
                value=number(social['followers'])
            elif label=='Bubble MAX': value=bubble_display(pool or {},'max')
            elif label=='Bubble OTHER': value=bubble_display(pool or {},'other')
            txt(d,(x,y+24),str(value),23,WHITE if value!='—' else MUTED,True)
    d.line((40,215,1455,215),fill=LINE,width=2)
    txt(d,(52,227),'X Summary:',24,WHITE,True)
    txt(d,(255,227),'Ожидаем подтверждённые данные API для оценки активности и органики',23,MUTED)
    return im


def render(pool, updated=None, changes=None):
    # Original upper snapshot remains unchanged except the MC comparison in its header.
    top = render_panel(pool, initial_mc=updated)  # Initial values remain fixed; MC header compares first/current
    lower = render_panel(updated or pool, blank=updated is None, changes=changes, lower=True)
    # Remove repeated header from the lower snapshot without shrinking any metric rows.
    body = lower.crop((0,190,1500,735))
    bottom = Image.new('RGB',(1500,570),BG)
    bottom.paste(body,(0,12))
    draw=ImageDraw.Draw(bottom)
    draw.rounded_rectangle((11,5,1489,563),radius=28,outline=PURPLE,width=4)
    # V40: keep only the two verified Meteora panels; X/Wallet panel removed.
    result=Image.new('RGB',(1500,735+570),BG)
    result.paste(top,(0,0));result.paste(bottom,(0,735))
    out=io.BytesIO();result.save(out,format='PNG');out.seek(0)
    return out


# Full GMGN payload: preserve risk/security fields for approved card.
def enrich_gmgn(pool):
    mint = token_mint(pool)
    if not mint:
        return pool
    data = fetch_gmgn([mint]).get(mint)
    enriched = dict(pool)
    if data:
        enriched['_gmgn'] = data
    # Fetch other published links when the token has no official profile.
    # This can recover a community or an explicitly linked X post.
    if not verified_social(enriched).get('official_url'):
        dex = fetch_dex_social(mint)
        if dex: enriched['_dex_social'] = dex
    social = verified_social(enriched)
    if social: enriched['_twitter'] = social
    return enriched

approved_card_render = render

# v30: keep original scanning unchanged. Persist sent cards and snapshots separately.
from threading import Thread, Lock
import hashlib

UPDATE_INTERVAL = 300  # background history sampling every 5 minutes
HISTORY_HOURS = 3
_REFRESH_LOCK = Lock()


def v30_db():
    con = sqlite3.connect(DB, timeout=15)
    con.execute('CREATE TABLE IF NOT EXISTS v30_cards (chat_id TEXT, message_id INTEGER, pool_address TEXT, original_json TEXT, sent_ts REAL, PRIMARY KEY(chat_id,message_id))')
    con.execute('CREATE TABLE IF NOT EXISTS v30_samples (pool_address TEXT, ts REAL, data_json TEXT, PRIMARY KEY(pool_address,ts))')
    con.commit()
    return con


def record_sample(con, pool, ts=None):
    a=addr(pool)
    if a:
        con.execute('INSERT OR REPLACE INTO v30_samples VALUES(?,?,?)',(a,ts or time.time(),json.dumps(pool,ensure_ascii=False)))
        con.commit()


_real_sleep = time.sleep   # captured once at import


def rpc(method, params):
    url=os.getenv('SOLANA_RPC_URL','https://api.mainnet-beta.solana.com')
    for attempt in (1,2):
        r=SESSION.post(url,json={'jsonrpc':'2.0','id':1,'method':method,'params':params},timeout=12)
        if r.status_code==429 and attempt==1:   # public RPC rate limit: wait and retry once
            _real_sleep(1.5); continue
        r.raise_for_status(); data=r.json()
        if data.get('error'): raise ValueError(str(data['error']))
        return data['result']
    raise RuntimeError('RPC: нет ответа')


# Base58 + Ed25519 PDA derivation, no new Python dependency.
_B58='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def b58decode(s):
    n=0
    for c in s: n=n*58+_B58.index(c)
    raw=n.to_bytes((n.bit_length()+7)//8,'big') if n else b''
    return b'\x00'*(len(s)-len(s.lstrip('1')))+raw

def b58encode(b):
    n=int.from_bytes(b,'big'); chars=''
    while n: n,mod=divmod(n,58); chars=_B58[mod]+chars
    return '1'*(len(b)-len(b.lstrip(b'\x00')))+(chars or '')

_P=2**255-19
_D=(-121665*pow(121666,_P-2,_P))%_P

def ed25519_point(data):
    y=int.from_bytes(data,'little')&((1<<255)-1)
    if y>=_P: return False
    y2=y*y%_P; u=(y2-1)%_P; v=(_D*y2+1)%_P
    x2=u*pow(v,_P-2,_P)%_P
    if x2==0: return True
    return pow(x2,(_P-1)//2,_P)==1

def derive_pda(seeds, program):
    for bump in range(255,-1,-1):
        digest=hashlib.sha256(b''.join(seeds)+bytes([bump])+program+b'ProgramDerivedAddress').digest()
        if not ed25519_point(digest): return b58encode(digest)
    raise ValueError('PDA derivation failed')


# ---- Bin Rent (v31) -------------------------------------------------------------------------------------
# Opening a position needs the "bin arrays" that cover its price range. The first LP who touches an array that
# does not exist yet pays its rent, and that rent is NOT refundable (position rent is refundable).
BIN_ARRAY_BINS = 70       # bins per bin array
BIN_ARRAY_BYTES = 10136   # account size: 8 + 48 header + 70 bins x 144 bytes; matches the 0.05214 SOL seen in a real tx
BIN_RENT_BELOW = max(0, int(os.getenv('BIN_RENT_BINS_BELOW', '34')))   # assumed position range: bins below the active bin
BIN_RENT_ABOVE = max(0, int(os.getenv('BIN_RENT_BINS_ABOVE', '34')))   # ... and above it (34+1+34 = 69 bins)

_CACHE = {}
def cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (time.time(), value)
    return value


def read_lb_pair(pair, pool):
    """On-chain LbPair account -> (program id bytes, active bin id).

    The account layout is located by the token mints and CROSS-CHECKED against Meteora's own bin_step;
    if anything does not match we raise instead of guessing (the card then shows н/д, never a made-up number).
    """
    import base64
    info = rpc('getAccountInfo', [pair, {'encoding': 'base64'}])['value']
    if not info:
        raise ValueError('аккаунт пула не найден в сети')
    raw = base64.b64decode(info['data'][0])
    disc = pool.get('_discovery') or {}
    conf = pool.get('pool_config') or {}
    step = at(disc, 'dlmm_params', 'bin_step') or conf.get('bin_step') or pool.get('bin_step')
    mints = [b58decode(str(t['address'])) for t in ((pool.get('token_x') or {}), (pool.get('token_y') or {})) if t.get('address')]
    for off in sorted({raw.find(m) for m in mints} - {-1}):
        if off < 12:
            continue
        active = int.from_bytes(raw[off - 12:off - 8], 'little', signed=True)
        bstep = int.from_bytes(raw[off - 8:off - 6], 'little')
        if step is not None and int(float(step)) == bstep:
            return b58decode(info['owner']), active
    raise ValueError('не нашёл active bin в данных пула (bin_step не сошёлся)')


def bin_rent_details(pool):
    pair = addr(pool)
    if not pair:
        raise ValueError('нет адреса пула')
    program, active = read_lb_pair(pair, pool)
    lo, hi = active - BIN_RENT_BELOW, active + BIN_RENT_ABOVE
    indices = list(range(lo // BIN_ARRAY_BINS, hi // BIN_ARRAY_BINS + 1))   # floor division: also right for negative ids
    pair_b = b58decode(pair)
    addresses = [derive_pda([b'bin_array', pair_b, i.to_bytes(8, 'little', signed=True)], program) for i in indices]
    accounts = rpc('getMultipleAccounts', [addresses, {'encoding': 'base64', 'dataSlice': {'offset': 0, 'length': 0}}])['value']
    lamports = cached('rent_lamports', 3600, lambda: rpc('getMinimumBalanceForRentExemption', [BIN_ARRAY_BYTES]))
    exists = [x is not None for x in accounts]
    missing = exists.count(False)
    return {'active': active, 'lo': lo, 'hi': hi, 'indices': indices, 'addresses': addresses,
            'exists': exists, 'missing': missing, 'lamports': lamports, 'sol': missing * lamports / 1e9}


def bin_rent(pool):
    """'0' = every bin array of the assumed range exists; '≈0.052' = SOL that would be lost forever; 'н/д' = unknown."""
    a = addr(pool)
    if not a:
        return 'н/д'
    try:
        d = cached(('bin_rent', a), 45, lambda: bin_rent_details(pool))
    except Exception as exc:
        logging.warning('Bin Rent %s: %s', a, exc)
        return 'н/д'
    return '0' if not d['missing'] else f"≈{d['sol']:.3f}"


def bin_rent_color(v):
    v = str(v or '')
    if v == '0':
        return GREEN
    if v.startswith('≈'):
        return RED
    return MUTED


def holder_is_technical(holder, label_info=None):
    """Conservative: reject explicitly named/labelled DEX, AMM, pools, vaults, burns and exchanges.

    A naked Solana address is not a name. Missing InsightX labels do NOT prove
    the holder is independent; unclassified holders need manual verification.
    """
    holder = holder if isinstance(holder, dict) else {}
    info = label_info if isinstance(label_info, dict) else {}
    combined = [holder, info]
    names = []
    tags = []
    for item in combined:
        for k in ('label','name','entity','account_name','owner_name','account_type',
                  'type','category','description','protocol','project','platform'):
            v = item.get(k)
            if isinstance(v, str):
                names.append(v.lower())
        for k in ('tags','labels','categories'):
            v = item.get(k)
            if isinstance(v, str): tags.append(v.lower())
            elif isinstance(v, list):
                tags.extend(str(x).lower() for x in v if isinstance(x, (str,int,float)))
        if item.get('smart_contract') is True or item.get('is_pool') is True or item.get('is_dex') is True:
            return True
    text=' '.join(names + tags)
    # Include Pump AMM & PumpSwap; do not match generic 'pump' (legitimate token names use it).
    technical_terms=('pump amm','pumpamm','pumpswap','pump.fun amm','liquidity pool',
                     'liquidity_pool','dex pool','amm pool','lp vault','lp token',
                     'raydium','meteora','orca whirlpool','orca pool','whirlpool',
                     'binance','coinbase','bybit','okx','kucoin','burn address',
                     'burn wallet','vault','router','smart_contract')
    # The standalone "pool" label is meaningful; do not mistake an arbitrary token name.
    if any(term in text for term in technical_terms):
        return True
    return bool(set(tags) & {'dex','liquidity_pool','pool','exchange','burn',
                             'smart_contract','router','vault','amm','cex'})


def bubble_metrics(pool):
    """Optional wallet analytics for Telegram; never blocks pool eligibility.

    Cluster percentage metrics keep the V40 formulas unchanged.
    Linked Wallets is the number of distinct addresses in connected clusters.
    Top Wallet is the largest non-recognized-technical holder among the top 20
    returned by InsightX Scanner; a missing label does not prove independence.
    """
    mint=token_mint(pool)
    key=os.getenv('INSIGHTX_API_KEY','').strip()
    if not mint or not key:
        return {}
    headers={'X-API-Key':key, 'accept':'application/json'}
    try:
        r=SESSION.get(f'https://api.insightx.network/dex-metrics/v1/sol/{mint}/clusters',
                      headers=headers,timeout=12)
        r.raise_for_status()
        obj=r.json()
        if not isinstance(obj,dict) or not isinstance(obj.get('clusters'),list):
            return {}
        clusters=obj['clusters']
        total=num(obj.get('total_cluster_pct'))
        vals=[num(c.get('pct')) for c in clusters if isinstance(c,dict)]
        vals=[v for v in vals if v is not None]
        if total is None or (clusters and not vals):
            return {}
        maximum=max(vals,default=0)
        # V40's confirmed percentages are intentionally left exactly as before.
        result={'max':maximum,'other':max(0,total-maximum)}
    except (requests.RequestException,ValueError,TypeError) as exc:
        logging.warning('InsightX clusters unavailable for %s: %s',mint,exc)
        return {}

    # An additional request is needed for holders not appearing in any cluster.
    # Both calls below are best-effort; on free-tier limits Top Wallet stays н/д.
    try:
        scan=SESSION.get(f'https://api.insightx.network/scanner/v1/tokens/sol/{mint}',
                         headers=headers,timeout=12)
        scan.raise_for_status()
        report=scan.json()
        holders=(((report.get('results') or {}).get('advanced') or {}).get('top_holders') or [])
        if not isinstance(holders,list) or not holders:
            return result
        holders=[h for h in holders if isinstance(h,dict) and h.get('address')]
        addresses=list(dict.fromkeys(str(h['address']) for h in holders))[:20]
        if not addresses:
            return result
        # Labels API identifies pools/exchanges. Do not use an unclassified
        # scanner list for this metric if the batch label check failed.
        url='https://api.insightx.network/labels/v1/sol/'+','.join(addresses)
        lab=SESSION.get(url,headers=headers,timeout=12)
        lab.raise_for_status()
        raw_labels=lab.json()
        if not isinstance(raw_labels,list):
            return result
        labels={str(x['address']):x for x in raw_labels if isinstance(x,dict) and x.get('address')}
        best=None
        for h in holders:
            if holder_is_technical(h, labels.get(str(h['address']))):
                continue
            pct=num(h.get('percentage'))
            if pct is None or pct<0 or pct>100:
                continue
            best=pct if best is None else max(best,pct)
        if best is not None:
            result['top_wallet']=best
        return result
    except (requests.RequestException,ValueError,TypeError) as exc:
        logging.warning('InsightX scanner/labels unavailable for %s: %s; Top Wallet=н/д',mint,exc)
        return result


def bubble_display(pool,kind):
    v=num((pool.get('_bubble') or {}).get(kind))
    if v is None: return 'н/д'
    return f'{v:.1f}%'


def bubble_color(pool,kind):
    v=num((pool.get('_bubble') or {}).get(kind))
    if v is None: return MUTED
    green,yellow={'max':(5,10),'other':(10,20),'top_wallet':(
        threshold('TOP_WALLET_GREEN_MAX',2.5),threshold('TOP_WALLET_YELLOW_MAX',3.5))}[kind]
    return GREEN if v<=green else (YELLOW if v<=yellow else RED)


def fetch_honeypot_score(mint):
    """Optional InsightX risk score (0 best, 100 worst). No credentials are logged.

    The scanner can rate risk from conditions such as transfer restrictions;
    a zero score does not prove that the token is safe from price manipulation.
    """
    key=os.getenv('INSIGHTX_API_KEY','').strip()
    if not mint or not key:
        return None
    try:
        r=SESSION.get(f'https://api.insightx.network/scanner/v1/tokens/sol/{mint}',
                      headers={'X-API-Key':key,'accept':'application/json'}, timeout=12)
        r.raise_for_status()
        data=r.json()
        advanced=((data.get('results') or {}).get('advanced') or {})
        h=advanced.get('honeypot') or {}
        score=num(h.get('score'))
        if score is None or not 0 <= score <= 100:
            return None
        return int(score) if score.is_integer() else score
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        logging.warning('InsightX honeypot unavailable for %s: %s',mint,exc)
        return None


def honeypot_display(pool):
    score=num(pool.get('_honeypot_score'))
    return 'н/д' if score is None else f'{score:g}/100'


def honeypot_color(pool):
    score=num(pool.get('_honeypot_score'))
    if score is None:
        return MUTED
    # Risk score: low = safer; high = dangerous (NOT a safety score).
    if score == 0: return GREEN
    if score < 50: return YELLOW
    return RED


def x_followers_display(pool):
    """Display verified profile followers only if included in already available metadata.
    No browser-only GMGN API / no invented numbers; inaccessible -> н/д.
    """
    social=verified_social(pool)
    v=(social.get('members') if social.get('community_url') and not social.get('official_url') else social.get('followers'))
    return f'{int(v):,}'.replace(',', ' ') if v is not None else 'н/д'


def x_followers_color(pool):
    # Follower count alone is not proof of organic interest or safety.
    social=verified_social(pool)
    value=social.get('members') if social.get('community_url') and not social.get('official_url') else social.get('followers')
    return WHITE if value is not None else MUTED


def changes_from_original(original,current):
    """Delta between current 30m figures and original 30m figures, never Meteora's change_pct."""
    before=metric_values(original); after=metric_values(current)
    result={}
    for label in METEORA_CHANGE_FIELDS:
        old=before.get(label); new=after.get(label)
        if old is None or new is None:
            result[label]=('н/д',MUTED)
        elif old == 0:
            result[label]=('н/д',MUTED)
        else:
            change=(new-old)/abs(old)*100
            digits=1 if label=='Fees/A TVL' else 0
            result[label]=(f'{change:+.{digits}f}%' if abs(change)>=0.5*10**(-digits) else ('0.0%' if digits else '0%'),
                           GREEN if change>0 else RED if change<0 else MUTED)
    return result


def prepare_card(pool):
    p=dict(pool)
    mint=token_mint(p)
    p['_transfer_fee']=cached(('tf',mint),120,lambda: transfer_fee(p)) if mint else 'н/д'
    p['_bin_rent']=bin_rent(p)
    # Refresh risk/wallet data even when a prior card's _bubble field is inherited.
    # Keep 120 s shared TTL to avoid exceeding InsightX API quota on each click.
    p['_bubble']=cached(('bubble_v46',mint),120,lambda: bubble_metrics(p)) if mint else {}
    # Honeypot is refreshed separately from 30m Meteora metrics for BOTH Update buttons.
    # A short cache avoids needlessly consuming InsightX's free API quota.
    p['_honeypot_score']=cached(('honeypot',mint),120,lambda: fetch_honeypot_score(mint)) if mint else None
    return p


def metric_values(p):
    m=v11.metrics(p); g=p.get('_gmgn') or {}
    disc=p.get('_discovery') or {}
    def get(*keys): return v11.first_num(disc,[keys])
    return {
        'Total TVL':get('tvl'), 'Fees':get('fee'), 'Volume':get('volume'),
        'Swaps':num(m['swaps'].replace(' ','') if isinstance(m['swaps'],str) else m['swaps']),
        'Holders':num(m['holders'].replace(' ','') if isinstance(m['holders'],str) else m['holders']),
        'GMGN Views':num(m['gmgn_views'].replace(' ','') if isinstance(m['gmgn_views'],str) else m['gmgn_views']),
        'Active TVL':get('active_tvl'),
        'Fees/A TVL':num(str(m['fee_ratio']).replace('%','')),
        'Vol/A TVL':num(str(m['vol_ratio']).replace('%','')),
        'Traders':num(str(m['traders']).replace(' ','')),
        'Total LPs':num(str(m['lps']).replace(' ','')),
        'GMGN Total Fees':num(g.get('total_fee')),
        'Top 10':num(g.get('top_10_holder_rate')),
        'Bundler':num(g.get('top_bundler_trader_percentage')),
        'Phishing*':num(g.get('top_entrapment_trader_percentage')),
        'DEV':num(g.get('dev_team_hold_rate')),
        'Rug %':num(g.get('rug_ratio')),
        'Transfer Fee':num(str(p.get('_transfer_fee','')).replace('%','')),
    }


# Meteora Discovery's change_pct fields refer to the requested 30m timeframe.
# They are NOT changes since the Telegram card was sent.
METEORA_CHANGE_FIELDS = {
    'Total TVL': 'tvl_change_pct',
    'Fees': 'fee_change_pct',
    'Volume': 'volume_change_pct',
    'Swaps': 'swap_count_change_pct',
    'Holders': 'base_token_holders_change_pct',
    'Active TVL': 'active_tvl_change_pct',
    'Fees/A TVL': 'fee_active_tvl_ratio_change_pct',
    'Vol/A TVL': 'volume_active_tvl_ratio_change_pct',
    'Traders': 'unique_traders_change_pct',
    'Total LPs': 'total_lps_change_pct',
}


def make_changes(old, new):
    """Use only Meteora's own 30m percentage changes; never invent missing deltas.

    The old argument is retained for compatibility with the existing Update flow.
    GMGN/security fields have no verified Meteora 30m change equivalent.
    """
    disc = new.get('_discovery') or {}
    out = {}
    for label, field in METEORA_CHANGE_FIELDS.items():
        value = num(disc.get(field))
        if value is None:
            continue
        digits = 1 if label == 'Fees/A TVL' else 0
        formatted = f'{value:+.{digits}f}%'
        if abs(value) < 0.5 * 10 ** (-digits):
            formatted = '0.0%' if digits else '0%'
        out[label] = (formatted, GREEN if value > 0 else RED if value < 0 else MUTED)
    return out


def links_with_update(pool, key):
    markup=links(pool) or {'inline_keyboard':[]}
    markup['inline_keyboard'].append([{'text':'↻ Update','callback_data':f'v38:5:{key}'},
                                      {'text':'↻ Update 30MIN','callback_data':f'v38:30:{key}'}])
    return markup


_CARD_CACHE = {}      # (chat_id, message_id) -> (pool_address, original_json, sent_ts); survives a failed SQLite write
SEND_ATTEMPTS = {}    # pool_address -> failed sendPhoto attempts in this process
MAX_SEND_ATTEMPTS = max(1, int(os.getenv('MAX_SEND_ATTEMPTS', '3')))


def store_card(chat, mid, a, original_json, ts):
    """Remember which pool a Telegram message shows. Retries; never raises (the photo is already sent)."""
    _CARD_CACHE[(chat, mid)] = (a, original_json, ts)
    for attempt in range(1, 5):
        try:
            con = v30_db()
            try:
                con.execute('INSERT OR REPLACE INTO v30_cards VALUES(?,?,?,?,?)', (chat, mid, a, original_json, ts))
                con.commit()
            finally:
                con.close()
            return True
        except sqlite3.Error as exc:
            logging.warning('v30_cards: запись не удалась (попытка %s/4): %s', attempt, exc)
            _real_sleep(1)
    logging.error('v30_cards: карточка %s/%s не записана в базу; Update будет работать до перезапуска', chat, mid)
    return False


def token_hashtag(pool):
    # Searchable Telegram hashtag; symbols with dots, hyphens, emoji, etc.
    # cannot be used literally inside a Telegram hashtag.
    base = str((pool.get('token_x') or {}).get('symbol') or '').strip()
    if base.upper() in ('SOL', 'WSOL'):
        base = str((pool.get('token_y') or {}).get('symbol') or '').strip()
    import re
    base = re.sub(r'[^\w]', '_', base, flags=re.UNICODE).strip('_')[:48]
    return '#' + base if base else ''


def card_caption(p, prefix, suffix=''):
    tag = token_hashtag(p)
    # One tag on initial cards and both Update paths; same symbol searches
    # all matching pools, which is useful when there are multiple bin steps.
    return f'{prefix}{symbol(p)}{suffix}' + (f' | {tag}' if tag else '')


def send_card(pool, test=False):
    a = addr(pool)
    try:
        if not test and a:
            con = v30_db()
            try:
                exists = con.execute('SELECT 1 FROM v30_cards WHERE pool_address=? LIMIT 1', (a,)).fetchone()
            finally:
                con.close()
            if exists or any(v[0] == a for v in _CARD_CACHE.values()):
                logging.info('ДУБЛЬ ЗАБЛОКИРОВАН: %s | %s', symbol(pool), a)
                return True   # mark the pool as sent without a second photo
        p = prepare_card(pool)
        photo = approved_card_render(p)
        if not TOKEN or not CHAT:
            path = ROOT / ('radar_v30_test.png' if test else 'radar_v30_preview.png')
            path.write_bytes(photo.getvalue())
            logging.warning('Telegram не настроен; карточка сохранена: %s', path)
            return True
        key = hashlib.sha256((a or symbol(p)).encode()).hexdigest()[:16]
        response = SESSION.post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto',
            data={'chat_id': CHAT, 'caption': card_caption(p, '🧪 ТЕСТ · ' if test else '🆕 Новый пул · '),
                  'reply_markup': json.dumps(links_with_update(p, key))},
            files={'photo': ('radar_v32.png', photo, 'image/png')}, timeout=25)
        response.raise_for_status()
        result = response.json()
        if not result.get('ok'):
            raise RuntimeError(str(result))
        msg = result['result']
    except Exception:
        logging.exception('Ошибка отправки карточки v31')
        return False
    # From here the photo IS in Telegram. Nothing below may report failure, otherwise the radar
    # would send the same pool again on the next scan (this was the v30 bug).
    try:
        store_card(str(msg['chat']['id']), msg['message_id'], a, json.dumps(p, ensure_ascii=False), time.time())
        con = v30_db()
        try:
            record_sample(con, p)
        finally:
            con.close()
    except Exception:
        logging.exception('Карточка отправлена, но не сохранена локально (повторно слать не буду)')
    return True


def discovery_record(a):
    """Fresh Discovery 30m record for one pool: the 'new' feed that detects pools, then the 'top' feed."""
    try:
        feed = cached('disc_new', 20, lambda: {addr(x): (x.get('_discovery') or x)
                                              for x in fetch_recent_discovery(quiet=True) if addr(x)})
        if a in feed:
            return feed[a]
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Discovery (new) недоступен: %s', exc)
    try:
        return cached('disc_top', 60, fetch_discovery_index).get(a)
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Discovery (top) недоступен: %s', exc)
        return None


def refresh_pool(a, base=None, light=False):
    """Current picture of ONE pool: detail API + fresh Discovery + GMGN. light=True skips the RPC lookups."""
    detail = None
    try:
        detail = fetch_detail(a)
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Update %s: детальный API недоступен: %s', a, exc)
        if not base:
            raise
    p = merge_pool(base, detail) if (base and detail) else dict(detail or base)
    p.pop('_discovery', None)          # never present old 30m numbers as fresh
    p['address'] = a
    rec = discovery_record(a)
    if rec:
        p['_discovery'] = rec
    else:
        logging.info('Update %s: свежей записи Discovery нет; проценты изменения будут отсутствовать', a)
    p = enrich_gmgn(p)                 # returns the pool unchanged (keeps the previous GMGN block) if GMGN is down
    return p if light else prepare_card(p)



def discovery_record_5m(a):
    """Look for an exact pool in Meteora Discovery 5m feed; no 30m fallback."""
    for category in ('new', 'top'):
        cursor = None
        seen = set()
        for _ in range(max(1, min(10, int(os.getenv('DISCOVERY_5M_PAGES', '4'))))):
            params = {'page_size': 100, 'timeframe': '5m', 'category': category}
            if category == 'new' and DISCOVERY_FILTER_BY:
                params['filter_by'] = DISCOVERY_FILTER_BY
            if cursor: params['after_key'] = cursor
            try:
                r = SESSION.get(DISCOVERY_API, params=params, timeout=20)
                if r.status_code in (400, 422) and 'filter_by' in params:
                    params.pop('filter_by')
                    r = SESSION.get(DISCOVERY_API, params=params, timeout=20)
                r.raise_for_status()
                payload = r.json()
            except (requests.RequestException, ValueError) as exc:
                logging.warning('Discovery 5m (%s): %s', category, exc)
                break
            batch = payload.get('data', []) if isinstance(payload, dict) else []
            if not isinstance(batch, list): break
            for item in batch:
                if isinstance(item, dict) and addr(item) == a:
                    return item
            next_cursor = payload.get('after_key')
            if not batch or payload.get('has_more') is False or not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
    return None


def refresh_pool_5m(a, base):
    """Return 5m Discovery snapshot; do not silently relabel 30m values."""
    rec = discovery_record_5m(a)
    if rec is None:
        raise RuntimeError('Meteora Discovery не вернул 5m для этого пула. Карточка не изменена.')
    # Require explicit window-sensitive metrics; otherwise do not present a 30m snapshot as 5m.
    required = ('fee', 'volume', 'swap_count', 'unique_traders')
    if any(num(rec.get(k)) is None for k in required):
        raise RuntimeError('Discovery 5m вернул неполные данные; обновление отменено.')
    p = dict(base)
    p.pop('_discovery', None)
    for k in ('fees', 'volume', 'swaps', 'traders'):
        if isinstance(p.get(k), dict): p.pop(k, None)
    p['_discovery'] = rec
    p['address'] = a
    # Enrich token risk/social data without changing 5m trading metrics.
    return prepare_card(enrich_gmgn(p))


def find_card(chat, mid, key):
    """(pool_address, original_json, sent_ts) for a Telegram message, or None."""
    con = v30_db()
    try:
        row = con.execute('SELECT pool_address,original_json,sent_ts FROM v30_cards WHERE chat_id=? AND message_id=?',
                          (chat, mid)).fetchone()
        if row:
            return row
        if (chat, mid) in _CARD_CACHE:
            return _CARD_CACHE[(chat, mid)]
        # Card sent by v30 (its row was never saved): recover the pool from the button key.
        for (a,) in con.execute('SELECT address FROM pools').fetchall():
            if hashlib.sha256(a.encode()).hexdigest()[:16] == key:
                raw = con.execute('SELECT data_json FROM pools WHERE address=?', (a,)).fetchone()[0]
                logging.warning('Update: карточка %s/%s не была сохранена; пул восстановлен по кнопке: %s', chat, mid, a)
                return (a, raw, time.time())
    finally:
        con.close()
    return None


def update_card(call):
    msg = call.get('message') or {}
    chat = str((msg.get('chat') or {}).get('id', '')); mid = msg.get('message_id'); data = call.get('data', '')
    if not chat or not mid:
        return 'skip'
    if data.startswith('v38:'):
        parts = data.split(':', 2)
        if len(parts) != 3 or parts[1] not in ('5', '30'): return 'skip'
        span = int(parts[1]); key = parts[2]
    elif data.startswith('v30:'):
        span = 30; key = data[4:]
    else:
        return 'skip'
    row = find_card(chat, mid, key)
    if not row:
        raise RuntimeError('Карточка не найдена ни в базе, ни среди пулов радара')
    a, original_raw, sent_ts = row
    if hashlib.sha256(a.encode()).hexdigest()[:16] != key:
        raise RuntimeError('Ключ карточки не совпадает')
    original = json.loads(original_raw)
    if '_bin_rent' not in original:                 # card restored from the pools table
        original = prepare_card(original)
        store_card(chat, mid, a, json.dumps(original, ensure_ascii=False), sent_ts)
    new = refresh_pool(a, base=original)  # Both buttons use identical 30m source
    now = time.time()
    # Never mix incomplete Discovery snapshots with stale original values.
    disc = new.get('_discovery') or {}
    required = ('tvl', 'active_tvl', 'fee', 'volume', 'swap_count', 'unique_traders')
    if any(num(disc.get(k)) is None for k in required):
        raise RuntimeError('Meteora Discovery 30m не вернул полный набор значений. Карточка не изменена.')
    if span == 5:
        delta = changes_from_original(original, new)
    else:
        delta = make_changes(original, new) if now - sent_ts >= 1800 else {label:('н/д',MUTED) for label in METEORA_CHANGE_FIELDS}
    photo = render(original, updated=new, changes=delta)
    minutes = max(0, round((now - sent_ts) / 60))
    caption = card_caption(new, '🆕 Новый пул · ', f' | ↻ {"Update" if span == 5 else "30MIN"} · {minutes} мин с первой карточки')
    response = SESSION.post(f'https://api.telegram.org/bot{TOKEN}/editMessageMedia',
        data={'chat_id': chat, 'message_id': mid,
              'media': json.dumps({'type': 'photo', 'media': 'attach://card', 'caption': caption}),
              'reply_markup': json.dumps(links_with_update(new, key))},
        files={'card': ('radar_v32_update.png', photo, 'image/png')}, timeout=35)
    if response.status_code == 400 and 'not modified' in response.text.lower():
        logging.info('UPDATE: показатели не изменились | %s', a)
        return 'same'
    if response.status_code >= 400:
        raise RuntimeError(f'Telegram {response.status_code}: {response.text[:200]}')
    if not response.json().get('ok'):
        raise RuntimeError(response.text[:250])
    logging.info('UPDATE УСПЕШЕН: %s | msg=%s | %s', symbol(new), mid, a)
    return 'ok'


def tg_call(method, **data):
    try:
        SESSION.post(f'https://api.telegram.org/bot{TOKEN}/{method}', data=data, timeout=10)
    except Exception as exc:
        logging.debug('Telegram %s: %s', method, exc)


def telegram_updates_loop():
    # Telegram allows one getUpdates consumer per bot token: run only ONE radar window.
    offset=None
    while True:
        try:
            args={'timeout':5,'allowed_updates':json.dumps(['callback_query'])}
            if offset is not None: args['offset']=offset
            r=SESSION.get(f'https://api.telegram.org/bot{TOKEN}/getUpdates',params=args,timeout=15)
            r.raise_for_status();payload=r.json()
            for upd in payload.get('result',[]):
                offset=upd['update_id']+1
                call=upd.get('callback_query')
                if not call or not str(call.get('data','')).startswith(('v30:', 'v38:')):continue
                msg=call.get('message') or {}
                logging.info('UPDATE НАЖАТ: msg=%s data=%s',msg.get('message_id'),call.get('data'))
                # Answer at once: Telegram drops late answers and the button would spin forever.
                tg_call('answerCallbackQuery',callback_query_id=call['id'],text='Обновляю…')
                try:
                    with _REFRESH_LOCK: update_card(call)
                except Exception as exc:
                    logging.exception('UPDATE ОШИБКА')
                    tg_call('sendMessage',chat_id=(msg.get('chat') or {}).get('id'),
                            reply_to_message_id=msg.get('message_id'),
                            text='⚠️ Update не удался: '+str(exc)[:200])
        except Exception as exc:
            logging.warning('Telegram updates: %s',exc);time.sleep(5)


def sample_loop():
    # Sample every 5 minutes, so a later refresh can be compared with the state ~30 minutes ago.
    while True:
        time.sleep(UPDATE_INTERVAL)
        try:
            con=v30_db(); now=time.time()
            try:
                con.execute('DELETE FROM v30_samples WHERE ts<?',(now-HISTORY_HOURS*3600,));con.commit()
                rows=con.execute('SELECT DISTINCT pool_address FROM v30_cards WHERE sent_ts>? ORDER BY sent_ts DESC LIMIT 15',(now-2*3600,)).fetchall()
            finally:
                con.close()
            for (a,) in rows:
                try:
                    sample=refresh_pool(a,light=True)
                    con=v30_db()
                    try: record_sample(con,sample)
                    finally: con.close()
                except Exception as exc: logging.warning('Sample %s: %s',a,exc)
        except Exception: logging.exception('v31 sampling')


# Full monitor and its filtering remain unchanged.

def diagnose_local():
    """Read-only v29 SQLite overview; does not launch polling or Telegram."""
    if not DB.exists():
        print('Не найдена база:', DB)
        return
    con = sqlite3.connect(f'file:{DB.as_posix()}?mode=ro', uri=True, timeout=5)
    try:
        print('База:', DB)
        for status, count in con.execute('SELECT status,COUNT(*) FROM pools GROUP BY status ORDER BY COUNT(*) DESC'):
            print(f'  {status}: {count}')
        print('Последние 25 пулов в базе:')
        rows=con.execute('SELECT first_seen,status,data_json FROM pools ORDER BY first_seen DESC LIMIT 25').fetchall()
        for first_seen,status,raw in rows:
            try:
                p=json.loads(raw)
                name=symbol(p)
                age=age_minutes(p.get('pool_created_at') or p.get('created_at'))
                mc=market_cap(p)
                fee=pos(first_num(p.get('_discovery') or {}, [('fee_pct',)]))
                if fee is None: fee=pos(first_num(p.get('pool_config') or {}, [('base_fee_pct',)]))
                fees=best_fees30(p)
                print(f'  {str(first_seen)[:19]} | {status:15} | {name:24} | age={age!s:>7}m | MC={mc!s:>10} | fee={fee!s:>6}% | fees30={fees!s}')
            except Exception as exc:
                print('  Запись не разобрана:',type(exc).__name__)
    except sqlite3.Error as exc:
        print('Ошибка чтения базы:',exc)
    finally:
        con.close()


def acquire_single_instance():
    '''Two radar windows on one database = duplicate cards and Telegram 409 errors. Refuse to start twice.'''
    import socket
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1',int(os.getenv('RADAR_LOCK_PORT','47931'))))
    except OSError:
        raise SystemExit('Радар уже запущен в другом окне. Остановите его (Ctrl+C) и запустите заново.')
    return sock   # keep a reference: the lock lives as long as the process


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if '--test-social-routing' in __import__('sys').argv:
        cases = [
          ('Официальный профиль', {'_gmgn':{'link':{'twitter_username':'Payday_Direct'}}}, 'https://x.com/Payday_Direct'),
          ('Сообщество', {'_dex_social':{'community_url':'https://x.com/i/communities/2011049724028047546'}}, 'https://x.com/i/communities/2011049724028047546'),
          ('Публикация', {'_dex_social':{'mention_url':'https://x.com/example/status/1234567890123456789'}}, 'https://x.com/example/status/1234567890123456789'),
          ('Поиск', {}, 'https://x.com/search?q=TEST_MINT'),
        ]
        for name, meta, expected in cases:
            p={'address':'POOL_TEST','token_x':{'symbol':'TEST','address':'TEST_MINT'},'token_y':{'symbol':'SOL'},**meta}
            button=links(p)['inline_keyboard'][0][-1]
            ok=button['url']==expected
            print(('PASS' if ok else 'FAIL'), name, '=>', button['url'])
            if not ok: raise SystemExit(1)
        return
    if '--check-bubble-mint' in __import__('sys').argv:
        args = __import__('sys').argv
        i = args.index('--check-bubble-mint')
        if i + 1 >= len(args) or args[i+1].startswith('--'):
            raise SystemExit('Использование: python Radar_V45.py --check-bubble-mint АДРЕС_ТОКЕНА')
        mint = args[i+1].strip()
        key = os.getenv('INSIGHTX_API_KEY', '').strip()
        if not key:
            raise SystemExit('INSIGHTX_API_KEY не найден в .env')
        headers = {'X-API-Key': key, 'accept': 'application/json'}
        for caption, path in [('Clusters','dex-metrics/v1/sol/'+mint+'/clusters'),
                              ('Scanner','scanner/v1/tokens/sol/'+mint)]:
            try:
                response=SESSION.get('https://api.insightx.network/'+path,headers=headers,timeout=15)
                print(caption, 'HTTP:', response.status_code)
                if response.status_code != 200:
                    print('  API недоступен для запроса (лимит/ошибка/нет данных).')
                    continue
                obj=response.json()
                if caption=='Clusters':
                    clusters=obj.get('clusters') if isinstance(obj,dict) else None
                    total=obj.get('total_cluster_pct') if isinstance(obj,dict) else None
                    print('  total_cluster_pct:', total if total is not None else 'н/д')
                    print('  clusters:', len(clusters) if isinstance(clusters,list) else 'нет списка')
                    if isinstance(clusters,list):
                        vals=[num(c.get('pct')) for c in clusters if isinstance(c,dict)]
                        vals=[v for v in vals if v is not None]
                        print('  Bubble MAX:', max(vals) if vals else ('0' if len(clusters)==0 and total is not None else 'н/д'))
                        print('  Bubble OTHER:', max(0,float(total)-max(vals,default=0)) if total is not None else 'н/д')
                else:
                    advanced=((obj.get('results') or {}).get('advanced') or {}) if isinstance(obj,dict) else {}
                    holders=advanced.get('top_holders') or []
                    print('  top_holders:', len(holders) if isinstance(holders,list) else 'нет списка')
                    print('  Примечание: для Top Wallet также нужен Labels API.')
            except (requests.RequestException,ValueError,TypeError) as exc:
                print('  Ошибка:', str(exc)[:180])
        return
    if '--check-risk-mint' in __import__('sys').argv:
        args=__import__('sys').argv
        i=args.index('--check-risk-mint')
        if i+1 >= len(args):
            raise SystemExit('Использование: python Radar_V44.py --check-risk-mint АДРЕС_ТОКЕНА')
        mint=args[i+1].strip()
        key=os.getenv('INSIGHTX_API_KEY','').strip()
        if not key:
            raise SystemExit('INSIGHTX_API_KEY не найден в .env')
        headers={'X-API-Key':key,'accept':'application/json'}
        try:
            r=SESSION.get(f'https://api.insightx.network/scanner/v1/tokens/sol/{mint}',headers=headers,timeout=15)
            print('Scanner HTTP:',r.status_code)
            r.raise_for_status()
            data=r.json()
            advanced=((data.get('results') or {}).get('advanced') or {})
            hp=advanced.get('honeypot') or {}
            print('Honeypot score:',hp.get('score','н/д'))
            print('Honeypot message:',hp.get('message','н/д'))
            holders=advanced.get('top_holders') or []
            holders=[h for h in holders if isinstance(h,dict) and h.get('address')][:20]
            addresses=list(dict.fromkeys(str(h['address']) for h in holders))
            labels={}
            if addresses:
                lr=SESSION.get('https://api.insightx.network/labels/v1/sol/'+','.join(addresses),headers=headers,timeout=15)
                print('Labels HTTP:',lr.status_code)
                lr.raise_for_status()
                raw=lr.json()
                if isinstance(raw,list):
                    labels={str(x['address']):x for x in raw if isinstance(x,dict) and x.get('address')}
            print('TOP HOLDERS: имя/метка, доля и решение фильтра')
            for h in holders:
                a=str(h['address']); lab=labels.get(a) or {}
                name=lab.get('label') or h.get('name') or h.get('label') or h.get('entity') or 'метки нет'
                pct=h.get('percentage','н/д')
                status='ТЕХНИЧЕСКИЙ / ИСКЛЮЧЁН' if holder_is_technical(h,lab) else 'НЕ РАСПОЗНАН / НУЖНА ПРОВЕРКА'
                print(f' {str(pct):>8}% | {a[:7]}...{a[-6:]} | {str(name)[:50]} | {status}')
        except (requests.RequestException,ValueError,TypeError) as exc:
            print('Ошибка проверки:',str(exc)[:250])
        return
    if '--check-social-mint' in __import__('sys').argv:
        argv=__import__('sys').argv
        idx=argv.index('--check-social-mint')
        if idx + 1 >= len(argv):
            raise SystemExit('Использование: python Radar_V46.py --check-social-mint АДРЕС_ТОКЕНА')
        mint=argv[idx+1].strip()
        test_pool={'address':'TEST_POOL','token_x':{'symbol':'TOKEN','address':mint},'token_y':{'symbol':'SOL'}}
        enriched=enrich_gmgn(test_pool)
        soc=verified_social(enriched)
        print('Token:',mint)
        print('GMGN direct social:', json.dumps(verified_social({'_gmgn': enriched.get('_gmgn') or {}}),ensure_ascii=False))
        print('DEX social:',json.dumps(enriched.get('_dex_social') or {},ensure_ascii=False))
        print('Social:',json.dumps(soc,ensure_ascii=False))
        button=links(enriched)['inline_keyboard'][0][-1]
        print('X button:',button['text'],'->',button['url'])
        print('X Followers:',soc.get('followers','н/д'))
        print('X Community Members:',soc.get('members','н/д'))
        return
    if '--check-twitter' in __import__('sys').argv:
        argv = __import__('sys').argv
        idx = argv.index('--check-twitter')
        if idx + 1 >= len(argv) or argv[idx + 1].startswith('--'):
            raise SystemExit('Укажите адрес пула после --check-twitter')
        twitter_diagnostic(argv[idx + 1].strip())
        return
    if '--diagnose' in __import__('sys').argv:
        diagnose_local()
        return
    if '--bin-rent' in __import__('sys').argv:
        argv=__import__('sys').argv; idx=argv.index('--bin-rent')
        if idx+1>=len(argv): raise SystemExit('Укажите адрес пула: python radar_v31.py --bin-rent АДРЕС_ПУЛА')
        pool_address=argv[idx+1].strip()
        detail=fetch_detail(pool_address); detail['address']=pool_address
        d=bin_rent_details(detail)
        print(f'Пул: {symbol(detail)} | {pool_address}')
        print(f'Активный бин: {d["active"]} | предполагаемый диапазон: {d["lo"]} .. {d["hi"]} ({d["hi"]-d["lo"]+1} бинов)')
        for index,address,ok in zip(d['indices'],d['addresses'],d['exists']):
            print(f'  bin array #{index}: {address[:4]}…{address[-4:]}  {"СУЩЕСТВУЕТ" if ok else "НЕ СОЗДАН (первый LP заплатит)"}')
        print(f'Аренда одного bin array: {d["lamports"]/1e9:.5f} SOL')
        print(f'Итог Bin Rent: {"0 (платить не нужно)" if not d["missing"] else "≈%.3f SOL безвозвратно" % d["sol"]}')
        return
    if '--preview' in __import__('sys').argv or '--test-telegram' in __import__('sys').argv:
        example={'address':'11111111111111111111111111111111',
          'token_x':{'symbol':'PILLHEADS'},'token_y':{'symbol':'SOL'},
          'pool_config':{'base_fee_pct':5,'bin_step':100},
          '_discovery':{'market_cap':243000,'tvl':191,'active_tvl':0,'fee':7,'volume':142,
             'swap_count':8,'unique_traders':2,'total_lps':0,'base_token_holders':722,
             'fee_active_tvl_ratio':0,'volume_active_tvl_ratio':0},
          '_gmgn':{'visiting_count':529,'total_fee':4,'top_10_holder_rate':.05,
             'top_bundler_trader_percentage':.49,'top_entrapment_trader_percentage':0,
             'dev_team_hold_rate':0,'rug_ratio':0},'_transfer_fee':'0%','_bin_rent':'н/д'}
        if '--test-telegram' in __import__('sys').argv:
            print('Отправка тестовой карточки:', send_card(example,test=True))
            return
        output=ROOT/'radar_v30_compact_preview.png'
        output.write_bytes(render(example).getvalue())
        print('Проверка карточки завершена:',output)
        return
    guard=acquire_single_instance()
    logging.info('Radar v39 SAFE | база=%s | source=%s | discovery filter=%r | legacy page=%s | окно свежести=%s мин | потолок возраста=%s мин | лимит карточек/скан=%s',
                 DB, LIST_SOURCE, DISCOVERY_FILTER_BY, LEGACY_PAGE_SIZE, SIGNAL_MAX_AGE_MINUTES, SEND_MAX_AGE_MINUTES, MAX_SENDS_PER_SCAN)
    if TOKEN and CHAT:
        Thread(target=telegram_updates_loop,daemon=True).start()
        # No periodic history collection: only initial snapshot + manual updates.
    try:
        run()
    except KeyboardInterrupt:
        logging.info('Мониторинг v32 остановлен пользователем')


if __name__ == '__main__':
    main()

"""Refresh sourced deal observations for display; never authorize inference."""
from datetime import datetime, timezone, timedelta
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.request
from subscription_value import atomic_json

URL = 'https://commandcode.ai/docs/resources/pricing-limits'
FRESH_SECONDS = 6 * 3600
MODELS = ('deepseek-v4.1-flash', 'mimo-v2.6-flash', 'minimax-m3', 'grok-4.7',
          'mimo-v2.5', 'stealth/space-bunny-alpha', 'laguna-s-2.1-free',
          'ling-3.0-flash-sante:free', 'typesafe/jev')

class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.ignored=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.ignored+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.ignored=max(0,self.ignored-1)
    def handle_data(self,data):
        if not self.ignored:self.parts.append(data)


def parse(raw, now):
    parser=Text();parser.feed(raw.decode('utf-8'))
    text=re.sub(r'\s+',' ',' '.join(parser.parts))
    offers=[]
    for model in MODELS:
        # Only recognized explicit DEAL banners; a model's mention in a price table isn't a deal.
        match=re.search(r'\bDEAL\s*'+re.escape(model)+r'\s+(.{1,450}?)(?=\s+DEAL\b|How it works|Overview|$)',text)
        if not match:continue
        description=match.group(1).strip()
        # Keep only bounded promotional prose with a visible benefit, not arbitrary page sections.
        if not any(word in description.lower() for word in ('free','usage','off','credits')):continue
        expiry = None
        dated = re.search(r'through (?:Sep(?:tember)?)[ ]+(\d{1,2})(?:st|nd|rd|th)?(?:, (\d{4}))?', description)
        if dated:
            try: expiry = datetime(int(dated.group(2) or now.year), 9, int(dated.group(1))).date().isoformat()
            except ValueError: pass
        offers.append({'expires_date':expiry, 'provider':'command-code','model':model,'description':description,
                       'terms':'Provider advertisement; eligibility, current tariff and callable route require dispatch-time verification.',
                       'source':URL})
    if not offers:raise ValueError('No recognized deal banners')
    return {'schema_version':1,'observed_at':now.isoformat(),'fresh_for_seconds':FRESH_SECONDS,
            'source_sha256':hashlib.sha256(raw).hexdigest(),'offers':offers}


def _validate_observation(value, now, *, require_digest):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('invalid offer observation schema')
    stamp = value.get('observed_at')
    if not isinstance(stamp, str): raise ValueError('invalid offer observation timestamp')
    try: observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError as exc: raise ValueError('invalid offer observation timestamp') from exc
    if observed.tzinfo is None or observed.utcoffset() is None: raise ValueError('offer timestamp needs timezone')
    observed = observed.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    if observed > now_utc: raise ValueError('offer observation is in the future')
    freshness = value.get('fresh_for_seconds')
    if type(freshness) is not int or not 0 < freshness <= 86400:
        raise ValueError('invalid offer freshness interval')
    digest = value.get('source_sha256')
    if (require_digest or digest is not None) and (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('invalid offer source digest')
    offers = value.get('offers')
    if not isinstance(offers, list) or not offers or len(offers) > len(MODELS):
        raise ValueError('invalid offer list')
    seen = set()
    for offer in offers:
        if not isinstance(offer, dict): raise ValueError('invalid offer entry')
        if (offer.get('provider') != 'command-code' or offer.get('model') not in MODELS
                or offer['model'] in seen or offer.get('source') != URL
                or not isinstance(offer.get('description'), str) or not offer['description'].strip()
                or len(offer['description']) > 450 or not isinstance(offer.get('terms'), str)
                or not offer['terms'].strip() or len(offer['terms']) > 500):
            raise ValueError('invalid offer fields')
        expiry = offer.get('expires_date')
        if expiry is not None:
            if not isinstance(expiry, str): raise ValueError('invalid offer expiry')
            try: datetime.strptime(expiry, '%Y-%m-%d')
            except ValueError as exc: raise ValueError('invalid offer expiry') from exc
        seen.add(offer['model'])
    return value


def load(directory, now=None, fetch=None):
    now=now or datetime.now(timezone.utc)
    path=Path(directory)/'offer-observations.json'
    cached=None
    try:
        cached=_validate_observation(json.loads(path.read_text()), now, require_digest=True)
        age=(now-datetime.fromisoformat(cached['observed_at'].replace('Z','+00:00'))).total_seconds()
        if 0<=age<cached['fresh_for_seconds']:return cached
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    try:
        if fetch is None:
            req=urllib.request.Request(URL,headers={'User-Agent':'TMOS-AI-Usage/0.3'})
            with urllib.request.urlopen(req,timeout=8) as response:raw=response.read(2*1024*1024+1)
        else:raw=fetch()
        if len(raw)>2*1024*1024:raise ValueError('oversized offer page')
        result=parse(raw,now)
        atomic_json(path,result)
        return result
    except (OSError,ValueError,UnicodeError,TypeError):
        # Preserve timestamp on failure so the UI accurately flags stale evidence.
        if isinstance(cached,dict):return cached
        bundled = json.loads(Path(__file__).with_name('offer-observations.json').read_text())
        return _validate_observation(bundled, now, require_digest=False)
